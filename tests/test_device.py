"""Интеграционные тесты связки транспорт → клиент → устройство.

Проверяется сквозной обмен через симулятор: реальный поток чтения, реальные
таймауты, разбор кадров. Ошибочные ситуации из раздела 7 задания воспроизводятся
имитацией неисправностей, а не физическим отключением кабеля.
"""

from __future__ import annotations

import threading
import time

import pytest

from servo_configurator.device import ServoDevice
from servo_configurator.protocol import (
    Command,
    CommandError,
    CommandTimeout,
    DeviceError,
    DeviceState,
    Direction,
    Notification,
    Telemetry,
    TransportError,
)
from servo_configurator.simulation import ServoModel, SimulatedFirmware
from servo_configurator.transport import SimulatedTransport

#: Укороченный таймаут: тесты не должны ждать штатную секунду.
FAST_TIMEOUT = 0.3


class Collector:
    """Потокобезопасный сбор телеметрии и событий."""

    def __init__(self) -> None:
        self.telemetry: list[Telemetry] = []
        self.events: list[Notification] = []
        self.link_lost: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def on_telemetry(self, frame: Telemetry) -> None:
        with self._lock:
            self.telemetry.append(frame)

    def on_event(self, notification: Notification) -> None:
        with self._lock:
            self.events.append(notification)

    def on_link_lost(self, code: str, message: str) -> None:
        with self._lock:
            self.link_lost.append((code, message))

    def wait_for(self, predicate, timeout: float = 3.0) -> bool:
        """Ждёт выполнения условия, не полагаясь на фиксированные паузы."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if predicate(self):
                    return True
            time.sleep(0.01)
        return False


@pytest.fixture
def transport() -> SimulatedTransport:
    """Симулятор без сохранения конфигурации между тестами."""
    return SimulatedTransport(
        persist=False,
        firmware=SimulatedFirmware(servo=ServoModel(position=1500.0)),
    )


@pytest.fixture
def collector() -> Collector:
    return Collector()


@pytest.fixture
def device(transport, collector):
    """Подключённое устройство; отключается по завершении теста."""
    device = ServoDevice(
        transport,
        on_telemetry=collector.on_telemetry,
        on_event=collector.on_event,
        on_link_lost=collector.on_link_lost,
    )
    device.connect()
    yield device
    device.disconnect()


class TestConnection:
    def test_handshake_returns_device_info(self, device):
        info = device.ping()
        assert info["dev"] == "ws-servo-esp32"
        assert info["proto"] == 1

    def test_handshake_reports_servo_presence(self, device):
        """Наличие привода на шине видно сразу после подключения."""
        assert device.ping()["servo_online"] is True

    def test_connect_reports_state(self, device):
        assert device.is_connected

    def test_boot_event_arrives(self, device, collector):
        assert collector.wait_for(lambda c: any(e.evt == "boot" for e in c.events))

    def test_disconnect_is_idempotent(self, device):
        device.disconnect()
        device.disconnect()
        assert not device.is_connected

    def test_commands_after_disconnect_are_rejected(self, device):
        device.disconnect()
        with pytest.raises(TransportError):
            device.ping()

    def test_failed_handshake_closes_the_port(self, transport, collector):
        """Порт не должен остаться открытым после неудачного подключения."""
        transport.faults.silent = True
        device = ServoDevice(transport, on_link_lost=collector.on_link_lost)

        with pytest.raises(CommandTimeout):
            device.connect()
        assert not transport.is_open

    def test_busy_port_reports_transport_error(self, transport):
        transport.faults.link_broken = True
        with pytest.raises(TransportError):
            ServoDevice(transport).connect()


class TestConfiguration:
    def test_read_returns_full_config(self, device):
        config, dirty = device.read_config()
        assert config["homing"]["speed"] == 300
        assert dirty is False

    def test_write_and_read_back(self, device):
        device.write_config({"homing": {"speed": 450}})
        config, dirty = device.read_config()
        assert config["homing"]["speed"] == 450
        assert dirty is True

    def test_save_clears_dirty(self, device):
        device.write_config({"homing": {"speed": 450}})
        device.save_config()
        _, dirty = device.read_config()
        assert dirty is False

    def test_local_validation_precedes_transmission(self, device):
        """Неверное значение объясняется сразу, а не кодом ошибки от устройства."""
        with pytest.raises(ValueError, match=r"homing\.speed"):
            device.write_config({"homing": {"speed": 99999}})

    def test_device_rejects_out_of_range_value(self, device):
        """Проверка на устройстве обязана работать и в обход локальной валидации."""
        with pytest.raises(CommandError) as info:
            device._client.request(
                Command.SET_CONFIG, {"config": {"homing": {"speed": 99999}}}
            )
        assert info.value.code == DeviceError.RANGE

    def test_restore_defaults(self, device):
        device.write_config({"homing": {"speed": 450}})
        config = device.restore_defaults()
        assert config["homing"]["speed"] == 300


class TestPersistence:
    """Пункт 17 сценария приёмки на настоящем файловом хранилище.

    Остальные тесты работают с хранилищем в памяти, чтобы не влиять друг на
    друга; здесь проверяется именно тот путь, которым пользуется приложение.
    """

    def make_device(self, path):
        transport = SimulatedTransport(persist=True, nvs_path=path)
        device = ServoDevice(transport)
        device.connect()
        return device

    def test_saved_config_survives_restart(self, tmp_path):
        path = tmp_path / "sim_nvs.json"

        first = self.make_device(path)
        first.write_config({"homing": {"speed": 777, "dir": "ccw"}})
        first.save_config()
        first.disconnect()

        second = self.make_device(path)
        config, dirty = second.read_config()
        second.disconnect()

        assert config["homing"]["speed"] == 777
        assert config["homing"]["dir"] == "ccw"
        assert dirty is False

    def test_unsaved_changes_are_lost(self, tmp_path):
        path = tmp_path / "sim_nvs.json"

        first = self.make_device(path)
        first.write_config({"homing": {"speed": 777}})  # без save_config
        first.disconnect()

        second = self.make_device(path)
        config, _ = second.read_config()
        second.disconnect()

        assert config["homing"]["speed"] == 300

    def test_restore_defaults_is_persisted(self, tmp_path):
        path = tmp_path / "sim_nvs.json"

        first = self.make_device(path)
        first.write_config({"homing": {"speed": 777}})
        first.save_config()
        first.restore_defaults(save=True)
        first.disconnect()

        second = self.make_device(path)
        config, _ = second.read_config()
        second.disconnect()

        assert config["homing"]["speed"] == 300


class TestMotion:
    def test_homing_completes(self, device, collector):
        device.start_homing()
        assert collector.wait_for(
            lambda c: any(e.evt == "homing" for e in c.events), timeout=15.0
        )
        (result,) = [e.data for e in collector.events if e.evt == "homing"]
        assert result["result"] == "completed"

    def test_move_before_homing_is_rejected(self, device):
        with pytest.raises(CommandError) as info:
            device.move_to(2000)
        assert info.value.code == DeviceError.NOT_HOMED

    def test_move_to_confirms_target(self, device, collector):
        device.start_homing()
        collector.wait_for(lambda c: any(e.evt == "homing" for e in c.events), timeout=15.0)
        assert device.move_to(1200) == 1200

    def test_out_of_range_target_is_rejected(self, device, collector):
        device.start_homing()
        collector.wait_for(lambda c: any(e.evt == "homing" for e in c.events), timeout=15.0)
        with pytest.raises(CommandError) as info:
            device.move_to(9999)
        assert info.value.code == DeviceError.RANGE

    def test_motor_run_and_stop(self, device, transport):
        device.motor_run(Direction.CCW, speed=600)
        assert transport.firmware.state is DeviceState.MOTOR
        device.stop()
        assert transport.firmware.state is DeviceState.IDLE

    def test_keepalive_prevents_watchdog_during_long_motion(self, device, transport):
        """Пауза в командах во время вращения не должна выглядеть как обрыв связи.

        Прошивка останавливает привод, если ПК молчит дольше link_timeout_ms.
        Фоновый ping обязан удерживать канал живым, пока оператор просто смотрит
        на вращающийся привод.
        """
        device.motor_run(Direction.CCW, speed=500)
        time.sleep(1.5)
        assert transport.firmware.state is DeviceState.MOTOR

    def test_stop_is_retried_when_answer_is_lost(self, device, transport):
        """Потерянный ответ на остановку не должен выглядеть отказом.

        Повторное исполнение stop безвредно — привод уже стоит, — а вот
        сообщение об ошибке в момент, когда оператор останавливает движение,
        недопустимо.
        """
        device.motor_run(Direction.CCW, speed=500)

        # Ближайшие кадры портятся: первый ответ на stop до клиента не дойдёт.
        transport.faults.corrupt_frames = 2
        device.stop()

        assert transport.firmware.state is DeviceState.IDLE

    def test_keepalive_survives_occasional_losses(self, device, transport):
        """Отдельные помехи не должны прекращать фоновый обмен.

        Прекращение означало бы, что устройство через секунду сочтёт ПК
        отключившимся и остановит привод посреди штатного движения. Связь
        считается потерянной только когда ответов нет подряд (проверяется
        отдельно в :meth:`test_silent_device_is_treated_as_link_loss`).
        """
        device.motor_run(Direction.CCW, speed=400)

        # Между порчами проходит несколько успешных обменов, поэтому счётчик
        # неудач сбрасывается и связь остаётся живой.
        for _ in range(3):
            transport.faults.corrupt_frames = 1
            time.sleep(1.0)

        transport.faults.corrupt_frames = 0
        time.sleep(0.3)
        assert device.is_connected, "единичные помехи признаны потерей связи"
        assert transport.firmware.state is DeviceState.MOTOR, "привод остановлен watchdog"
        device.stop()

    def test_emergency_stop_releases_torque(self, device, transport):
        device.motor_run(Direction.CW, speed=600)
        device.stop(emergency=True)
        assert transport.firmware.servo.velocity == 0.0


class TestTelemetryStream:
    def test_frames_arrive(self, device, collector):
        device.set_telemetry(True, period_ms=20)
        assert collector.wait_for(lambda c: len(c.telemetry) >= 5)

    def test_frames_are_parsed(self, device, collector):
        device.set_telemetry(True, period_ms=20)
        collector.wait_for(lambda c: len(c.telemetry) >= 3)
        frame = collector.telemetry[0]
        assert isinstance(frame.pos, int)
        assert frame.state in set(DeviceState)

    def test_last_frame_is_cached(self, device, collector):
        device.set_telemetry(True, period_ms=20)
        collector.wait_for(lambda c: len(c.telemetry) >= 3)
        assert device.last_telemetry is not None

    def test_stall_is_detected_when_device_goes_silent(self, device, transport, collector):
        """Порт открыт, но данных нет — это тоже потеря связи."""
        device.set_telemetry(True, period_ms=20)
        collector.wait_for(lambda c: len(c.telemetry) >= 3)
        assert not device.is_telemetry_stalled()

        transport.faults.silent = True
        time.sleep(0.7)
        assert device.is_telemetry_stalled()

    def test_silent_device_is_treated_as_link_loss(self, device, transport, collector):
        """Молчание устройства обязано приводить к разрыву, а не к бесконечным
        таймаутам.

        Порт остаётся открытым и запись проходит, поэтому ошибка транспорта не
        возникает. Без отдельной проверки приложение продолжало бы слать команды
        и отвечать оператору таймаутами, вместо того чтобы признать связь
        потерянной и дать переподключиться.
        """
        device.set_telemetry(True, 20)
        time.sleep(0.2)

        transport.faults.silent = True
        assert collector.wait_for(lambda c: bool(c.link_lost), timeout=8.0),             "молчание устройства не признано потерей связи"

        assert not device.is_connected
        _code, message = collector.link_lost[0]
        assert "не отвеча" in message

    def test_no_stall_reported_without_telemetry(self, device):
        assert device.is_telemetry_stalled() is False


class TestErrorHandling:
    def test_timeout_when_device_is_silent(self, device, transport):
        transport.faults.silent = True
        with pytest.raises(CommandTimeout):
            device._client.request(Command.STOP, {}, timeout=FAST_TIMEOUT)

    def test_link_loss_is_reported_once(self, device, transport, collector):
        device.set_telemetry(True, period_ms=20)
        collector.wait_for(lambda c: len(c.telemetry) >= 2)

        transport.faults.link_broken = True
        assert collector.wait_for(lambda c: len(c.link_lost) == 1)

        time.sleep(0.3)
        assert len(collector.link_lost) == 1

    def test_pending_command_fails_on_link_loss(self, device, transport):
        """Ожидающий ответа вызов не должен висеть до таймаута после обрыва."""
        transport.faults.silent = True
        errors: list[Exception] = []

        def call() -> None:
            try:
                device.ping()
            except Exception as exc:
                errors.append(exc)

        caller = threading.Thread(target=call)
        caller.start()
        time.sleep(0.05)
        transport.faults.link_broken = True
        caller.join(timeout=3.0)

        assert not caller.is_alive()
        assert errors

    def test_corrupted_frames_do_not_break_the_link(self, device, transport, collector):
        """Битые кадры отбрасываются, обмен продолжается со следующего сообщения."""
        transport.faults.corrupt_frames = 3
        device.set_telemetry(True, period_ms=20)
        assert collector.wait_for(lambda c: len(c.telemetry) >= 5)
        assert device.ping()["proto"] == 1

    def test_device_error_carries_code_and_message(self, device):
        with pytest.raises(CommandError) as info:
            device.move_to(2000)
        assert info.value.code == DeviceError.NOT_HOMED
        assert info.value.detail


class TestConcurrency:
    def test_parallel_commands_get_matching_responses(self, device):
        """Ответы обязаны попадать своим запросам, а не первому ожидающему."""
        results: dict[int, str] = {}
        lock = threading.Lock()

        def worker(index: int) -> None:
            info = device.ping()
            with lock:
                results[index] = info["dev"]

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        assert len(results) == 8
        assert set(results.values()) == {"ws-servo-esp32"}

    def test_stop_works_while_another_command_is_pending(self, device, transport):
        """Аварийный останов не должен ждать освобождения канала."""
        device.motor_run(Direction.CCW, speed=500)

        stopped = threading.Event()

        def emergency() -> None:
            device.stop(emergency=True)
            stopped.set()

        thread = threading.Thread(target=emergency)
        thread.start()
        thread.join(timeout=3.0)

        assert stopped.is_set()
        # Аварийный останов запирает устройство, а не возвращает его в idle.
        assert transport.firmware.state is DeviceState.ESTOP
