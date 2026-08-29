"""Проверка согласованности прошивки ESP32 и приложения.

Прошивка и приложение — две независимые кодовые базы, связанные одним
протоколом. Ошибка здесь самая неприятная: код собирается, тесты Python
проходят, а на стенде команда молча отвергается или параметр получает не тот
диапазон. Тесты ниже разбирают исходники прошивки и сверяют их со схемой и
типами протокола.

Проверяется соответствие имён и чисел, а не поведения: поведение прошивки
проверяется на оборудовании (см. раздел «Известные ограничения» README).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

from servo_configurator.protocol import (
    CONFIG_VERSION,
    PARAMS,
    PARAMS_BY_PATH,
    PROTOCOL_VERSION,
    Command,
    DeviceError,
    DeviceState,
    Direction,
    EnumParam,
    Event,
    HomingResult,
    IntParam,
)

FIRMWARE_ROOT = Path(__file__).resolve().parent.parent / "esp32"


def read(*parts: str) -> str:
    path = FIRMWARE_ROOT.joinpath(*parts)
    if not path.exists():
        pytest.skip(f"нет исходника прошивки: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def controller_source() -> str:
    return read("src", "controller.cpp")


@pytest.fixture(scope="module")
def settings_source() -> str:
    return read("src", "settings.cpp")


@pytest.fixture(scope="module")
def settings_header() -> str:
    return read("include", "settings.h")


class TestCommands:
    @pytest.mark.parametrize("command", list(Command), ids=lambda c: c.value)
    def test_every_command_is_handled(self, controller_source, command):
        """Каждая команда протокола должна разбираться прошивкой."""
        assert f'strcmp(command, "{command.value}")' in controller_source

    def test_no_unknown_commands_are_handled(self, controller_source):
        """Прошивка не должна отвечать на команды, которых нет в протоколе."""
        handled = set(re.findall(r'strcmp\(command, "(\w+)"\)', controller_source))
        assert handled <= {c.value for c in Command}

    @pytest.mark.parametrize("command", ["ping", "get_config", "telemetry", "stop"])
    def test_service_commands_are_allowed_in_any_state(self, controller_source, command):
        """Команды из раздела 8 протокола обязаны работать и в состоянии fault."""
        guard = controller_source.split("bool Controller::stateAllows")[1].split("}")[0]
        assert f'strcmp(command, "{command}")' in guard


class TestStatesAndEvents:
    @pytest.mark.parametrize("state", list(DeviceState), ids=lambda s: s.value)
    def test_state_names_match(self, controller_source, state):
        assert f'return "{state.value}";' in controller_source

    @pytest.mark.parametrize("result", list(HomingResult), ids=lambda r: r.value)
    def test_homing_results_match(self, controller_source, result):
        assert f'data["result"] = "{result.value}"' in controller_source

    @pytest.mark.parametrize("event", list(Event), ids=lambda e: e.value)
    def test_events_are_emitted(self, controller_source, event):
        assert f'sendEvent("{event.value}"' in controller_source

    @pytest.mark.parametrize("direction", list(Direction), ids=lambda d: d.value)
    def test_directions_are_recognised(self, controller_source, direction):
        assert f'"{direction.value}"' in controller_source


class TestErrorCodes:
    #: Коды, которые прошивка обязана уметь возвращать. E_INTERNAL протоколом
    #: зарезервирован для непредвиденных сбоев и в рабочих путях не встречается,
    #: поэтому его отсутствие не считается ошибкой.
    REQUIRED: ClassVar[list[DeviceError]] = [
        code for code in DeviceError if code is not DeviceError.INTERNAL
    ]

    @pytest.mark.parametrize("code", REQUIRED, ids=lambda c: c.value)
    def test_device_error_codes_are_declared(self, controller_source, code):
        assert f'"{code.value}"' in controller_source

    def test_link_timeout_is_reported_by_watchdog(self, controller_source):
        assert "E_LINK_TIMEOUT" in controller_source


class TestVersions:
    def test_protocol_version_matches(self, controller_source):
        match = re.search(r"constexpr int PROTOCOL_VERSION = (\d+)", controller_source)
        assert match and int(match.group(1)) == PROTOCOL_VERSION

    def test_config_version_matches(self, settings_header):
        match = re.search(r"CONFIG_VERSION = (\d+)", settings_header)
        assert match and int(match.group(1)) == CONFIG_VERSION


class TestParameterRanges:
    """Диапазоны параметров обязаны совпадать в приложении и прошивке.

    Расхождение приводит к тому, что интерфейс позволяет ввести значение,
    которое устройство отвергнет, — или наоборот, устройство примет значение,
    выходящее за проверенные пределы.
    """

    @staticmethod
    def firmware_ranges(source: str) -> dict[str, tuple[int, int]]:
        pattern = re.compile(
            r'inRange<long>\(\s*[\w.]+\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*"([\w.]+)"'
        )
        return {
            name: (int(minimum), int(maximum))
            for minimum, maximum, name in pattern.findall(source)
        }

    def test_all_integer_parameters_are_validated(self, settings_source):
        ranges = self.firmware_ranges(settings_source)
        expected = {p.path for p in PARAMS if isinstance(p, IntParam)}
        assert expected - set(ranges) == set(), "прошивка не проверяет часть параметров"

    @pytest.mark.parametrize(
        "param",
        [p for p in PARAMS if isinstance(p, IntParam)],
        ids=lambda p: p.path,
    )
    def test_range_matches_schema(self, settings_source, param):
        ranges = self.firmware_ranges(settings_source)
        assert ranges[param.path] == (param.minimum, param.maximum)

    def test_enum_choices_match(self, settings_source):
        enum_params = [p for p in PARAMS if isinstance(p, EnumParam)]
        for param in enum_params:
            for choice in param.choices:
                assert f'"{choice}"' in settings_source


class TestParameterDefaults:
    """Значения по умолчанию должны совпадать: иначе `restore_defaults`
    вернёт не то, что приложение показывает как заводские значения."""

    @staticmethod
    def firmware_defaults(header: str) -> dict[str, int]:
        # Поля структур вида `uint16_t speed = 300;` внутри именованных блоков.
        defaults: dict[str, int] = {}
        for struct_name, section in [
            ("HomingSettings", "homing"),
            ("OperatingSettings", "operating"),
            ("SafetySettings", "safety"),
        ]:
            match = re.search(rf"struct {struct_name} \{{(.*?)\}};", header, re.S)
            if match is None:
                continue
            for field, value in re.findall(r"(\w+)\s*=\s*(\d+);", match.group(1)):
                defaults[f"{section}.{field}"] = int(value)
        return defaults

    #: Имена полей прошивки отличаются стилем от путей схемы.
    FIELD_TO_PATH: ClassVar[dict[str, str]] = {
        "homing.speed": "homing.speed",
        "homing.loadThreshold": "homing.load_threshold",
        "homing.timeoutMs": "homing.timeout_ms",
        "homing.maxTravel": "homing.max_travel",
        "homing.zeroPosition": "homing.zero_position",
        "operating.speed": "operating.speed",
        "operating.loadLimit": "operating.load_limit",
        "operating.posMin": "operating.pos_min",
        "operating.posMax": "operating.pos_max",
        "operating.acceleration": "operating.accel",
        "safety.linkTimeoutMs": "safety.link_timeout_ms",
    }

    def test_defaults_match_schema(self, settings_header):
        defaults = self.firmware_defaults(settings_header)
        mismatched = []

        for field, path in self.FIELD_TO_PATH.items():
            if field not in defaults:
                mismatched.append(f"{field}: нет в прошивке")
                continue
            expected = PARAMS_BY_PATH[path].default
            if defaults[field] != expected:
                mismatched.append(f"{path}: прошивка {defaults[field]}, схема {expected}")

        assert not mismatched, "; ".join(mismatched)

    def test_default_homing_direction_matches(self, settings_header):
        match = re.search(r"HomingDirection direction = HomingDirection::(\w+)", settings_header)
        assert match is not None
        assert match.group(1).lower() == PARAMS_BY_PATH["homing.dir"].default

    def test_default_servo_id_matches(self, settings_header):
        match = re.search(r"uint8_t servoId = (\d+)", settings_header)
        assert match and int(match.group(1)) == PARAMS_BY_PATH["servo.id"].default


class TestSafetyInvariants:
    """Меры безопасности, которые обязаны присутствовать в прошивке."""

    def test_link_watchdog_exists(self, controller_source):
        assert "checkLinkWatchdog" in controller_source

    def test_overload_protection_exists(self, controller_source):
        assert "checkOverload" in controller_source
        assert "loadLimit" in controller_source

    def test_homing_settle_delay_exists(self, controller_source):
        """Бросок момента при трогании не должен приниматься за упор."""
        assert "HOMING_SETTLE_MS" in controller_source

    def test_homing_has_timeout_and_travel_limit(self, controller_source):
        assert "timeoutMs" in controller_source
        assert "maxTravel" in controller_source

    def test_position_reached_checks_distance(self, controller_source):
        """Проверка достижения цели не должна опираться только на флаг движения."""
        body = controller_source.split("void Controller::checkPositionReached")[1]
        assert "POSITION_REACHED_TOLERANCE" in body.split("}")[0] + body[:600]

    def test_stop_is_performed_on_every_homing_outcome(self, controller_source):
        body = controller_source.split("void Controller::finishHoming")[1][:800]
        assert "stopMotion" in body

    @pytest.mark.parametrize(
        "handler",
        ["Controller::commandMoveTo", "Controller::commandMotorRun", "Controller::startHoming"],
    )
    def test_torque_is_restored_before_motion(self, controller_source, handler):
        """Аварийный останов снимает момент; пуск движения обязан его вернуть.

        Иначе привод примет команду, но останется свободным и не тронется —
        расхождение, которое проявится только на оборудовании.
        """
        # Границей служит начало следующей функции: обработчик перемещения
        # длинный из-за проверок аргументов, и фиксированное окно его обрезало бы.
        body = controller_source.split(f"void {handler}")[1]
        body = body.split("\nvoid Controller::")[0]
        assert "setTorque" in body, f"{handler} не возвращает момент"

    def test_line_length_limit_matches_protocol(self):
        source = read("src", "main.cpp")
        match = re.search(r"MAX_LINE_LENGTH = (\d+)", source)
        from servo_configurator.protocol import MAX_LINE_BYTES

        assert match and int(match.group(1)) == MAX_LINE_BYTES


@pytest.fixture(scope="module")
def bus_header() -> str:
    return read("include", "servo_bus.h")


class TestBusDriver:
    """Проверки драйвера шины, критичные для ST-серии."""

    def test_little_endian_word_order(self, bus_header):
        """У ST-серии младший байт идёт первым; порядок SC-серии обратный."""
        body = bus_header.split("static uint16_t toWord")[1][:300]
        assert "data[0]" in body and "data[1]) << 8" in body

    def test_sign_is_taken_from_bit15(self, bus_header):
        """Скорость и нагрузка передаются модулем со знаком в старшем бите."""
        assert "0x8000" in bus_header and "0x7FFF" in bus_header

    def test_feedback_block_is_read_at_once(self, bus_header):
        assert "FEEDBACK_LENGTH" in bus_header

    def test_baudrate_matches_documentation(self, bus_header):
        source = read("include", "board_config.h")
        assert "SERVO_BAUDRATE = 1000000" in source
        assert "HOST_BAUDRATE = 115200" in source

    def test_servo_uart_pins_match_documentation(self):
        source = read("include", "board_config.h")
        assert "SERVO_RX_PIN = 18" in source
        assert "SERVO_TX_PIN = 19" in source
