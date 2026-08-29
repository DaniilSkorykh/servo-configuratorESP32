"""Тесты интерфейса: сборка окна, реакция на события устройства, сценарий работы.

Проверяется не внешний вид, а поведение: доступность органов управления в разных
состояниях, обновление показаний, отсутствие блокировки интерфейса во время
обмена и корректное завершение работы.
"""

from __future__ import annotations

import numpy as np
import pytest

from servo_configurator.app import ServoService
from servo_configurator.protocol import (
    DeviceState,
    Telemetry,
    default_config,
)
from servo_configurator.transport import SimulatedTransport
from servo_configurator.ui.main_window import MainWindow
from servo_configurator.ui.widgets.charts_panel import _RingBuffer
from servo_configurator.ui.widgets.config_panels import HOMING_PARAMS, OPERATING_PARAMS
from servo_configurator.ui.widgets.param_form import ParamForm
from servo_configurator.ui.widgets.telemetry_panel import TelemetryPanel


def telemetry(**overrides) -> Telemetry:
    """Кадр телеметрии с разумными значениями по умолчанию."""
    defaults = {
        "seq": 1, "ts": 1000, "pos": 2048, "spd": 500, "load": 120,
        "volt": 74, "temp": 31, "cur": 45, "moving": True,
        "state": DeviceState.POSITION, "homed": True, "err": None,
    }
    return Telemetry(**{**defaults, **overrides})


class TestParamForm:
    def test_fields_are_built_from_schema(self, qapp):
        form = ParamForm(HOMING_PARAMS)
        assert set(form.values()) == set(HOMING_PARAMS)

    def test_spinbox_range_comes_from_schema(self, qapp):
        form = ParamForm(["homing.speed"])
        editor = form._editors["homing.speed"]
        assert (editor.minimum(), editor.maximum()) == (1, 3000)

    def test_load_fills_values(self, qapp):
        form = ParamForm(HOMING_PARAMS)
        config = default_config()
        config["homing"]["speed"] = 777
        form.load(config)
        assert form.values()["homing.speed"] == 777

    def test_loading_does_not_count_as_edit(self, qapp):
        """Программное заполнение не должно выглядеть правкой пользователя."""
        form = ParamForm(HOMING_PARAMS)
        form.load(default_config())
        assert not form.has_changes()

    def test_edit_is_detected(self, qapp):
        form = ParamForm(HOMING_PARAMS)
        form.load(default_config())
        form._editors["homing.speed"].setValue(999)
        assert form.changed_values() == {"homing.speed": 999}

    def test_only_changed_fields_are_reported(self, qapp):
        form = ParamForm(OPERATING_PARAMS)
        form.load(default_config())
        form._editors["operating.speed"].setValue(1234)
        assert list(form.changed_values()) == ["operating.speed"]

    def test_enum_field_holds_choices(self, qapp):
        form = ParamForm(["homing.dir"])
        combo = form._editors["homing.dir"]
        assert {combo.itemData(i) for i in range(combo.count())} == {"cw", "ccw"}

    def test_signal_is_emitted_on_edit(self, qapp):
        form = ParamForm(["homing.speed"])
        seen = []
        form.edited.connect(lambda: seen.append(True))
        form._editors["homing.speed"].setValue(500)
        assert seen


class TestRingBuffer:
    def test_keeps_points_in_order(self):
        buffer = _RingBuffer(capacity=5)
        for index in range(3):
            buffer.append(float(index), float(index * 10))
        times, values = buffer.data()
        assert list(times) == [0.0, 1.0, 2.0]
        assert list(values) == [0.0, 10.0, 20.0]

    def test_discards_oldest_when_full(self):
        buffer = _RingBuffer(capacity=3)
        for index in range(5):
            buffer.append(float(index), float(index))
        times, _ = buffer.data()
        assert list(times) == [2.0, 3.0, 4.0]

    def test_order_is_preserved_after_wrap(self):
        """После заворачивания данные обязаны остаться хронологическими."""
        buffer = _RingBuffer(capacity=4)
        for index in range(10):
            buffer.append(float(index), float(index))
        times, values = buffer.data()
        assert np.all(np.diff(times) > 0)
        assert list(values) == [6.0, 7.0, 8.0, 9.0]

    def test_clear_empties_buffer(self):
        buffer = _RingBuffer(capacity=4)
        buffer.append(1.0, 1.0)
        buffer.clear()
        assert len(buffer.data()[0]) == 0


class TestTelemetryPanel:
    def test_values_are_displayed(self, qapp):
        panel = TelemetryPanel()
        panel.update_telemetry(telemetry(pos=1234, spd=-56, load=78))
        assert panel._values["pos"].text() == "1234"
        assert panel._values["spd"].text() == "-56"
        assert panel._values["load"].text() == "78"

    def test_voltage_is_converted_to_volts(self, qapp):
        panel = TelemetryPanel()
        panel.update_telemetry(telemetry(volt=74))
        assert panel._values["volt"].text() == "7.4"

    def test_missing_optional_fields_are_shown_as_dashes(self, qapp):
        panel = TelemetryPanel()
        panel.update_telemetry(telemetry(volt=None, temp=None, cur=None))
        assert panel._values["temp"].text() == "—"

    def test_error_is_displayed(self, qapp):
        panel = TelemetryPanel()
        panel.update_telemetry(telemetry(err="E_SERVO_ERROR"))
        assert "ошибк" in panel.error_label.text().lower()

    def test_clear_resets_values(self, qapp):
        panel = TelemetryPanel()
        panel.update_telemetry(telemetry())
        panel.clear()
        assert panel._values["pos"].text() == "—"


@pytest.fixture
def window(qapp, pump):
    """Главное окно; после теста гарантированно закрывается."""
    window = MainWindow()
    yield window
    window.service.shutdown()
    window.close()
    pump(0.1)


class TestWindowLayout:
    def test_all_required_blocks_are_present(self, window):
        """Блоки из таблицы п. 5 задания."""
        assert window.connection_panel is not None
        assert window.homing_panel is not None
        assert window.operating_panel is not None
        assert window.manual_panel is not None
        assert window.telemetry_panel is not None
        assert window.charts_panel is not None

    def test_controls_are_disabled_before_connection(self, window):
        assert not window.manual_panel.move_button.isEnabled()
        assert not window.homing_panel.start_button.isEnabled()
        assert not window.operating_panel.save_button.isEnabled()

    def test_simulator_is_offered_first(self, window):
        assert window.connection_panel.port_combo.itemData(0) is None

    def test_three_charts_are_created(self, window):
        assert set(window.charts_panel._curves) == {"pos", "spd", "load"}


class TestConnectionFlow:
    def test_connects_to_simulator(self, window, pump):
        connect_demo(window, pump)
        assert window.service.is_connected

    def test_controls_become_available(self, window, pump):
        connect_demo(window, pump)
        assert window.manual_panel.move_button.isEnabled()
        assert window.homing_panel.start_button.isEnabled()

    def test_config_is_loaded_into_forms(self, window, pump):
        connect_demo(window, pump)
        pump(3.0, until=lambda: window.homing_panel.form.values()["homing.speed"] == 300)
        assert window.homing_panel.form.values()["homing.speed"] == 300

    def test_demo_mode_is_reported(self, window, pump):
        connect_demo(window, pump)
        assert "DEMO" in window.mode_label.text()

    def test_fault_injection_panel_is_visible_in_demo(self, window, pump):
        # Окно в тестах не показывается, поэтому проверяется видимость
        # относительно родителя, а не на экране.
        assert not window.demo_faults.isVisibleTo(window)
        connect_demo(window, pump)
        assert window.demo_faults.isVisibleTo(window)

    def test_disconnect_disables_controls(self, window, pump):
        connect_demo(window, pump)

        window.connection_panel.disconnect_requested.emit()
        assert pump(3.0, until=lambda: not window.manual_panel.move_button.isEnabled())
        assert not window.service.is_connected

    def test_unknown_port_is_reported_without_crash(self, window, pump):
        window.connection_panel.connect_requested.emit("COM_NOT_EXISTS")
        pump(3.0, until=lambda: "COM_NOT_EXISTS" in window.connection_panel.status_label.text())
        assert not window.service.is_connected


class TestTelemetryFlow:
    @pytest.fixture
    def connected(self, window, pump):
        connect_demo(window, pump)
        return window

    def test_frames_reach_the_panel(self, connected, pump):
        assert pump(3.0, until=lambda: connected.telemetry_panel._values["pos"].text() != "—")

    def test_frames_reach_the_charts(self, connected, pump):
        pump(3.0, until=lambda: len(connected.charts_panel._buffers["pos"].data()[0]) > 3)
        assert len(connected.charts_panel._buffers["pos"].data()[0]) > 3

    def test_charts_are_cleared_on_request(self, connected, pump):
        pump(1.0, until=lambda: len(connected.charts_panel._buffers["pos"].data()[0]) > 3)
        connected.charts_panel.clear()
        assert len(connected.charts_panel._buffers["pos"].data()[0]) == 0

    def test_position_range_follows_configuration(self, connected, pump):
        pump(3.0, until=lambda: connected.manual_panel.position_spin.maximum() == 4095)
        # Ноль Homing обязан лежать в рабочем диапазоне, поэтому он меняется
        # вместе с границами: иначе устройство отвергнет патч целиком.
        connected.service.write_config({
            "operating": {"pos_min": 100, "pos_max": 2000},
            "homing": {"zero_position": 100},
        })
        pump(3.0, until=lambda: connected.manual_panel.position_spin.maximum() == 2000)
        assert connected.manual_panel.position_spin.minimum() == 100


class TestScenario:
    """Сквозной прогон основного сценария через интерфейс."""

    @pytest.fixture
    def connected(self, window, pump):
        connect_demo(window, pump)
        return window

    def test_homing_completes_and_is_reported(self, connected, pump):
        connected.homing_panel.set_running(True)
        connected.service.start_homing()

        assert pump(20.0, until=lambda: "Completed" in connected.homing_panel.result_label.text())
        assert not connected.homing_panel.abort_button.isEnabled()

    def test_move_after_homing(self, connected, pump):
        connected.service.start_homing()
        pump(20.0, until=lambda: "Completed" in connected.homing_panel.result_label.text())

        connected.manual_panel.position_spin.setValue(1500)
        connected.manual_panel.move_button.click()

        assert pump(10.0, until=lambda: abs(_position(connected) - 1500) < 30)

    def test_motor_and_stop(self, connected, pump):
        connected.manual_panel.ccw_button.click()
        assert pump(3.0, until=lambda: _state(connected) is DeviceState.MOTOR)

        connected.manual_panel.stop_button.click()
        assert pump(3.0, until=lambda: _state(connected) is DeviceState.IDLE)

    def test_fault_state_points_to_the_stop_button(self, connected, pump):
        """Из состояния отказа должно быть видно, что делать дальше."""
        # Вращение до механического упора модели: сработает защита по нагрузке.
        connected.manual_panel.ccw_button.click()
        assert pump(20.0, until=lambda: _state(connected) is DeviceState.FAULT)
        pump(0.5)

        assert connected.manual_panel.stop_button.styleSheet(), "кнопка СТОП не выделена"
        assert "СТОП" in connected.telemetry_panel.state_label.text()
        assert "упор" in connected.status_bar.currentMessage()

        connected.manual_panel.stop_button.click()
        assert pump(5.0, until=lambda: _state(connected) is DeviceState.IDLE)
        pump(0.5)
        assert not connected.manual_panel.stop_button.styleSheet(), "выделение не снято"

    def test_emergency_stop(self, connected, pump):
        connected.manual_panel.ccw_button.click()
        pump(2.0, until=lambda: _state(connected) is DeviceState.MOTOR)

        connected.manual_panel.emergency_button.click()
        assert pump(3.0, until=lambda: _state(connected) is DeviceState.IDLE)

    def test_write_and_save_configuration(self, connected, pump):
        form = connected.operating_panel.form
        pump(3.0, until=lambda: form.values()["operating.speed"] == 1000)

        form._editors["operating.speed"].setValue(1234)
        assert connected.operating_panel.write_button.isEnabled()

        connected.operating_panel.write_button.click()
        assert pump(5.0, until=lambda: form.values()["operating.speed"] == 1234)

        connected.operating_panel.save_button.click()
        assert pump(5.0, until=lambda: "сохранена" in connected.operating_panel.dirty_label.text())

    def test_link_loss_is_handled(self, connected, pump):
        """Имитация выдёргивания USB не должна ронять приложение."""
        connected.demo_faults.fault_requested.emit("link")
        assert pump(5.0, until=lambda: not connected.service.is_connected)
        assert not connected.manual_panel.move_button.isEnabled()

    def test_corrupted_frames_do_not_break_ui(self, connected, pump):
        connected.demo_faults.fault_requested.emit("corrupt")
        pump(2.0)
        assert connected.service.is_connected

    def test_window_closes_without_leaving_drive_running(self, connected, pump):
        connected.manual_panel.ccw_button.click()
        pump(2.0, until=lambda: _state(connected) is DeviceState.MOTOR)

        transport = connected.service.transport
        assert isinstance(transport, SimulatedTransport)
        firmware = transport.firmware

        connected.close()
        pump(1.0)
        assert firmware.servo.velocity == 0.0


class TestSelfTest:
    """Режим самопроверки: им проверяют работоспособность установленной сборки."""

    def test_self_test_passes_on_simulator(self, qapp):
        import main

        window = MainWindow()
        try:
            code = main.run_self_test(qapp, window)
        finally:
            window.service.shutdown()
        assert code == 0

    def test_self_test_reports_all_checks(self, qapp, capsys):
        import main

        window = MainWindow()
        try:
            main.run_self_test(qapp, window)
        finally:
            window.service.shutdown()

        output = capsys.readouterr().out
        for expected in ("подключение", "конфигурация", "телеметрия", "графики"):
            assert expected in output


class TestServiceWithoutUi:
    def test_shutdown_without_connection_is_safe(self, qapp):
        service = ServoService()
        service.shutdown()

    def test_commands_without_connection_report_error(self, qapp, pump):
        service = ServoService()
        errors: list[tuple[str, str]] = []
        service.error_occurred.connect(lambda title, detail: errors.append((title, detail)))

        service.read_config()
        pump(2.0, until=lambda: bool(errors))
        assert errors
        service.shutdown()


def connect_demo(window: MainWindow, pump) -> None:
    """Подключается к симулятору и ждёт, пока это отразится в интерфейсе.

    Ждать по ``service.is_connected`` нельзя: флаг становится истинным в рабочем
    потоке раньше, чем сигнал дойдёт до окна, и проверки виджетов оказываются
    гонкой.
    """
    window.connection_panel.connect_requested.emit(None)
    assert pump(5.0, until=lambda: window.manual_panel.move_button.isEnabled()),         "интерфейс не перешёл в подключённое состояние"


def _position(window: MainWindow) -> int:
    text = window.telemetry_panel._values["pos"].text()
    return int(text) if text.lstrip("-").isdigit() else -10000


def _state(window: MainWindow) -> DeviceState | None:
    transport = window.service.transport
    return transport.firmware.state if isinstance(transport, SimulatedTransport) else None
