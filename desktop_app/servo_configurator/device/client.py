"""Клиент устройства: обмен командами поверх транспорта.

Слой решает три задачи, которые не должны попасть ни в UI, ни в транспорт:

* фоновое чтение — байты разбираются в отдельном потоке, поэтому вызывающий код
  никогда не ждёт данных в потоке отрисовки;
* сопоставление ответов запросам по ``id`` и таймауты;
* обнаружение потери связи, в том числе молчаливой — когда порт формально открыт,
  а устройство перестало отвечать.

Модуль намеренно не зависит от Qt: он проверяется обычными тестами, а привязка к
сигналам делается уровнем выше.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..protocol import (
    IDEMPOTENT_COMMANDS,
    Command,
    CommandError,
    CommandTimeout,
    LineAssembler,
    LinkError,
    Notification,
    ProtocolError,
    Request,
    Response,
    TransportError,
    decode_message,
    encode_request,
)
from ..transport.base import Transport

logger = logging.getLogger(__name__)

#: Таймаут ответа на обычную команду, с (раздел 6 протокола).
DEFAULT_TIMEOUT = 1.0

#: Таймаут команд, которые пишут в энергонезависимую память, с.
NVS_TIMEOUT = 3.0

#: Число повторов идемпотентной команды после таймаута.
MAX_RETRIES = 2

#: Квант чтения из транспорта, с.
_READ_TIMEOUT = 0.05

#: Максимальный ``id`` запроса перед возвратом к единице.
_MAX_REQUEST_ID = 0xFFFF


@dataclass
class _PendingRequest:
    """Ожидание ответа на отправленную команду."""

    event: threading.Event = field(default_factory=threading.Event)
    response: Response | None = None


class DeviceClient:
    """Транспортно-независимый клиент устройства.

    :param transport: открытый или закрытый канал; клиент открывает его сам.
    :param on_notification: вызывается из потока чтения на каждое событие
        устройства. Обработчик обязан быть быстрым и не блокировать поток.
    :param on_link_lost: вызывается один раз при потере связи.
    """

    def __init__(
        self,
        transport: Transport,
        on_notification: Callable[[Notification], None] | None = None,
        on_link_lost: Callable[[str, str], None] | None = None,
    ) -> None:
        self._transport = transport
        self._on_notification = on_notification
        self._on_link_lost = on_link_lost

        self._assembler = LineAssembler()
        self._pending: dict[int, _PendingRequest] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

        self._reader: threading.Thread | None = None
        self._stopping = threading.Event()
        self._next_id = 1
        self._link_lost_reported = False
        self._last_write_at = 0.0

    @property
    def seconds_since_write(self) -> float:
        """Сколько секунд прошло с последней отправки команды.

        По этой величине уровень выше решает, нужно ли послать пустой запрос,
        чтобы watchdog устройства не принял тишину за обрыв связи.
        """
        return time.monotonic() - self._last_write_at

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._transport.is_open and not self._stopping.is_set()

    @property
    def description(self) -> str:
        return self._transport.description

    def connect(self) -> None:
        """Открывает канал и запускает поток чтения.

        :raises TransportError: канал недоступен.
        """
        if self.is_connected:
            return

        self._stopping.clear()
        self._link_lost_reported = False
        self._assembler.reset()
        self._transport.open()

        # Поле заполняется только после старта: иначе одновременный disconnect
        # попытается присоединиться к ещё не запущенному потоку.
        reader = threading.Thread(target=self._read_loop, name="device-reader", daemon=True)
        reader.start()
        self._reader = reader
        logger.info("подключено: %s", self._transport.description)

    def report_link_lost(self, code: str, message: str) -> None:
        """Объявляет связь потерянной по решению вызывающего кода.

        Нужна там, где обрыв не проявляется ошибкой транспорта: порт открыт и
        запись проходит, но устройство перестало отвечать. Дальнейшие попытки
        обмена в такой ситуации бессмысленны.
        """
        self._handle_link_loss(code, message)

    def clear_callbacks(self) -> None:
        """Перестаёт вызывать подписчиков (см. :meth:`ServoDevice.clear_callbacks`)."""
        self._on_notification = None
        self._on_link_lost = None

    def disconnect(self) -> None:
        """Останавливает чтение и закрывает канал. Повторный вызов безопасен."""
        self._stopping.set()

        reader, self._reader = self._reader, None
        if (reader is not None and reader is not threading.current_thread()
                and reader.is_alive()):
            reader.join(timeout=2.0)

        self._transport.close()
        self._fail_pending()
        logger.info("отключено")

    # ------------------------------------------------------------------
    # Отправка команд
    # ------------------------------------------------------------------

    def request(
        self,
        command: Command,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
        *,
        retry: bool = True,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """Отправляет команду и дожидается ответа.

        Вызов блокирующий, поэтому выполняется в рабочем потоке приложения,
        но не в потоке UI.

        :param retry: разрешить повторы идемпотентной команды. Отключается там,
            где повтор организует сам вызывающий код: фоновый опрос канала
            повторяется по своему расписанию, и внутренние попытки лишь
            растягивают обнаружение молчания устройства.
        :param quiet: не писать в журнал о неудаче. Для фонового опроса, который
            сообщает о происходящем сам и одним сообщением, а не на каждый цикл.
        :returns: содержимое поля ``data`` успешного ответа.
        :raises CommandError: устройство отклонило команду.
        :raises CommandTimeout: ответ не получен за отведённое время.
        :raises TransportError: связь потеряна.
        """
        if timeout is None:
            timeout = NVS_TIMEOUT if command in _NVS_COMMANDS else DEFAULT_TIMEOUT

        attempts = MAX_RETRIES + 1 if (retry and command in IDEMPOTENT_COMMANDS) else 1
        last_error: CommandTimeout | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._exchange(command, args or {}, timeout)
            except CommandTimeout as exc:
                last_error = exc
                # Отдельные попытки пишутся в отладочный уровень: при молчании
                # устройства их десятки в секунду, и они заслоняют собой
                # сообщение о настоящей причине.
                logger.debug("таймаут команды %s (попытка %d из %d)",
                             command, attempt, attempts)
                continue

            if not response.ok:
                raise CommandError(response.err or "", response.msg)
            return response.data

        assert last_error is not None
        if not quiet:
            logger.warning("команда %s не выполнена: устройство не ответило", command)
        raise last_error

    def _exchange(self, command: Command, args: dict[str, Any], timeout: float) -> Response:
        """Один цикл «отправить — дождаться ответа»."""
        if not self._transport.is_open:
            raise TransportError("устройство не подключено", LinkError.DISCONNECTED)

        request_id = self._allocate_id()
        pending = _PendingRequest()

        with self._lock:
            self._pending[request_id] = pending

        try:
            frame = encode_request(Request(id=request_id, cmd=command, args=args))
            # Запись сериализуется: команды могут отправляться из разных потоков
            # (например, аварийный останов во время чтения конфигурации).
            with self._write_lock:
                self._transport.write(frame)
                self._last_write_at = time.monotonic()

            if not pending.event.wait(timeout):
                raise CommandTimeout(f"устройство не ответило на {command} за {timeout} с")

            response = pending.response
            if response is None:
                raise TransportError("связь потеряна во время обмена", LinkError.DISCONNECTED)
            return response
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _allocate_id(self) -> int:
        with self._lock:
            request_id = self._next_id
            self._next_id = request_id % _MAX_REQUEST_ID + 1
        return request_id

    # ------------------------------------------------------------------
    # Поток чтения
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                chunk = self._transport.read(_READ_TIMEOUT)
            except TransportError as exc:
                self._handle_link_loss(exc.code, str(exc))
                return
            except Exception:
                # Поток чтения не имеет права умереть молча: без этого приложение
                # осталось бы подключённым к каналу, который больше не читается.
                logger.exception("непредвиденная ошибка в потоке чтения")
                self._handle_link_loss(LinkError.DISCONNECTED, "внутренняя ошибка чтения")
                return

            if not chunk:
                continue

            for line in self._assembler.feed(chunk):
                self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        """Разбирает строку и направляет её ожидающему запросу или подписчику."""
        try:
            message = decode_message(line)
        except ProtocolError as exc:
            # Битый кадр не повод рвать связь: протокол самосинхронизируется на
            # следующем разделителе, поэтому сообщение просто отбрасывается.
            logger.warning("отброшено сообщение: %s (%.120s)", exc, line)
            return

        if isinstance(message, Response):
            self._deliver_response(message)
        else:
            self._deliver_notification(message)

    def _deliver_response(self, response: Response) -> None:
        with self._lock:
            pending = self._pending.get(response.id)

        if pending is None:
            # Ответ на команду, которую вызывающий код уже перестал ждать.
            logger.debug("ответ на неизвестный запрос id=%d отброшен", response.id)
            return

        pending.response = response
        pending.event.set()

    def _deliver_notification(self, notification: Notification) -> None:
        if self._on_notification is None:
            return
        try:
            self._on_notification(notification)
        except Exception:
            # Ошибка подписчика не должна останавливать чтение канала.
            logger.exception("ошибка обработчика события %s", notification.evt)

    def _handle_link_loss(self, code: str, message: str) -> None:
        """Единая точка обработки потери связи."""
        if self._link_lost_reported:
            return
        self._link_lost_reported = True

        logger.error("потеря связи: %s", message)
        self._stopping.set()
        self._fail_pending()

        try:
            self._transport.close()
        except Exception:
            logger.debug("ошибка при закрытии транспорта после обрыва", exc_info=True)

        if self._on_link_lost is not None:
            try:
                self._on_link_lost(code, message)
            except Exception:
                logger.exception("ошибка обработчика потери связи")

    def _fail_pending(self) -> None:
        """Будит всех ожидающих: без ответа они провисели бы до таймаута."""
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()

        for item in pending:
            item.response = None
            item.event.set()


#: Команды, работающие с энергонезависимой памятью: им отведён больший таймаут.
_NVS_COMMANDS = frozenset({Command.SAVE_CONFIG, Command.RESTORE_DEFAULTS})
