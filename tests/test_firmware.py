"""Тесты эмулируемой прошивки: конечный автомат, Homing, конфигурация, защиты.

Время в тестах задаётся явно, поэтому процедуры длиной в десятки секунд
проверяются мгновенно и без гонок.
"""

from __future__ import annotations

import json

import pytest

from servo_configurator.protocol import (
    CONFIG_VERSION,
    DeviceError,
    DeviceState,
    HomingResult,
    LinkError,
    default_config,
)
from servo_configurator.simulation import ServoModel, SimulatedFirmware, SimulatedNvs


class Harness:
    """Обёртка над прошивкой: отправка команд и продвижение времени."""

    def __init__(self, firmware: SimulatedFirmware) -> None:
        self.firmware = firmware
        self.events: list[dict] = []
        self._id = 0
        self._now = 0
        self._collect(firmware.boot(0))

    def send(self, cmd: str, **args) -> dict:
        """Отправляет команду и возвращает разобранный ответ."""
        self._id += 1
        message = {"id": self._id, "cmd": cmd}
        if args:
            message["args"] = args

        responses = self._collect(self.firmware.handle_line(json.dumps(message)))
        for item in responses:
            if item.get("id") == self._id:
                return item
        raise AssertionError(f"нет ответа на команду {cmd}")

    def run(self, duration_ms: int, step_ms: int = 20, *, keepalive: bool = True) -> None:
        """Прокручивает время.

        :param keepalive: имитировать периодические сообщения от ПК. Без них
            срабатывает watchdog связи — в реальной работе эту роль выполняет
            фоновый ``ping`` приложения.
        """
        for _ in range(duration_ms // step_ms):
            self._now += step_ms
            self._collect(self.firmware.tick(self._now))
            if keepalive:
                self.firmware._last_rx_ms = self._now

    def _collect(self, messages: list[str]) -> list[dict]:
        parsed = [json.loads(message) for message in messages]
        self.events.extend(item for item in parsed if "evt" in item)
        return parsed

    def events_named(self, name: str) -> list[dict]:
        return [item["data"] for item in self.events if item["evt"] == name]

    @property
    def state(self) -> DeviceState:
        return self.firmware.state


@pytest.fixture
def harness() -> Harness:
    """Прошивка с приводом в середине хода и хранилищем в памяти."""
    firmware = SimulatedFirmware(
        servo=ServoModel(position=1500.0),
        load_config=SimulatedNvs().load,
    )
    return Harness(firmware)


class TestBoot:
    def test_boot_event_is_emitted(self, harness):
        (boot,) = harness.events_named("boot")
        assert boot["proto"] == 1
        assert boot["cfg_loaded"] is False

    def test_starts_in_idle(self, harness):
        assert harness.state is DeviceState.IDLE

    def test_not_homed_after_boot(self, harness):
        assert harness.firmware.homed is False


class TestPing:
    def test_returns_device_info(self, harness):
        data = harness.send("ping")["data"]
        assert data["dev"] == "ws-servo-esp32"
        assert data["servo"] == "STS3215"
        assert data["proto"] == 1


class TestConfig:
    def test_defaults_are_returned(self, harness):
        config = harness.send("get_config")["data"]["config"]
        assert config == default_config()

    def test_partial_update_keeps_other_fields(self, harness):
        harness.send("set_config", config={"homing": {"speed": 400}})
        config = harness.send("get_config")["data"]["config"]
        assert config["homing"]["speed"] == 400
        assert config["homing"]["timeout_ms"] == default_config()["homing"]["timeout_ms"]

    def test_update_marks_config_dirty(self, harness):
        assert harness.send("set_config", config={"homing": {"speed": 400}})["data"]["dirty"]

    def test_save_clears_dirty_flag(self, harness):
        harness.send("set_config", config={"homing": {"speed": 400}})
        harness.send("save_config")
        assert harness.send("get_config")["data"]["dirty"] is False

    def test_invalid_value_is_rejected(self, harness):
        response = harness.send("set_config", config={"homing": {"speed": 99999}})
        assert response["err"] == DeviceError.RANGE

    def test_rejected_update_changes_nothing(self, harness):
        """Отказ обязан быть атомарным: ни одно поле патча не применяется."""
        before = harness.send("get_config")["data"]["config"]
        harness.send("set_config", config={"homing": {"speed": 400, "timeout_ms": 999999}})
        assert harness.send("get_config")["data"]["config"] == before

    def test_cross_field_violation_is_rejected(self, harness):
        response = harness.send("set_config", config={"operating": {"pos_min": 4000}})
        assert response["err"] == DeviceError.RANGE

    def test_restore_defaults(self, harness):
        harness.send("set_config", config={"homing": {"speed": 400}})
        config = harness.send("restore_defaults")["data"]["config"]
        assert config == default_config()

    def test_unknown_command_is_reported(self, harness):
        assert harness.send("fly_to_moon")["err"] == DeviceError.UNKNOWN_CMD

    def test_malformed_json_does_not_crash(self, harness):
        (event,) = [json.loads(m) for m in harness.firmware.handle_line("{broken")]
        assert event["data"]["err"] == DeviceError.PARSE


class TestConfigPersistence:
    def test_saved_config_survives_restart(self, tmp_path):
        """Пункт 17 сценария приёмки: конфигурация переживает перезагрузку."""
        nvs = SimulatedNvs(tmp_path / "nvs.json")

        first = Harness(SimulatedFirmware(load_config=nvs.load, save_config=nvs.save))
        first.send("set_config", config={"homing": {"speed": 777, "dir": "ccw"}})
        first.send("save_config")

        second = Harness(SimulatedFirmware(load_config=nvs.load, save_config=nvs.save))
        config = second.send("get_config")["data"]["config"]
        assert config["homing"]["speed"] == 777
        assert config["homing"]["dir"] == "ccw"
        assert second.events_named("boot")[0]["cfg_loaded"] is True

    def test_unsaved_changes_are_lost_on_restart(self, tmp_path):
        nvs = SimulatedNvs(tmp_path / "nvs.json")
        first = Harness(SimulatedFirmware(load_config=nvs.load, save_config=nvs.save))
        first.send("set_config", config={"homing": {"speed": 777}})  # без save_config

        second = Harness(SimulatedFirmware(load_config=nvs.load, save_config=nvs.save))
        assert second.send("get_config")["data"]["config"] == default_config()

    def test_config_with_foreign_version_is_ignored(self, tmp_path):
        path = tmp_path / "nvs.json"
        stored = default_config() | {"version": CONFIG_VERSION + 1}
        stored["homing"]["speed"] = 777
        path.write_text(json.dumps(stored), encoding="utf-8")

        nvs = SimulatedNvs(path)
        firmware = SimulatedFirmware(load_config=nvs.load, save_config=nvs.save)
        assert firmware.config == default_config()
        assert firmware.config_loaded is False

    def test_corrupted_storage_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "nvs.json"
        path.write_text("{not json", encoding="utf-8")

        firmware = SimulatedFirmware(load_config=SimulatedNvs(path).load)
        assert firmware.config == default_config()

    def test_storage_failure_reports_nvs_error(self, harness):
        def failing_save(config):
            raise OSError("нет места")

        harness.firmware._save_config = failing_save
        assert harness.send("save_config")["err"] == DeviceError.NVS


class TestHoming:
    def test_completes_at_mechanical_stop(self, harness):
        harness.send("home_start")
        assert harness.state is DeviceState.HOMING

        harness.run(12000)
        (result,) = harness.events_named("homing")
        assert result["result"] == HomingResult.COMPLETED
        assert harness.state is DeviceState.IDLE
        assert harness.firmware.homed is True

    def test_zero_position_is_assigned_to_the_stop(self, harness):
        harness.send("set_config", config={"homing": {"zero_position": 100}})
        harness.send("home_start")
        harness.run(12000)
        assert harness.firmware.servo.position == pytest.approx(100, abs=5)

    def test_does_not_finish_before_settle_time(self, harness):
        """Бросок момента при трогании не должен считаться упором."""
        harness.send("home_start")
        harness.run(200)
        assert harness.events_named("homing") == []

    def test_timeout_leads_to_fault(self, harness):
        # Упор недостижим: порог нагрузки выше того, что даёт модель в упоре.
        harness.send("set_config", config={
            "homing": {"load_threshold": 990, "timeout_ms": 2000},
            "operating": {"load_limit": 1000},
        })
        harness.send("home_start")
        harness.run(4000)

        (result,) = harness.events_named("homing")
        assert result["result"] == HomingResult.TIMEOUT
        assert harness.state is DeviceState.FAULT
        assert harness.firmware.homed is False

    def test_travel_limit_leads_to_fault(self, harness):
        harness.send("set_config", config={
            "homing": {"load_threshold": 990, "max_travel": 200, "timeout_ms": 60000},
            "operating": {"load_limit": 1000},
        })
        harness.send("home_start")
        harness.run(4000)

        (result,) = harness.events_named("homing")
        assert result["result"] == HomingResult.ERROR
        assert result["err"] == DeviceError.RANGE
        assert harness.state is DeviceState.FAULT

    def test_abort_stops_the_drive(self, harness):
        harness.send("home_start")
        harness.run(500)
        harness.send("home_abort")

        (result,) = harness.events_named("homing")
        assert result["result"] == HomingResult.ABORTED
        assert harness.state is DeviceState.IDLE
        assert harness.firmware.servo.velocity == 0.0

    def test_stop_during_homing_reports_result(self, harness):
        """Прерванный командой stop Homing обязан сообщить исход, иначе UI зависнет."""
        harness.send("home_start")
        harness.run(500)
        harness.send("stop")

        (result,) = harness.events_named("homing")
        assert result["result"] == HomingResult.ABORTED
        assert harness.state is DeviceState.IDLE

    def test_second_homing_is_rejected(self, harness):
        harness.send("home_start")
        assert harness.send("home_start")["err"] == DeviceError.STATE

    def test_abort_without_homing_is_rejected(self, harness):
        assert harness.send("home_abort")["err"] == DeviceError.STATE


class TestPositionMode:
    @pytest.fixture
    def homed(self, harness) -> Harness:
        harness.send("home_start")
        harness.run(12000)
        return harness

    def test_move_requires_homing(self, harness):
        assert harness.send("move_to", pos=2000)["err"] == DeviceError.NOT_HOMED

    def test_reaches_target(self, homed):
        homed.send("move_to", pos=2000)
        homed.run(6000)
        assert homed.firmware.servo.position == pytest.approx(2000, abs=15)
        assert homed.state is DeviceState.IDLE

    def test_stays_in_position_state_while_moving(self, homed):
        """Состояние не должно схлопнуться в idle до начала движения."""
        homed.send("move_to", pos=3000)
        homed.run(100)
        assert homed.state is DeviceState.POSITION

    def test_target_outside_range_is_rejected(self, homed):
        homed.send("set_config", config={"operating": {"pos_max": 2000}})
        assert homed.send("move_to", pos=3000)["err"] == DeviceError.RANGE

    def test_non_integer_target_is_rejected(self, homed):
        assert homed.send("move_to", pos="середина")["err"] == DeviceError.SCHEMA

    def test_stop_interrupts_movement(self, homed):
        homed.send("move_to", pos=3000)
        homed.run(300)
        homed.send("stop")
        position = homed.firmware.servo.position

        homed.run(1000)
        assert homed.firmware.servo.position == pytest.approx(position, abs=1)
        assert homed.state is DeviceState.IDLE


class TestMotorMode:
    def test_cw_decreases_position(self, harness):
        harness.send("motor_run", dir="cw", speed=800)
        start = harness.firmware.servo.position
        harness.run(500)
        assert harness.firmware.servo.position < start
        assert harness.state is DeviceState.MOTOR

    def test_ccw_increases_position(self, harness):
        harness.send("motor_run", dir="ccw", speed=800)
        start = harness.firmware.servo.position
        harness.run(500)
        assert harness.firmware.servo.position > start

    def test_unknown_direction_is_rejected(self, harness):
        assert harness.send("motor_run", dir="upwards")["err"] == DeviceError.SCHEMA

    def test_stop_halts_rotation(self, harness):
        harness.send("motor_run", dir="ccw", speed=800)
        harness.run(300)
        harness.send("stop")
        assert harness.firmware.servo.velocity == 0.0
        assert harness.state is DeviceState.IDLE

    def test_direction_can_be_changed_without_stop(self, harness):
        harness.send("motor_run", dir="cw", speed=500)
        harness.run(200)
        assert harness.send("motor_run", dir="ccw", speed=500)["ok"] is True

    def test_position_commands_are_rejected_while_rotating(self, harness):
        harness.send("motor_run", dir="cw", speed=500)
        assert harness.send("move_to", pos=1000)["err"] == DeviceError.STATE


class TestSafety:
    def test_overload_stops_the_drive(self, harness):
        """Упор при перемещении обязан приводить к остановке, а не к давлению в стену."""
        harness.send("home_start")
        harness.run(12000)
        harness.send("set_config", config={"operating": {"pos_max": 4095}})

        # Цель за верхним механическим упором модели.
        harness.send("move_to", pos=4000)
        harness.run(20000)

        assert harness.state is DeviceState.FAULT
        assert harness.firmware.error == DeviceError.SERVO_ERROR
        assert harness.firmware.servo.velocity == 0.0

    def test_brief_load_spike_does_not_trip(self, harness):
        harness.send("home_start")
        harness.run(12000)
        harness.send("move_to", pos=1000)
        harness.run(1000)
        assert harness.state in (DeviceState.POSITION, DeviceState.IDLE)

    def test_link_watchdog_stops_movement(self, harness):
        """Молчание ПК во время вращения останавливает привод."""
        harness.send("motor_run", dir="ccw", speed=500)
        harness.run(2000, keepalive=False)

        assert harness.state is DeviceState.FAULT
        assert harness.firmware.error == LinkError.LINK_TIMEOUT
        assert harness.firmware.servo.velocity == 0.0

    def test_keepalive_prevents_watchdog(self, harness):
        harness.send("motor_run", dir="ccw", speed=500)
        harness.run(3000, keepalive=True)
        assert harness.state is DeviceState.MOTOR

    def test_watchdog_can_be_disabled(self, harness):
        harness.send("set_config", config={"safety": {"link_timeout_ms": 0}})
        harness.send("motor_run", dir="ccw", speed=500)
        harness.run(3000, keepalive=False)
        assert harness.state is DeviceState.MOTOR

    def test_watchdog_is_idle_when_drive_is_stopped(self, harness):
        harness.run(5000, keepalive=False)
        assert harness.state is DeviceState.IDLE


class TestFaultState:
    @pytest.fixture
    def faulted(self, harness) -> Harness:
        harness.send("set_config", config={
            "homing": {"load_threshold": 990, "timeout_ms": 1000},
            "operating": {"load_limit": 1000},
        })
        harness.send("home_start")
        harness.run(3000)
        assert harness.state is DeviceState.FAULT
        return harness

    def test_movement_is_blocked(self, faulted):
        assert faulted.send("motor_run", dir="cw")["err"] == DeviceError.STATE

    @pytest.mark.parametrize("command", ["ping", "get_config", "telemetry"])
    def test_service_commands_still_work(self, faulted, command):
        assert faulted.send(command)["ok"] is True

    def test_stop_clears_the_fault(self, faulted):
        assert faulted.send("stop")["ok"] is True
        assert faulted.state is DeviceState.IDLE
        assert faulted.firmware.error is None

    def test_fault_does_not_clear_itself(self, faulted):
        faulted.run(5000)
        assert faulted.state is DeviceState.FAULT


class TestTelemetry:
    def test_disabled_by_default(self, harness):
        harness.run(1000)
        assert harness.events_named("tlm") == []

    def test_period_is_respected(self, harness):
        harness.send("telemetry", enabled=True, period_ms=100)
        harness.run(1000, step_ms=20)
        assert 8 <= len(harness.events_named("tlm")) <= 12

    def test_frame_contains_required_fields(self, harness):
        harness.send("telemetry", enabled=True, period_ms=50)
        harness.run(200)
        frame = harness.events_named("tlm")[0]
        assert {"seq", "ts", "pos", "spd", "load", "state", "homed"} <= frame.keys()

    def test_sequence_increments(self, harness):
        harness.send("telemetry", enabled=True, period_ms=50)
        harness.run(500)
        numbers = [frame["seq"] for frame in harness.events_named("tlm")]
        assert numbers == sorted(numbers)
        assert len(set(numbers)) == len(numbers)

    def test_can_be_disabled(self, harness):
        harness.send("telemetry", enabled=True, period_ms=50)
        harness.run(200)
        harness.send("telemetry", enabled=False)
        before = len(harness.events_named("tlm"))

        harness.run(500)
        assert len(harness.events_named("tlm")) == before

    def test_invalid_period_is_rejected(self, harness):
        assert harness.send("telemetry", enabled=True, period_ms=5)["err"] == DeviceError.RANGE


class TestStateEvents:
    def test_transition_reports_previous_state(self, harness):
        harness.send("home_start")
        (transition,) = harness.events_named("state")
        assert transition == {"state": "homing", "prev": "idle"}

    def test_no_event_without_change(self, harness):
        harness.send("stop")
        assert harness.events_named("state") == []
