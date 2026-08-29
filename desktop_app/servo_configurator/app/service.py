"""Сервисный слой между UI и устройством.

Решает главную задачу раздела 9 задания — «Serial и телеметрия не должны
блокировать главный UI-поток»:

* команды выполняются в рабочем потоке, а UI получает результат сигналом;
* события устройства приходят из потока чтения и превращаются в сигналы Qt,
  которые Qt сам доставляет в поток получателя;
* аварийный останов исполняется в обход очереди команд — он не должен ждать,
  пока завершится чтение конфигурации.

UI не знает ни о потоках, ни о протоколе: он подключается к сигналам и вызывает
методы этого класса.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from ..device import ServoDevice
from ..protocol import (
    CommandError,
    CommandTimeout,
    DeviceState,
    Direction,
    Event,
    Notification,
    Telemetry,
    TransportError,
    describe,
)
from ..transport import SerialTransport, SimulatedTransport
from ..transport.base import Transport

logger = logging.getLogger(__name__)

#: Период телеметрии по умолчанию, мс.
DEFAULT_TELEMETRY_PERIOD_MS = 50


class ServoService(QObject):
    """Фасад устройства для UI.

    Все сигналы доставляются в поток, которому принадлежит получатель, поэтому
    обработчики в UI могут свободно трогать виджеты.
    """

    #: Успешное подключение; передаются сведения из handshake.
    connected = pyqtSignal(dict)

    #: Отключение. Пустая строка — по команде пользователя, иначе причина обрыва.
    disconnected = pyqtSignal(str)

    #: Очередной кадр телеметрии (:class:`Telemetry`).
    telemetry_received = pyqtSignal(object)

    #: Смена состояния устройства (:class:`DeviceState`).
    state_changed = pyqtSignal(object)

    #: Результат процедуры Homing: исход, позиция, длительность в мс.
    homing_finished = pyqtSignal(str, int, int)

    #: Прочитанная конфигурация и признак несохранённых изменений.
    config_loaded = pyqtSignal(dict, bool)

    #: Сообщение для строки состояния.
    status_message = pyqtSignal(str)

    #: Ошибка: краткий заголовок и подробность.
    error_occurred = pyqtSignal(str, str)

    #: Выполняется команда — UI блокирует органы управления.
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._device: ServoDevice | None = None
        self._transport: Transport | None = None

        # Один рабочий поток: команды устройству идут строго последовательно,
        # как и ожидает протокол «запрос — ответ».
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="servo-cmd")
        # Отдельный поток для останова: он не должен стоять в общей очереди.
        self._urgent = ThreadPoolExecutor(max_workers=1, thread_name_prefix="servo-stop")
        self._pending = 0

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._device is not None and self._device.is_connected

    @property
    def is_demo(self) -> bool:
        """Работает ли приложение с симулятором."""
        return isinstance(self._transport, SimulatedTransport)

    def is_telemetry_stalled(self) -> bool:
        """Прервался ли поток телеметрии при формально исправном канале.

        Проверяется по таймеру из UI: порт может остаться открытым, а данные
        прекратиться — обычная ошибка транспорта такого не показывает.
        """
        device = self._device
        return device is not None and device.is_telemetry_stalled()

    @property
    def transport(self) -> Transport | None:
        """Текущий транспорт — нужен UI для имитации неисправностей в Demo."""
        return self._transport

    # ------------------------------------------------------------------
    # Соединение
    # ------------------------------------------------------------------

    def connect_to(self, port: str | None) -> None:
        """Подключается к порту; ``None`` — запустить симулятор.

        Подключение выполняется в рабочем потоке: открытие порта и handshake
        занимают заметное время, а на неотвечающем устройстве — целую секунду.
        """
        if self.is_connected:
            return

        transport: Transport = (
            SimulatedTransport() if port is None else SerialTransport(port)
        )
        device = ServoDevice(
            transport,
            on_telemetry=self._on_telemetry,
            on_event=self._on_event,
            on_link_lost=self._on_link_lost,
        )
        self._transport = transport
        self._device = device

        def task() -> dict[str, Any]:
            # Устройство захвачено замыканием: пользователь может нажать
            # «Отключить» или закрыть окно, пока идёт подключение, и поле
            # экземпляра к этому моменту уже обнулится.
            info = device.connect()
            device.set_telemetry(True, DEFAULT_TELEMETRY_PERIOD_MS)
            return info

        def on_success(info: dict[str, Any]) -> None:
            self.connected.emit(info)
            self.status_message.emit(
                f"Подключено: {info.get('dev', '?')}, прошивка {info.get('fw', '?')}"
            )
            self.read_config()

        def on_failure(error: Exception) -> None:
            device.clear_callbacks()
            if self._device is device:
                self._device = None
                self._transport = None
            self.disconnected.emit(_describe_exception(error))

        self._submit(task, on_success, on_failure, title="Подключение")

    def disconnect(self) -> None:
        """Отключается по команде пользователя.

        Интерфейс уведомляется немедленно, а канал закрывается в рабочем потоке:
        закрытие может занять до секунды, и всё это время органы управления не
        должны выглядеть работающими.
        """
        device = self._release_device()
        if device is None:
            return

        self.disconnected.emit("")

        def task() -> None:
            device.disconnect()

        self._submit(task, title="Отключение")

    # ------------------------------------------------------------------
    # Конфигурация
    # ------------------------------------------------------------------

    def read_config(self) -> None:
        """Читает конфигурацию устройства."""
        def task() -> tuple[dict[str, Any], bool]:
            return self._require_device().read_config()

        def on_success(result: tuple[dict[str, Any], bool]) -> None:
            config, dirty = result
            self.config_loaded.emit(config, dirty)
            self.status_message.emit("Конфигурация прочитана")

        self._submit(task, on_success, title="Чтение конфигурации")

    def write_config(self, patch: dict[str, Any]) -> None:
        """Записывает изменённые параметры в оперативную память устройства."""
        def task() -> None:
            self._require_device().write_config(patch)

        def on_success(_: Any) -> None:
            self.status_message.emit("Параметры записаны (не сохранены)")
            self.read_config()

        self._submit(task, on_success, title="Запись параметров")

    def save_config(self) -> None:
        """Сохраняет конфигурацию в энергонезависимую память контроллера."""
        def task() -> None:
            self._require_device().save_config()

        def on_success(_: Any) -> None:
            self.status_message.emit("Конфигурация сохранена в памяти устройства")
            self.read_config()

        self._submit(task, on_success, title="Сохранение конфигурации")

    def restore_defaults(self) -> None:
        """Возвращает заводские значения и сразу сохраняет их."""
        def task() -> None:
            self._require_device().restore_defaults(save=True)

        def on_success(_: Any) -> None:
            self.status_message.emit("Восстановлены значения по умолчанию")
            self.read_config()

        self._submit(task, on_success, title="Восстановление значений")

    # ------------------------------------------------------------------
    # Движение
    # ------------------------------------------------------------------

    def start_homing(self) -> None:
        def task() -> None:
            self._require_device().start_homing()

        self._submit(task, lambda _: self.status_message.emit("Homing запущен"),
                     title="Homing")

    def abort_homing(self) -> None:
        def task() -> None:
            self._require_device().abort_homing()

        self._submit(task, lambda _: self.status_message.emit("Homing прерван"),
                     title="Прерывание Homing")

    def move_to(self, position: int, speed: int | None = None) -> None:
        def task() -> int:
            return self._require_device().move_to(position, speed)

        self._submit(task,
                     lambda target: self.status_message.emit(f"Перемещение в {target}"),
                     title="Перемещение")

    def motor_run(self, direction: Direction, speed: int | None = None) -> None:
        def task() -> None:
            self._require_device().motor_run(direction, speed)

        label = "по часовой стрелке" if direction is Direction.CW else "против часовой стрелки"
        self._submit(task,
                     lambda _: self.status_message.emit(f"Вращение {label}"),
                     title="Вращение")

    def stop(self, *, emergency: bool = False) -> None:
        """Останавливает привод.

        Исполняется отдельным потоком и не ждёт очереди обычных команд: между
        нажатием кнопки и остановкой не должно стоять ничего.
        """
        device = self._device
        if device is None:
            return

        def task() -> None:
            device.stop(emergency=emergency)

        label = "Аварийный останов" if emergency else "Останов"
        self._submit(task,
                     lambda _: self.status_message.emit(f"{label} выполнен"),
                     title=label, urgent=True)

    def reset_emergency(self) -> None:
        """Снимает аварийный останов."""
        def task() -> None:
            self._require_device().reset()

        self._submit(
            task,
            lambda _: self.status_message.emit("Аварийный останов снят"),
            title="Снятие аварийного останова",
            urgent=True,
        )

    # ------------------------------------------------------------------
    # Завершение работы
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Останавливает привод и закрывает соединение при выходе из приложения.

        Оставлять привод вращающимся после закрытия окна недопустимо, поэтому
        останов выполняется синхронно, до завершения процесса.
        """
        device = self._release_device()

        if device is not None:
            try:
                if device.is_connected:
                    device.stop()
            except Exception:
                logger.debug("останов при выходе не удался", exc_info=True)
            finally:
                device.disconnect()

        self._executor.shutdown(wait=False, cancel_futures=True)
        self._urgent.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _release_device(self) -> ServoDevice | None:
        """Перестаёт владеть устройством и отвязывает его обработчики.

        Отвязать необходимо в любом пути отключения, а не только при выходе:
        поток чтения устройства живёт до закрытия канала, и если он сохранит
        ссылку на сигналы этого объекта, то обратится к ним уже после того, как
        Qt его уничтожит.
        """
        device, self._device = self._device, None
        self._transport = None
        if device is not None:
            device.clear_callbacks()
        return device

    def _require_device(self) -> ServoDevice:
        device = self._device
        if device is None:
            raise TransportError("устройство не подключено")
        return device

    def _submit(
        self,
        task: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
        *,
        title: str = "Команда",
        urgent: bool = False,
    ) -> None:
        """Ставит задачу в рабочий поток и раздаёт результат сигналами."""
        self._set_busy(+1)
        executor = self._urgent if urgent else self._executor

        def run() -> None:
            try:
                result = task()
            except Exception as exc:  # ошибка задачи не должна ронять поток
                self._finish_failure(exc, title, on_failure)
            else:
                self._finish_success(result, title, on_success)

        try:
            executor.submit(run)
        except RuntimeError:
            # Пул уже остановлен: приложение закрывается.
            self._set_busy(-1)

    def _finish_success(
        self, result: Any, title: str, on_success: Callable[[Any], None] | None
    ) -> None:
        self._set_busy(-1)
        if on_success is None:
            return
        try:
            on_success(result)
        except Exception:
            logger.exception("ошибка обработчика результата %s", title)

    def _finish_failure(
        self, error: Exception, title: str, on_failure: Callable[[Exception], None] | None
    ) -> None:
        self._set_busy(-1)
        logger.warning("%s: %s", title, error)

        if on_failure is not None:
            try:
                on_failure(error)
            except Exception:
                logger.exception("ошибка обработчика отказа %s", title)

        self.error_occurred.emit(title, _describe_exception(error))

    def _set_busy(self, delta: int) -> None:
        was_busy = self._pending > 0
        self._pending = max(0, self._pending + delta)
        if was_busy != (self._pending > 0):
            self.busy_changed.emit(self._pending > 0)

    # --- обработчики, вызываемые из потока чтения ---

    def _on_telemetry(self, frame: Telemetry) -> None:
        self.telemetry_received.emit(frame)

    def _on_event(self, notification: Notification) -> None:
        if notification.evt == Event.STATE:
            try:
                self.state_changed.emit(DeviceState(notification.data.get("state")))
            except ValueError:
                logger.warning("неизвестное состояние: %s", notification.data)
            return

        if notification.evt == Event.HOMING:
            data = notification.data
            self.homing_finished.emit(
                str(data.get("result", "error")),
                int(data.get("pos", 0)),
                int(data.get("elapsed_ms", 0)),
            )
            return

        if notification.evt == Event.ERROR:
            data = notification.data
            self.error_occurred.emit(
                "Устройство", f"{describe(data.get('err'))}: {data.get('msg', '')}"
            )
            return

        if notification.evt == Event.BOOT:
            # Контроллер перезагрузился: прежние сведения о состоянии устарели.
            self.status_message.emit("Устройство перезагрузилось, конфигурация перечитана")
            self.read_config()

    def _on_link_lost(self, code: str, message: str) -> None:
        self._release_device()
        self.disconnected.emit(f"{describe(code)}: {message}")


def _describe_exception(error: Exception) -> str:
    """Приводит исключение к понятному пользователю виду."""
    if isinstance(error, CommandError):
        return f"{describe(error.code)}: {error.detail or error.code}"
    if isinstance(error, CommandTimeout):
        return "Устройство не ответило в отведённое время"
    if isinstance(error, TransportError):
        return str(error)
    if isinstance(error, ValueError):
        return f"Некорректные параметры: {error}"
    return f"{type(error).__name__}: {error}"
