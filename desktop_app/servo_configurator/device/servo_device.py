"""Типизированный API устройства поверх :class:`~.client.DeviceClient`.

Здесь протокол превращается в методы предметной области: вместо
``request(Command.MOVE_TO, {"pos": 2048})`` вызывающий код пишет
``device.move_to(2048)``. Благодаря этому имена команд и структура их аргументов
не расползаются по приложению, а остаются в одном файле.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ..protocol import (
    Command,
    Direction,
    Event,
    Notification,
    PROTOCOL_VERSION,
    Telemetry,
    validate_config,
)
from ..transport.base import Transport
from .client import DeviceClient

logger = logging.getLogger(__name__)

#: Во сколько раз молчание телеметрии должно превысить её период, чтобы связь
#: считалась потерянной, и минимальный порог в секундах (раздел 6 протокола).
_SILENCE_PERIODS = 3
_MIN_SILENCE_TIMEOUT = 0.5

#: Период фонового ``ping``, с. Втрое чаще, чем самый строгий watchdog устройства
#: (``safety.link_timeout_ms`` = 1000 мс), чтобы одна пропущенная посылка не
#: приводила к аварийной остановке привода.
_KEEPALIVE_INTERVAL = 0.3


class ServoDevice:
    """Управление сервоприводом через контроллер.

    :param on_telemetry: вызывается на каждый корректный кадр телеметрии.
    :param on_event: вызывается на прочие события устройства.
    :param on_link_lost: вызывается при потере связи.
    """

    def __init__(
        self,
        transport: Transport,
        on_telemetry: Callable[[Telemetry], None] | None = None,
        on_event: Callable[[Notification], None] | None = None,
        on_link_lost: Callable[[str, str], None] | None = None,
    ) -> None:
        self._on_telemetry = on_telemetry
        self._on_event = on_event
        self._client = DeviceClient(
            transport,
            on_notification=self._handle_notification,
            on_link_lost=on_link_lost,
        )

        self._telemetry_lock = threading.Lock()
        self._last_telemetry_at: float | None = None
        self._telemetry_period_s = 0.05
        self._last_telemetry: Telemetry | None = None

        self._keepalive_stop = threading.Event()
        self._keepalive: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Соединение
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def description(self) -> str:
        return self._client.description

    def connect(self) -> dict[str, Any]:
        """Подключается и выполняет handshake.

        Handshake обязателен: он подтверждает, что за портом действительно
        контроллер с совместимой прошивкой, а не произвольное устройство,
        занявшее тот же COM-порт.

        :returns: сведения об устройстве из ответа на ``ping``.
        :raises TransportError: канал недоступен.
        :raises CommandTimeout: устройство не отвечает.
        """
        self._client.connect()
        try:
            info = self.ping()
        except Exception:
            # Неудачный handshake не должен оставлять открытый порт: иначе
            # повторное подключение упрётся в занятый порт самим приложением.
            self._client.disconnect()
            raise

        self._start_keepalive()
        remote_version = info.get("proto")
        if remote_version != PROTOCOL_VERSION:
            logger.warning("версия протокола устройства %s, приложения %d",
                           remote_version, PROTOCOL_VERSION)
        return info

    def clear_callbacks(self) -> None:
        """Отвязывает обработчики событий.

        Вызывается перед уничтожением подписчика: поток чтения живёт до
        закрытия канала и иначе обратится к уже удалённому объекту.
        """
        self._on_telemetry = None
        self._on_event = None
        self._client.clear_callbacks()

    def disconnect(self) -> None:
        self._stop_keepalive()
        with self._telemetry_lock:
            self._last_telemetry_at = None
        self._client.disconnect()

    # ------------------------------------------------------------------
    # Поддержание канала
    # ------------------------------------------------------------------

    def _start_keepalive(self) -> None:
        """Запускает фоновую отправку ``ping`` в паузах между командами.

        Прошивка останавливает привод, если ПК молчит дольше
        ``safety.link_timeout_ms`` (раздел 6 протокола). Пауза возникает в самой
        обычной ситуации: команда перемещения отправлена, и приложение просто
        ждёт её выполнения. Без фонового запроса такое ожидание было бы
        неотличимо от обрыва USB, и привод останавливался бы посреди штатного
        движения.
        """
        self._keepalive_stop.clear()
        # Как и поток чтения, поле заполняется после старта.
        thread = threading.Thread(
            target=self._keepalive_loop, name="device-keepalive", daemon=True
        )
        thread.start()
        self._keepalive = thread

    def _stop_keepalive(self) -> None:
        self._keepalive_stop.set()
        thread, self._keepalive = self._keepalive, None
        if (thread is not None and thread is not threading.current_thread()
                and thread.is_alive()):
            thread.join(timeout=2.0)

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.wait(_KEEPALIVE_INTERVAL):
            if not self._client.is_connected:
                return
            if self._client.seconds_since_write < _KEEPALIVE_INTERVAL:
                continue
            try:
                self.ping()
            except Exception:
                # Обрыв обнаружит поток чтения; здесь достаточно прекратить опрос.
                logger.debug("keepalive прерван", exc_info=True)
                return

    # ------------------------------------------------------------------
    # Сервисные команды
    # ------------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        """Проверяет связь и возвращает сведения об устройстве."""
        return self._client.request(Command.PING)

    def read_config(self) -> tuple[dict[str, Any], bool]:
        """Читает конфигурацию устройства.

        :returns: пара «конфигурация, признак несохранённых изменений».
        """
        data = self._client.request(Command.GET_CONFIG)
        config = data.get("config", {})
        return config, bool(data.get("dirty", False))

    def write_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Записывает изменённые параметры в оперативную память устройства.

        Патч проверяется локально до отправки: так неверное значение сразу
        объясняется пользователю, а не возвращается кодом ``E_RANGE``, и в порт
        не уходит заведомо отвергаемая команда.

        :raises ValueError: патч не прошёл локальную проверку.
        """
        if errors := validate_config(patch, partial=True):
            raise ValueError("; ".join(errors))
        return self._client.request(Command.SET_CONFIG, {"config": patch})

    def save_config(self) -> None:
        """Сохраняет конфигурацию в энергонезависимую память контроллера."""
        self._client.request(Command.SAVE_CONFIG)

    def restore_defaults(self, *, save: bool = False) -> dict[str, Any]:
        """Возвращает заводские значения; при ``save`` сразу их сохраняет."""
        data = self._client.request(Command.RESTORE_DEFAULTS, {"save": save})
        return data.get("config", {})

    def set_telemetry(self, enabled: bool, period_ms: int = 50) -> None:
        """Включает или выключает поток телеметрии."""
        self._client.request(
            Command.TELEMETRY, {"enabled": enabled, "period_ms": period_ms}
        )
        with self._telemetry_lock:
            self._telemetry_period_s = period_ms / 1000.0
            self._last_telemetry_at = time.monotonic() if enabled else None

    # ------------------------------------------------------------------
    # Движение
    # ------------------------------------------------------------------

    def start_homing(self) -> None:
        """Запускает Homing; результат придёт событием ``homing``."""
        self._client.request(Command.HOME_START)

    def abort_homing(self) -> None:
        """Прерывает выполняющийся Homing."""
        self._client.request(Command.HOME_ABORT)

    def move_to(self, position: int, speed: int | None = None) -> int:
        """Перемещает привод в заданную позицию.

        :returns: подтверждённая устройством целевая позиция.
        """
        args: dict[str, Any] = {"pos": position}
        if speed is not None:
            args["speed"] = speed
        data = self._client.request(Command.MOVE_TO, args)
        return int(data.get("target", position))

    def motor_run(self, direction: Direction, speed: int | None = None) -> None:
        """Включает непрерывное вращение в заданную сторону."""
        args: dict[str, Any] = {"dir": str(direction)}
        if speed is not None:
            args["speed"] = speed
        self._client.request(Command.MOTOR_RUN, args)

    def stop(self, *, emergency: bool = False) -> None:
        """Останавливает движение.

        Команда разрешена в любом состоянии и служит выходом из ``fault``.
        При ``emergency`` дополнительно снимается момент удержания.
        """
        self._client.request(Command.STOP, {"emergency": emergency})

    # ------------------------------------------------------------------
    # Телеметрия
    # ------------------------------------------------------------------

    @property
    def last_telemetry(self) -> Telemetry | None:
        """Последний принятый кадр телеметрии."""
        with self._telemetry_lock:
            return self._last_telemetry

    def is_telemetry_stalled(self) -> bool:
        """Прервался ли поток телеметрии при формально исправном канале.

        Отвечает на случай, когда порт открыт, но данных больше нет: например,
        контроллер завис. Обычная ошибка транспорта такую ситуацию не выявляет.
        """
        with self._telemetry_lock:
            if self._last_telemetry_at is None:
                return False
            timeout = max(_MIN_SILENCE_TIMEOUT, self._telemetry_period_s * _SILENCE_PERIODS)
            return time.monotonic() - self._last_telemetry_at > timeout

    def _handle_notification(self, notification: Notification) -> None:
        """Разбирает событие устройства; вызывается из потока чтения."""
        if notification.evt == Event.TELEMETRY:
            telemetry = Telemetry.from_dict(notification.data)
            with self._telemetry_lock:
                self._last_telemetry_at = time.monotonic()
                self._last_telemetry = telemetry
            if self._on_telemetry is not None:
                self._on_telemetry(telemetry)
            return

        if self._on_event is not None:
            self._on_event(notification)
