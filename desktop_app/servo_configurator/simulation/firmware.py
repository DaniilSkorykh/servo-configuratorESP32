"""Эмуляция прошивки ESP32: разбор команд, конечный автомат, Homing, телеметрия.

Модуль реализует ту же логику, что и прошивка в `esp32/`, и говорит тем же
протоколом. Это осознанный приём, а не временная заглушка:

* приложение целиком отлаживается без стенда, и переключение Real / Demo
  не затрагивает ни одного слоя выше транспорта;
* алгоритм Homing и переходы состояний проверяются обычными тестами, где время
  задаётся явно, а не измеряется секундомером у стола с приводом;
* прошивка на C++ пишется как перенос уже проверенного поведения, что заметно
  сокращает отладку на реальном оборудовании.

Соответствие спецификации — `docs/PROTOCOL.md`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ..protocol import (
    CONFIG_VERSION,
    PROTOCOL_VERSION,
    Command,
    DeviceError,
    DeviceState,
    Direction,
    Event,
    HomingResult,
    LinkError,
    default_config,
    merge_config,
    validate_config,
)
from .servo_model import ServoModel

logger = logging.getLogger(__name__)

FIRMWARE_VERSION = "1.0.0-sim"
DEVICE_NAME = "ws-servo-esp32"
SERVO_NAME = "STS3215"

#: Направление CW соответствует убыванию счётчика позиции, CCW — возрастанию.
#: Соглашение зафиксировано здесь и в README: на стенде его следует подтвердить,
#: поскольку знак зависит от монтажа привода.
_DIRECTION_SIGN = {Direction.CW: -1.0, Direction.CCW: +1.0}

#: Пауза после старта Homing, в течение которой нагрузка не анализируется.
#: При трогании привод даёт кратковременный бросок момента, и без этой задержки
#: процедура завершалась бы «упором» немедленно после запуска.
_HOMING_SETTLE_MS = 300

#: Допуск, при котором целевая позиция считается достигнутой, шаг.
_POSITION_REACHED_TOLERANCE = 10.0

#: Время, в течение которого нагрузка должна превышать предел до остановки, мс.
_OVERLOAD_HOLD_MS = 300

#: Диапазон допустимых периодов телеметрии, мс (раздел 3.1 протокола).
_TELEMETRY_PERIOD_MIN = 20
_TELEMETRY_PERIOD_MAX = 1000
_TELEMETRY_PERIOD_DEFAULT = 50


class SimulatedFirmware:
    """Логика прошивки поверх модели привода.

    Время подаётся снаружи (``now_ms``), а не берётся из системных часов:
    так поведение воспроизводимо в тестах, где Homing длиной в десять секунд
    проигрывается мгновенно.
    """

    def __init__(
        self,
        servo: ServoModel | None = None,
        load_config: Callable[[], dict[str, Any] | None] | None = None,
        save_config: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.servo = servo or ServoModel()
        self._load_config = load_config or (lambda: None)
        self._save_config = save_config or (lambda config: None)

        self.config = default_config()
        self.config_loaded = False
        self.dirty = False

        self.state = DeviceState.IDLE
        self.homed = False
        self.error: str | None = None

        self.telemetry_enabled = False
        self.telemetry_period_ms = _TELEMETRY_PERIOD_DEFAULT

        self._now_ms = 0
        self._last_update_ms = 0
        self._last_telemetry_ms = 0
        self._last_rx_ms = 0
        self._seq = 0

        self._homing_started_ms = 0
        self._homing_start_position = 0.0
        self._target_position = 0.0
        self._overload_since_ms: int | None = None

        self._restore_config()

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def _restore_config(self) -> None:
        """Читает конфигурацию из хранилища, отбраковывая несовместимую."""
        stored = self._load_config()
        if stored is None:
            return

        if stored.get("version") != CONFIG_VERSION:
            logger.info("версия конфигурации в NVS не совпадает, взяты значения по умолчанию")
            return

        candidate = merge_config(default_config(), stored)
        if validate_config(candidate):
            logger.warning("конфигурация в NVS не прошла проверку, взяты значения по умолчанию")
            return

        self.config = candidate
        self.config_loaded = True

    def boot(self, now_ms: int = 0) -> list[str]:
        """Возвращает событие ``boot``, которым прошивка объявляет о старте."""
        self._now_ms = now_ms
        self._last_update_ms = now_ms
        self._last_rx_ms = now_ms
        return [_event(Event.BOOT, {
            "fw": FIRMWARE_VERSION,
            "proto": PROTOCOL_VERSION,
            "cfg_loaded": self.config_loaded,
        })]

    # ------------------------------------------------------------------
    # Периодическая работа
    # ------------------------------------------------------------------

    def tick(self, now_ms: int) -> list[str]:
        """Продвигает время: физика, Homing, телеметрия, watchdog связи."""
        if now_ms < self._now_ms:
            now_ms = self._now_ms
        self._now_ms = now_ms

        dt = (now_ms - self._last_update_ms) / 1000.0
        self._last_update_ms = now_ms
        self.servo.update(dt)

        messages: list[str] = []
        messages.extend(self._advance_homing())
        messages.extend(self._check_position_reached())
        messages.extend(self._check_overload())
        messages.extend(self._check_link_watchdog())
        messages.extend(self._emit_telemetry())
        return messages

    def _check_position_reached(self) -> list[str]:
        """Переводит устройство в ``idle``, когда целевая позиция достигнута.

        Проверяется именно расстояние до цели, а не только флаг движения: сразу
        после команды привод ещё стоит, и проверка по одному лишь ``moving``
        объявила бы перемещение завершённым, не начав его.
        """
        if self.state is not DeviceState.POSITION:
            return []
        if abs(self.servo.position - self._target_position) > _POSITION_REACHED_TOLERANCE:
            return []
        if self.servo.feedback()["moving"]:
            return []
        return self._change_state(DeviceState.IDLE)

    def _check_overload(self) -> list[str]:
        """Останавливает привод при затяжном превышении заданной нагрузки.

        Ограничение ``operating.load_limit`` имеет смысл только если по нему
        действительно останавливают движение. Типичная ситуация — команда
        перемещения в точку, до которой привод не доходит из-за препятствия:
        без этой проверки он давил бы в упор до перегрева.

        Кратковременные всплески игнорируются: превышение должно продержаться
        :data:`_OVERLOAD_HOLD_MS`, иначе бросок момента при трогании
        останавливал бы любое движение.
        """
        if self.state not in (DeviceState.POSITION, DeviceState.MOTOR):
            self._overload_since_ms = None
            return []

        limit = self.config["operating"]["load_limit"]
        if self.servo.measured_load < limit:
            self._overload_since_ms = None
            return []

        if self._overload_since_ms is None:
            self._overload_since_ms = self._now_ms
            return []

        if self._now_ms - self._overload_since_ms < _OVERLOAD_HOLD_MS:
            return []

        logger.warning("превышение нагрузки: движение остановлено")
        self.servo.stop()
        self._overload_since_ms = None
        self.error = DeviceError.SERVO_ERROR
        messages = self._change_state(DeviceState.FAULT)
        messages.append(_event(Event.ERROR, {
            "err": DeviceError.SERVO_ERROR,
            "msg": f"нагрузка {limit} превышена — вероятно, достигнут механический упор",
        }))
        return messages

    def _check_link_watchdog(self) -> list[str]:
        """Останавливает привод, если ПК замолчал во время движения.

        Защищает от выдёргивания USB при непрерывном вращении: без этого привод
        продолжал бы крутиться, а приложение уже не смогло бы его остановить.
        """
        timeout = self.config["safety"]["link_timeout_ms"]
        if timeout <= 0 or self.state not in (DeviceState.MOTOR, DeviceState.POSITION):
            return []
        if self._now_ms - self._last_rx_ms <= timeout:
            return []

        logger.warning("watchdog связи: движение остановлено")
        self.servo.stop()
        self.error = LinkError.LINK_TIMEOUT
        messages = self._change_state(DeviceState.FAULT)
        messages.append(_event(Event.ERROR, {
            "err": LinkError.LINK_TIMEOUT,
            "msg": "нет связи с ПК, привод остановлен",
        }))
        return messages

    def _emit_telemetry(self) -> list[str]:
        if not self.telemetry_enabled:
            return []
        if self._now_ms - self._last_telemetry_ms < self.telemetry_period_ms:
            return []
        self._last_telemetry_ms = self._now_ms
        return [_event(Event.TELEMETRY, self._telemetry_payload())]

    def _telemetry_payload(self) -> dict[str, Any]:
        self._seq = (self._seq + 1) & 0xFFFF
        payload: dict[str, Any] = {"seq": self._seq, "ts": self._now_ms}
        payload.update(self.servo.feedback())
        payload["state"] = str(self.state)
        payload["homed"] = self.homed
        payload["err"] = self.error
        return payload

    # ------------------------------------------------------------------
    # Homing
    # ------------------------------------------------------------------

    def _start_homing(self) -> None:
        homing = self.config["homing"]
        self._homing_started_ms = self._now_ms
        self._homing_start_position = self.servo.position
        self.homed = False
        sign = _DIRECTION_SIGN[Direction(homing["dir"])]
        self.servo.run(sign * homing["speed"])

    def _advance_homing(self) -> list[str]:
        """Один шаг процедуры поиска упора.

        Порядок проверок задаёт приоритет причин остановки: сначала аварийные
        (путь, время), затем штатное завершение по нагрузке. Так превышение
        лимитов не может быть замаскировано случайным всплеском нагрузки.
        """
        if self.state is not DeviceState.HOMING:
            return []

        homing = self.config["homing"]
        elapsed = self._now_ms - self._homing_started_ms
        travelled = abs(self.servo.position - self._homing_start_position)

        if travelled > homing["max_travel"]:
            return self._finish_homing(
                HomingResult.ERROR, DeviceError.RANGE,
                f"пройдено {int(travelled)} шаг при пределе {homing['max_travel']}",
            )

        if elapsed > homing["timeout_ms"]:
            return self._finish_homing(
                HomingResult.TIMEOUT, DeviceError.TIMEOUT,
                f"упор не найден за {homing['timeout_ms']} мс",
            )

        if elapsed < _HOMING_SETTLE_MS:
            return []

        if self.servo.measured_load >= homing["load_threshold"]:
            self.servo.stop()
            self.servo.set_position_counter(homing["zero_position"])
            self.homed = True
            return self._finish_homing(HomingResult.COMPLETED)

        return []

    def _finish_homing(
        self,
        result: HomingResult,
        error: str | None = None,
        message: str = "",
    ) -> list[str]:
        """Завершает Homing, всегда останавливая привод."""
        self.servo.stop()
        elapsed = self._now_ms - self._homing_started_ms

        messages = [_event(Event.HOMING, {
            "result": str(result),
            "pos": round(self.servo.position),
            "elapsed_ms": elapsed,
            **({"err": error, "msg": message} if error else {}),
        })]

        if result is HomingResult.COMPLETED:
            self.error = None
            messages.extend(self._change_state(DeviceState.IDLE))
        elif result is HomingResult.ABORTED:
            messages.extend(self._change_state(DeviceState.IDLE))
        else:
            self.error = error
            messages.extend(self._change_state(DeviceState.FAULT))

        return messages

    # ------------------------------------------------------------------
    # Приём сообщений
    # ------------------------------------------------------------------

    def handle_line(self, line: str) -> list[str]:
        """Разбирает и исполняет одну команду, возвращая сообщения в ответ."""
        self._last_rx_ms = self._now_ms

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return [_event(Event.ERROR, {"err": DeviceError.PARSE,
                                         "msg": "сообщение не разобрано"})]

        if not isinstance(message, dict):
            return [_event(Event.ERROR, {"err": DeviceError.SCHEMA,
                                         "msg": "ожидался объект"})]

        message_id = message.get("id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return [_event(Event.ERROR, {"err": DeviceError.SCHEMA,
                                         "msg": "отсутствует или некорректен id"})]

        name = message.get("cmd")
        args = message.get("args", {})
        if not isinstance(args, dict):
            return [_failure(message_id, DeviceError.SCHEMA, "args должен быть объектом")]

        try:
            command = Command(name)
        except ValueError:
            return [_failure(message_id, DeviceError.UNKNOWN_CMD, f"неизвестная команда {name!r}")]

        if (denial := self._check_state_allows(command)) is not None:
            return [_failure(message_id, DeviceError.STATE, denial)]

        handler = getattr(self, f"_cmd_{command.value}")
        return handler(message_id, args)

    def _check_state_allows(self, command: Command) -> str | None:
        """Проверяет допустимость команды в текущем состоянии (раздел 8)."""
        if command in _ALWAYS_ALLOWED:
            return None

        if self.state is DeviceState.FAULT:
            return f"устройство в состоянии fault, требуется stop (ошибка: {self.error})"

        if self.state is DeviceState.HOMING and command is not Command.HOME_ABORT:
            return "выполняется Homing"

        if self.state is DeviceState.MOTOR and command not in (Command.MOTOR_RUN,):
            return "выполняется непрерывное вращение, требуется stop"

        if command is Command.HOME_START and self.state is not DeviceState.IDLE:
            return f"Homing допустим только из idle, текущее состояние {self.state}"

        return None

    # ------------------------------------------------------------------
    # Обработчики команд
    # ------------------------------------------------------------------

    def _cmd_ping(self, message_id: int, args: dict[str, Any]) -> list[str]:
        return [_success(message_id, {
            "fw": FIRMWARE_VERSION,
            "proto": PROTOCOL_VERSION,
            "dev": DEVICE_NAME,
            "servo": SERVO_NAME,
            "servo_id": self.config["servo"]["id"],
            "uptime_ms": self._now_ms,
        })]

    def _cmd_get_config(self, message_id: int, args: dict[str, Any]) -> list[str]:
        return [_success(message_id, {"config": self.config, "dirty": self.dirty})]

    def _cmd_set_config(self, message_id: int, args: dict[str, Any]) -> list[str]:
        patch = args.get("config")
        if not isinstance(patch, dict):
            return [_failure(message_id, DeviceError.SCHEMA, "ожидался объект config")]

        # Патч сливается с текущей конфигурацией и проверяется целиком: только так
        # видны нарушения межполевых ограничений, затрагивающих неизменённые поля.
        candidate = merge_config(self.config, patch)
        candidate["version"] = CONFIG_VERSION
        if errors := validate_config(candidate):
            return [_failure(message_id, DeviceError.RANGE, "; ".join(errors))]

        self.config = candidate
        self.dirty = True
        return [_success(message_id, {"config": self.config, "dirty": True})]

    def _cmd_save_config(self, message_id: int, args: dict[str, Any]) -> list[str]:
        try:
            self._save_config(self.config)
        except OSError as exc:
            return [_failure(message_id, DeviceError.NVS, str(exc))]
        self.dirty = False
        return [_success(message_id, {"dirty": False})]

    def _cmd_restore_defaults(self, message_id: int, args: dict[str, Any]) -> list[str]:
        self.config = default_config()
        self.dirty = True

        if args.get("save"):
            try:
                self._save_config(self.config)
            except OSError as exc:
                return [_failure(message_id, DeviceError.NVS, str(exc))]
            self.dirty = False

        return [_success(message_id, {"config": self.config, "dirty": self.dirty})]

    def _cmd_telemetry(self, message_id: int, args: dict[str, Any]) -> list[str]:
        enabled = args.get("enabled", True)
        if not isinstance(enabled, bool):
            return [_failure(message_id, DeviceError.SCHEMA, "enabled должен быть булевым")]

        period = args.get("period_ms", self.telemetry_period_ms)
        if not isinstance(period, int) or isinstance(period, bool):
            return [_failure(message_id, DeviceError.SCHEMA, "period_ms должен быть целым")]
        if not _TELEMETRY_PERIOD_MIN <= period <= _TELEMETRY_PERIOD_MAX:
            return [_failure(
                message_id, DeviceError.RANGE,
                f"period_ms {period} вне диапазона "
                f"[{_TELEMETRY_PERIOD_MIN}, {_TELEMETRY_PERIOD_MAX}]",
            )]

        self.telemetry_enabled = enabled
        self.telemetry_period_ms = period
        return [_success(message_id, {"enabled": enabled, "period_ms": period})]

    def _cmd_home_start(self, message_id: int, args: dict[str, Any]) -> list[str]:
        self._start_homing()
        messages = [_success(message_id, {"state": str(DeviceState.HOMING)})]
        messages.extend(self._change_state(DeviceState.HOMING))
        return messages

    def _cmd_home_abort(self, message_id: int, args: dict[str, Any]) -> list[str]:
        if self.state is not DeviceState.HOMING:
            return [_failure(message_id, DeviceError.STATE, "Homing не выполняется")]
        messages = [_success(message_id, {"state": str(DeviceState.IDLE)})]
        messages.extend(self._finish_homing(HomingResult.ABORTED))
        return messages

    def _cmd_move_to(self, message_id: int, args: dict[str, Any]) -> list[str]:
        if not self.homed:
            return [_failure(message_id, DeviceError.NOT_HOMED,
                             "перед позиционированием требуется Homing")]

        position = args.get("pos")
        if not isinstance(position, int) or isinstance(position, bool):
            return [_failure(message_id, DeviceError.SCHEMA, "pos должен быть целым")]

        operating = self.config["operating"]
        if not operating["pos_min"] <= position <= operating["pos_max"]:
            return [_failure(
                message_id, DeviceError.RANGE,
                f"pos {position} вне диапазона "
                f"[{operating['pos_min']}, {operating['pos_max']}]",
            )]

        speed = args.get("speed", operating["speed"])
        if not isinstance(speed, int) or isinstance(speed, bool) or speed <= 0:
            return [_failure(message_id, DeviceError.RANGE, "speed должен быть положительным")]

        self.servo.move_to(position, speed)
        self._target_position = float(position)
        self._overload_since_ms = None
        messages = [_success(message_id, {"target": position})]
        messages.extend(self._change_state(DeviceState.POSITION))
        return messages

    def _cmd_motor_run(self, message_id: int, args: dict[str, Any]) -> list[str]:
        raw_direction = args.get("dir")
        try:
            direction = Direction(raw_direction)
        except ValueError:
            return [_failure(message_id, DeviceError.SCHEMA,
                             f"dir должен быть cw или ccw, получено {raw_direction!r}")]

        speed = args.get("speed", self.config["operating"]["speed"])
        if not isinstance(speed, int) or isinstance(speed, bool) or speed <= 0:
            return [_failure(message_id, DeviceError.RANGE, "speed должен быть положительным")]

        self.servo.run(_DIRECTION_SIGN[direction] * speed)
        messages = [_success(message_id, {"dir": str(direction), "speed": speed})]
        messages.extend(self._change_state(DeviceState.MOTOR))
        return messages

    def _cmd_stop(self, message_id: int, args: dict[str, Any]) -> list[str]:
        emergency = bool(args.get("emergency", False))
        self.servo.stop(release_torque=emergency)

        was_homing = self.state is DeviceState.HOMING
        self.error = None
        messages = [_success(message_id, {"state": str(DeviceState.IDLE)})]

        if was_homing:
            # Прерванный командой stop Homing обязан сообщить свой исход: иначе UI
            # остался бы с индикацией «выполняется» без завершающего события.
            messages.extend(self._finish_homing(HomingResult.ABORTED))
        else:
            messages.extend(self._change_state(DeviceState.IDLE))

        return messages

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------

    def _change_state(self, new_state: DeviceState) -> list[str]:
        if new_state is self.state:
            return []
        previous, self.state = self.state, new_state
        return [_event(Event.STATE, {"state": str(new_state), "prev": str(previous)})]


#: Команды, разрешённые в любом состоянии, включая fault (раздел 8 протокола).
_ALWAYS_ALLOWED = frozenset({
    Command.PING,
    Command.GET_CONFIG,
    Command.TELEMETRY,
    Command.STOP,
})


def _dump(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _success(message_id: int, data: dict[str, Any] | None = None) -> str:
    return _dump({"id": message_id, "ok": True, "data": data or {}})


def _failure(message_id: int, code: str, message: str = "") -> str:
    payload: dict[str, Any] = {"id": message_id, "ok": False, "err": str(code)}
    if message:
        payload["msg"] = message
    return _dump(payload)


def _event(name: str, data: dict[str, Any]) -> str:
    return _dump({"evt": str(name), "data": data})
