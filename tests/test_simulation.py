"""Тесты модели привода и транспорта-симулятора."""

from __future__ import annotations

import json

import pytest

from servo_configurator.protocol import LineAssembler, TransportError
from servo_configurator.simulation import MotionMode, ServoModel, SimulatedNvs
from servo_configurator.transport import SIMULATED_PORT, SimulatedTransport, simulated_port
from servo_configurator.transport.base import PortInfo


def advance(model: ServoModel, seconds: float, step: float = 0.02) -> None:
    """Интегрирует модель мелкими шагами, как это делает прошивка."""
    for _ in range(int(seconds / step)):
        model.update(step)


class TestServoMotion:
    def test_moves_towards_target(self):
        model = ServoModel(position=1000.0)
        model.move_to(2000, speed=1000)
        advance(model, 2.0)
        assert model.position == pytest.approx(2000, abs=10)

    def test_stops_at_target(self):
        model = ServoModel(position=1000.0)
        model.move_to(1500, speed=1000)
        advance(model, 3.0)
        assert model.mode is MotionMode.HOLD
        assert model.velocity == pytest.approx(0.0, abs=1.0)

    def test_does_not_overshoot(self):
        model = ServoModel(position=1000.0)
        model.move_to(1050, speed=3000)
        advance(model, 2.0)
        assert model.position == pytest.approx(1050, abs=10)

    def test_speed_is_limited(self):
        model = ServoModel(position=500.0)
        model.move_to(3500, speed=500)
        advance(model, 1.0)
        # За секунду со скоростью 500 шаг/с привод не может уйти намного дальше.
        assert model.position - 500 < 600

    def test_acceleration_is_gradual(self):
        """Скорость не должна достигать заданной мгновенно."""
        model = ServoModel(position=1000.0)
        model.move_to(3000, speed=2000)
        model.update(0.005)
        assert abs(model.velocity) < 2000

    def test_wheel_mode_runs_continuously(self):
        model = ServoModel(position=1000.0)
        model.run(600)
        advance(model, 1.0)
        assert model.position > 1500

    def test_negative_speed_reverses_direction(self):
        model = ServoModel(position=2000.0)
        model.run(-600)
        advance(model, 1.0)
        assert model.position < 1600

    def test_stop_halts_immediately(self):
        model = ServoModel(position=1000.0)
        model.run(1000)
        advance(model, 0.5)
        model.stop()
        position = model.position
        advance(model, 1.0)
        assert model.position == position


class TestMechanicalStops:
    def test_position_never_exceeds_upper_stop(self):
        model = ServoModel(position=3000.0, stop_max=3500.0)
        model.run(2000)
        advance(model, 3.0)
        assert model.position <= 3500.0

    def test_position_never_exceeds_lower_stop(self):
        model = ServoModel(position=500.0, stop_min=200.0)
        model.run(-2000)
        advance(model, 3.0)
        assert model.position >= 200.0

    def test_load_rises_at_the_stop(self):
        """Рост нагрузки в упоре — то, по чему Homing находит механический предел."""
        model = ServoModel(position=3400.0, stop_max=3500.0)
        model.run(1000)
        advance(model, 2.0)
        assert model.stalled
        assert model.measured_load > 500

    def test_load_is_low_while_moving_freely(self):
        model = ServoModel(position=1000.0)
        model.run(500)
        advance(model, 1.0)
        assert model.measured_load < 300

    def test_load_falls_after_leaving_the_stop(self):
        model = ServoModel(position=3400.0, stop_max=3500.0)
        model.run(1000)
        advance(model, 2.0)
        model.run(-1000)
        advance(model, 2.0)
        assert model.measured_load < 400


class TestPositionCounter:
    def test_stops_shift_with_the_counter(self):
        """Назначение нуля сдвигает всю шкалу, включая упоры."""
        model = ServoModel(position=3000.0, stop_min=180.0, stop_max=3900.0)
        model.set_position_counter(0)
        assert model.position == 0
        assert model.stop_min == pytest.approx(180.0 - 3000.0)
        assert model.stop_max == pytest.approx(3900.0 - 3000.0)

    def test_travel_range_is_preserved(self):
        model = ServoModel(position=2000.0, stop_min=100.0, stop_max=3800.0)
        span = model.stop_max - model.stop_min
        model.set_position_counter(500)
        assert model.stop_max - model.stop_min == pytest.approx(span)


class TestFeedback:
    def test_reports_all_registers(self):
        feedback = ServoModel().feedback()
        assert {"pos", "spd", "load", "volt", "temp", "cur", "moving"} == feedback.keys()

    def test_moving_flag_follows_velocity(self):
        model = ServoModel(position=1000.0)
        assert model.feedback()["moving"] is False
        model.run(1000)
        advance(model, 0.5)
        assert model.feedback()["moving"] is True

    def test_load_is_bounded(self):
        model = ServoModel(position=3400.0, stop_max=3500.0)
        model.run(3000)
        advance(model, 3.0)
        assert -1000 <= model.feedback()["load"] <= 1000

    def test_torque_release_drops_load(self):
        model = ServoModel(position=1000.0)
        model.run(1000)
        advance(model, 1.0)
        model.stop(release_torque=True)
        advance(model, 1.0)
        assert model.measured_load < 20

    def test_results_are_reproducible(self):
        """Один и тот же seed даёт одинаковый прогон — иначе тесты плавали бы."""
        first, second = ServoModel(seed=1), ServoModel(seed=1)
        for model in (first, second):
            model.run(800)
            advance(model, 1.0)
        assert first.feedback() == second.feedback()


class TestSimulatedNvs:
    def test_round_trip(self, tmp_path):
        nvs = SimulatedNvs(tmp_path / "nvs.json")
        nvs.save({"version": 1, "value": 42})
        assert nvs.load() == {"version": 1, "value": 42}

    def test_missing_storage_returns_none(self, tmp_path):
        assert SimulatedNvs(tmp_path / "absent.json").load() is None

    def test_corrupted_storage_returns_none(self, tmp_path):
        path = tmp_path / "nvs.json"
        path.write_text("{broken", encoding="utf-8")
        assert SimulatedNvs(path).load() is None

    def test_in_memory_mode_is_isolated(self):
        nvs = SimulatedNvs(None)
        nvs.save({"value": 1})
        assert nvs.load() == {"value": 1}
        assert SimulatedNvs(None).load() is None

    def test_clear_removes_data(self, tmp_path):
        nvs = SimulatedNvs(tmp_path / "nvs.json")
        nvs.save({"value": 1})
        nvs.clear()
        assert nvs.load() is None

    def test_save_is_atomic(self, tmp_path):
        """Временный файл не остаётся рядом с хранилищем."""
        path = tmp_path / "nvs.json"
        nvs = SimulatedNvs(path)
        nvs.save({"value": 1})
        assert list(tmp_path.iterdir()) == [path]


class TestSimulatedTransport:
    @pytest.fixture
    def transport(self):
        transport = SimulatedTransport(persist=False)
        transport.open()
        yield transport
        transport.close()

    def test_emits_boot_on_open(self, transport):
        (line,) = LineAssembler().feed(transport.read(0.1))
        assert json.loads(line)["evt"] == "boot"

    def test_command_gets_a_response(self, transport):
        transport.read(0.1)  # снять событие boot
        transport.write(b'{"id":1,"cmd":"ping"}\n')

        lines = LineAssembler().feed(transport.read(0.2))
        response = json.loads(lines[0])
        assert response["id"] == 1 and response["ok"] is True

    def test_read_returns_nothing_when_idle(self, transport):
        transport.read(0.1)
        assert transport.read(0.05) == b""

    def test_operations_require_open_channel(self):
        transport = SimulatedTransport(persist=False)
        with pytest.raises(TransportError):
            transport.read(0.01)
        with pytest.raises(TransportError):
            transport.write(b"{}\n")

    def test_broken_link_raises(self, transport):
        transport.faults.link_broken = True
        with pytest.raises(TransportError):
            transport.read(0.05)

    def test_silence_suppresses_output(self, transport):
        transport.read(0.1)
        transport.faults.silent = True
        transport.write(b'{"id":1,"cmd":"ping"}\n')
        assert transport.read(0.15) == b""

    def test_corruption_damages_only_requested_frames(self, transport):
        transport.read(0.1)
        transport.faults.corrupt_frames = 1

        transport.write(b'{"id":1,"cmd":"ping"}\n')
        transport.read(0.15)  # испорченный кадр

        transport.write(b'{"id":2,"cmd":"ping"}\n')
        lines = LineAssembler().feed(transport.read(0.2))
        assert any(json.loads(line).get("id") == 2 for line in lines)

    def test_context_manager_closes(self):
        with SimulatedTransport(persist=False) as transport:
            assert transport.is_open
        assert not transport.is_open


class TestPortInfo:
    def test_simulated_port_is_advertised(self):
        assert simulated_port().device == SIMULATED_PORT

    @pytest.mark.parametrize("vid", [0x10C4, 0x1A86, 0x0403, 0x303A])
    def test_known_bridges_are_recognised(self, vid):
        assert PortInfo(device="COM3", vid=vid).is_likely_esp32

    def test_unknown_vendor_is_not_recognised(self):
        assert not PortInfo(device="COM3", vid=0x1234).is_likely_esp32

    def test_label_includes_description(self):
        assert PortInfo(device="COM3", description="CP210x").label == "COM3 — CP210x"

    def test_label_without_description(self):
        assert PortInfo(device="COM3").label == "COM3"
