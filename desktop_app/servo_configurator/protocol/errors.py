"""Коды ошибок протокола и исключения транспортного уровня.

Раздел 5 `docs/PROTOCOL.md`. Коды разделены на две группы:

* :class:`DeviceError` — приходят от ESP32 в поле ``err`` ответа;
* :class:`LinkError` — возникают на стороне ПК и в линию не попадают.

Разделение важно для UI: ошибка устройства означает, что связь исправна и привод
отверг команду, а ошибка связи — что состояние привода приложению неизвестно и
безопасным считается только его остановка.
"""

from __future__ import annotations

from enum import StrEnum


class DeviceError(StrEnum):
    """Коды ошибок, которые возвращает ESP32."""

    PARSE = "E_PARSE"
    SCHEMA = "E_SCHEMA"
    UNKNOWN_CMD = "E_UNKNOWN_CMD"
    RANGE = "E_RANGE"
    STATE = "E_STATE"
    NOT_HOMED = "E_NOT_HOMED"
    SERVO_TIMEOUT = "E_SERVO_TIMEOUT"
    SERVO_ERROR = "E_SERVO_ERROR"
    TIMEOUT = "E_TIMEOUT"
    NVS = "E_NVS"
    INTERNAL = "E_INTERNAL"


class LinkError(StrEnum):
    """Коды ошибок связи, определяемые приложением."""

    PORT = "E_PORT"
    """Порт не открывается: занят другим приложением, нет прав, не существует."""

    LINK_TIMEOUT = "E_LINK_TIMEOUT"
    """Устройство не ответило на команду в отведённое время."""

    DISCONNECTED = "E_DISCONNECTED"
    """Связь потеряна: USB отключён либо поток телеметрии прервался."""


#: Человекочитаемые описания для строки состояния и логов.
ERROR_MESSAGES: dict[str, str] = {
    DeviceError.PARSE: "Устройство не разобрало сообщение",
    DeviceError.SCHEMA: "Устройство отвергло структуру сообщения",
    DeviceError.UNKNOWN_CMD: "Устройство не знает такой команды",
    DeviceError.RANGE: "Значение вне допустимого диапазона",
    DeviceError.STATE: "Команда недопустима в текущем состоянии",
    DeviceError.NOT_HOMED: "Требуется выполнить Homing",
    DeviceError.SERVO_TIMEOUT: "Сервопривод не отвечает",
    DeviceError.SERVO_ERROR: "Сервопривод сообщил об ошибке",
    DeviceError.TIMEOUT: "Превышено время выполнения операции",
    DeviceError.NVS: "Ошибка сохранения конфигурации в памяти устройства",
    DeviceError.INTERNAL: "Внутренняя ошибка прошивки",
    LinkError.PORT: "Не удалось открыть порт",
    LinkError.LINK_TIMEOUT: "Устройство не ответило",
    LinkError.DISCONNECTED: "Связь с устройством потеряна",
}


def describe(code: str | None) -> str:
    """Возвращает описание кода ошибки; неизвестный код отдаётся как есть."""
    if not code:
        return ""
    return ERROR_MESSAGES.get(code, code)


class ProtocolError(Exception):
    """Сообщение не соответствует протоколу и не может быть разобрано."""


class TransportError(Exception):
    """Отказ транспортного уровня: порт не открылся, чтение или запись не удались."""

    def __init__(self, message: str, code: LinkError = LinkError.PORT) -> None:
        super().__init__(message)
        self.code = code


class CommandError(Exception):
    """Устройство ответило отказом на команду."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or describe(code))
        self.code = code
        self.detail = message


class CommandTimeout(Exception):
    """Устройство не ответило на команду в отведённое время."""

    code = LinkError.LINK_TIMEOUT
