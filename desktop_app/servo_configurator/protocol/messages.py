"""Типы сообщений протокола: команды, ответы, события, телеметрия.

Раздел 2 `docs/PROTOCOL.md`. Датаклассы здесь — граница между «сырым» JSON и
остальным приложением: выше по стеку код работает только с этими типами и никогда
не разбирает словари вручную.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Версия протокола, которую поддерживает это приложение (поле ``proto`` в ``ping``).
PROTOCOL_VERSION = 1

#: Максимальная длина кадра в байтах, включая завершающий перевод строки.
MAX_LINE_BYTES = 512


class Command(StrEnum):
    """Имена команд ПК → ESP32 (раздел 3 протокола)."""

    PING = "ping"
    GET_CONFIG = "get_config"
    SET_CONFIG = "set_config"
    SAVE_CONFIG = "save_config"
    RESTORE_DEFAULTS = "restore_defaults"
    TELEMETRY = "telemetry"
    HOME_START = "home_start"
    HOME_ABORT = "home_abort"
    MOVE_TO = "move_to"
    MOTOR_RUN = "motor_run"
    STOP = "stop"
    RESET = "reset"


#: Команды, безопасные для автоматического повтора после таймаута.
#:
#: Повторить чтение — значит в худшем случае получить те же данные дважды.
#: Повторить пуск движения — значит рискнуть вторым запуском привода, когда
#: первый, возможно, уже принят; поэтому ``move_to``, ``motor_run`` и
#: ``home_start`` сюда не входят (раздел 6 протокола).
#:
#: Команды остановки, наоборот, повторять обязательно. Их повторное исполнение
#: безвредно — привод уже стоит, — а вот потерянный ответ на ``stop`` оставил бы
#: пользователя с сообщением об ошибке ровно тогда, когда ему нужно остановить
#: привод. Из всех команд именно эта должна доходить надёжнее прочих.
IDEMPOTENT_COMMANDS: frozenset[Command] = frozenset({
    Command.PING,
    Command.GET_CONFIG,
    Command.TELEMETRY,
    Command.STOP,
    Command.HOME_ABORT,
})


class Event(StrEnum):
    """Имена асинхронных событий ESP32 → ПК."""

    TELEMETRY = "tlm"
    STATE = "state"
    HOMING = "homing"
    ERROR = "error"
    BOOT = "boot"


class DeviceState(StrEnum):
    """Состояния устройства (раздел 8 протокола)."""

    IDLE = "idle"
    HOMING = "homing"
    POSITION = "position"
    MOTOR = "motor"
    FAULT = "fault"
    ESTOP = "estop"
    """Аварийный останов: движение запрещено до явного снятия оператором."""


class HomingResult(StrEnum):
    """Исход процедуры Homing."""

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    ABORTED = "aborted"
    ERROR = "error"


class Direction(StrEnum):
    """Направление вращения."""

    CW = "cw"
    CCW = "ccw"


@dataclass(frozen=True, slots=True)
class Request:
    """Команда, отправляемая устройству."""

    id: int
    cmd: Command
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        message: dict[str, Any] = {"id": self.id, "cmd": str(self.cmd)}
        if self.args:
            message["args"] = self.args
        return message


@dataclass(frozen=True, slots=True)
class Response:
    """Ответ устройства на команду."""

    id: int
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    err: str | None = None
    msg: str = ""


@dataclass(frozen=True, slots=True)
class Notification:
    """Асинхронное событие устройства."""

    evt: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Разобранный кадр телеметрии (раздел 4 протокола).

    Необязательные поля (``volt``, ``temp``, ``cur``) остаются ``None``, если
    сервопривод их не вернул: протокол прямо разрешает такой кадр, и приложение
    обязано его пережить.
    """

    seq: int = 0
    ts: int = 0
    pos: int = 0
    spd: int = 0
    load: int = 0
    volt: int | None = None
    temp: int | None = None
    cur: int | None = None
    moving: bool = False
    state: DeviceState = DeviceState.IDLE
    homed: bool = False
    err: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Telemetry:
        """Собирает кадр, игнорируя недостающие и неизвестные поля.

        Приёмник намеренно снисходителен: единственное, что должно случиться при
        неожиданном значении, — это отбрасывание кадра выше по стеку, но не
        исключение внутри потока чтения.
        """
        def as_int(key: str, default: int = 0) -> int:
            value = data.get(key, default)
            return value if isinstance(value, int) and not isinstance(value, bool) else default

        def as_optional_int(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        raw_state = data.get("state")
        try:
            state = DeviceState(raw_state)
        except ValueError:
            state = DeviceState.FAULT

        err = data.get("err")
        return cls(
            seq=as_int("seq"),
            ts=as_int("ts"),
            pos=as_int("pos"),
            spd=as_int("spd"),
            load=as_int("load"),
            volt=as_optional_int("volt"),
            temp=as_optional_int("temp"),
            cur=as_optional_int("cur"),
            moving=bool(data.get("moving", False)),
            state=state,
            homed=bool(data.get("homed", False)),
            err=err if isinstance(err, str) and err else None,
        )
