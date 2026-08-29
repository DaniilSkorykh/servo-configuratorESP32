"""Главное окно приложения.

Окно занимается только компоновкой и связыванием сигналов: вся работа с
устройством идёт через :class:`~..app.service.ServoService`. Логики протокола
здесь нет намеренно — это требование раздела 9 задания к архитектуре.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..app.service import ServoService
from ..protocol import DeviceState, Direction, Telemetry, get_value
from ..transport import SimulatedTransport
from .widgets.charts_panel import ChartsPanel
from .widgets.config_panels import HomingPanel, OperatingPanel
from .widgets.connection_panel import ConnectionPanel
from .widgets.manual_panel import ManualPanel
from .widgets.telemetry_panel import TelemetryPanel

logger = logging.getLogger(__name__)

#: Период проверки того, что поток телеметрии не оборвался, мс.
_STALL_CHECK_INTERVAL_MS = 500


class MainWindow(QMainWindow):
    """Основное окно конфигуратора."""

    def __init__(self, service: ServoService | None = None) -> None:
        super().__init__()
        self.service = service or ServoService(self)

        self.setWindowTitle("Конфигуратор сервопривода Feetech STS3215")
        self.resize(1180, 860)

        self._build_ui()
        self._connect_signals()
        self._set_connected(False)

    # ------------------------------------------------------------------
    # Компоновка
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.connection_panel = ConnectionPanel()
        self.homing_panel = HomingPanel()
        self.operating_panel = OperatingPanel()
        self.manual_panel = ManualPanel()
        self.telemetry_panel = TelemetryPanel()
        self.charts_panel = ChartsPanel()

        self.demo_faults = _DemoFaultPanel()

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self.connection_panel)
        controls_layout.addWidget(self.homing_panel)
        controls_layout.addWidget(self.operating_panel)
        controls_layout.addWidget(self.manual_panel)
        controls_layout.addWidget(self.demo_faults)
        controls_layout.addStretch(1)

        # Панели настроек не помещаются по высоте на небольших экранах,
        # поэтому левая колонка прокручивается.
        scroll = QScrollArea()
        scroll.setWidget(controls)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(470)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.telemetry_panel)
        right_layout.addWidget(self.charts_panel, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 710])

        self.setCentralWidget(splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.mode_label = QLabel()
        self.status_bar.addPermanentWidget(self.mode_label)
        self._update_mode_label()

    # ------------------------------------------------------------------
    # Связывание
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self.connection_panel.connect_requested.connect(self.service.connect_to)
        self.connection_panel.disconnect_requested.connect(self.service.disconnect)

        self.homing_panel.read_requested.connect(self.service.read_config)
        self.homing_panel.write_requested.connect(self.service.write_config)
        self.homing_panel.start_requested.connect(self._on_homing_start)
        self.homing_panel.abort_requested.connect(self.service.abort_homing)

        self.operating_panel.read_requested.connect(self.service.read_config)
        self.operating_panel.write_requested.connect(self.service.write_config)
        self.operating_panel.save_requested.connect(self.service.save_config)
        self.operating_panel.restore_requested.connect(self._on_restore_defaults)

        self.manual_panel.move_requested.connect(self.service.move_to)
        self.manual_panel.motor_requested.connect(self._on_motor_requested)
        self.manual_panel.stop_requested.connect(self._on_stop_requested)

        self.demo_faults.fault_requested.connect(self._on_demo_fault)

        self.service.connected.connect(self._on_connected)
        self.service.disconnected.connect(self._on_disconnected)
        self.service.telemetry_received.connect(self._on_telemetry)
        self.service.state_changed.connect(self._on_state_changed)
        self.service.homing_finished.connect(self._on_homing_finished)
        self.service.config_loaded.connect(self._on_config_loaded)
        self.service.status_message.connect(self._on_status_message)
        self.service.error_occurred.connect(self._on_error)
        self.service.busy_changed.connect(self.connection_panel.set_busy)

        # Проверка молчания телеметрии: порт может остаться открытым, а данные
        # прекратиться — обычная ошибка транспорта этого не показывает.
        self._stall_timer = QTimer(self)
        self._stall_timer.timeout.connect(self._check_telemetry_stall)
        self._stall_timer.start(_STALL_CHECK_INTERVAL_MS)

    # ------------------------------------------------------------------
    # Реакция на сигналы сервиса
    # ------------------------------------------------------------------

    def _on_connected(self, info: dict[str, Any]) -> None:
        description = f"{info.get('dev', '?')} · прошивка {info.get('fw', '?')}"
        self.connection_panel.set_connected(True, description)
        self._set_connected(True)
        self.charts_panel.clear()
        self._update_mode_label()

    def _on_disconnected(self, reason: str) -> None:
        self.connection_panel.set_connected(False, reason or "Не подключено")
        if reason:
            self.connection_panel.set_error(reason)
            self.status_bar.showMessage(f"Связь потеряна: {reason}", 10000)
        self._set_connected(False)
        self.telemetry_panel.clear()
        self._update_mode_label()

    def _on_telemetry(self, frame: Telemetry) -> None:
        self.telemetry_panel.update_telemetry(frame)
        self.charts_panel.add_telemetry(frame)

    def _on_state_changed(self, state: DeviceState) -> None:
        self.telemetry_panel.show_state(state)
        self.homing_panel.set_running(state is DeviceState.HOMING)
        self.manual_panel.set_fault(state is DeviceState.FAULT)

    def _on_homing_finished(self, result: str, position: int, elapsed_ms: int) -> None:
        self.homing_panel.set_running(False)
        self.homing_panel.show_result(result, position, elapsed_ms)
        self.status_bar.showMessage(f"Homing: {result}", 5000)

    def _on_config_loaded(self, config: dict[str, Any], dirty: bool) -> None:
        self.homing_panel.load_config(config)
        self.operating_panel.load_config(config)
        self.operating_panel.set_dirty(dirty)

        # Ручное управление подстраивается под настроенный диапазон и скорость.
        pos_min = get_value(config, "operating.pos_min")
        pos_max = get_value(config, "operating.pos_max")
        if isinstance(pos_min, int) and isinstance(pos_max, int):
            self.manual_panel.set_position_range(pos_min, pos_max)

        speed = get_value(config, "operating.speed")
        if isinstance(speed, int):
            self.manual_panel.set_default_speed(speed)

    def _on_status_message(self, message: str) -> None:
        self.status_bar.showMessage(message, 5000)

    def _on_error(self, title: str, detail: str) -> None:
        # Сообщение об ошибке не должно исчезать само: пользователь может
        # смотреть на графики и пропустить его за отведённые секунды.
        self.status_bar.showMessage(f"{title}: {detail}")
        logger.warning("%s: %s", title, detail)

    # ------------------------------------------------------------------
    # Реакция на действия пользователя
    # ------------------------------------------------------------------

    def _on_homing_start(self) -> None:
        # Homing двигает привод до механического упора, поэтому запуск
        # подтверждается: при неверно заданном направлении движение пойдёт
        # в неожиданную сторону.
        answer = QMessageBox.question(
            self,
            "Запуск Homing",
            "Привод будет двигаться до механического упора.\n"
            "Убедитесь, что путь свободен и параметры заданы верно.\n\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.homing_panel.set_running(True)
            self.service.start_homing()

    def _on_restore_defaults(self) -> None:
        answer = QMessageBox.question(
            self,
            "Значения по умолчанию",
            "Текущая конфигурация будет заменена заводскими значениями "
            "и сохранена в памяти устройства.\n\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.restore_defaults()

    def _on_motor_requested(self, direction: Direction, speed: int) -> None:
        self.service.motor_run(direction, speed)

    def _on_stop_requested(self, emergency: bool) -> None:
        self.service.stop(emergency=emergency)

    def _on_demo_fault(self, kind: str) -> None:
        """Имитация неисправностей в режиме симуляции."""
        transport = self.service.transport
        if not isinstance(transport, SimulatedTransport):
            return

        if kind == "link":
            transport.faults.link_broken = True
            self.status_bar.showMessage("Симуляция: связь разорвана", 5000)
        elif kind == "silence":
            transport.faults.silent = True
            self.status_bar.showMessage("Симуляция: устройство перестало отвечать", 5000)
        elif kind == "corrupt":
            transport.faults.corrupt_frames = 5
            self.status_bar.showMessage("Симуляция: следующие кадры испорчены", 5000)

    def _check_telemetry_stall(self) -> None:
        if not self.service.is_connected:
            return
        if self.service.is_telemetry_stalled():
            self.telemetry_panel.show_state(DeviceState.FAULT)
            self.status_bar.showMessage(
                "Телеметрия не поступает: устройство не отвечает", 5000
            )

    # ------------------------------------------------------------------
    # Прочее
    # ------------------------------------------------------------------

    def _set_connected(self, connected: bool) -> None:
        for panel in (self.homing_panel, self.operating_panel, self.manual_panel):
            panel.set_connected(connected)
        self.demo_faults.setVisible(connected and self.service.is_demo)

    def _update_mode_label(self) -> None:
        if not self.service.is_connected:
            self.mode_label.setText("Режим: не подключено")
            self.mode_label.setStyleSheet("color: #8a8a8a;")
        elif self.service.is_demo:
            self.mode_label.setText("Режим: DEMO (симулятор)")
            self.mode_label.setStyleSheet("color: #8a6d1f; font-weight: bold;")
        else:
            self.mode_label.setText("Режим: реальное устройство")
            self.mode_label.setStyleSheet("color: #1a7f37; font-weight: bold;")

    def closeEvent(self, event: QCloseEvent) -> None:
        """Останавливает привод перед выходом.

        Закрытие окна не должно оставлять привод вращающимся.
        """
        self._stall_timer.stop()
        self.service.shutdown()
        super().closeEvent(event)


class _DemoFaultPanel(QGroupBox):
    """Кнопки имитации неисправностей — видны только в режиме симуляции.

    Позволяют проверить обработку ошибок из раздела 7 задания, не выдёргивая
    физический кабель и не дожидаясь настоящего сбоя.
    """

    #: Запрошена имитация неисправности: ``link``, ``silence`` или ``corrupt``.
    fault_requested = pyqtSignal(str)

    _FAULTS: ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("Обрыв связи", "link", "Как при выдёргивании USB во время работы"),
        ("Молчание", "silence", "Порт открыт, но устройство не отвечает"),
        ("Помехи", "corrupt", "Несколько испорченных кадров подряд"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Demo: имитация неисправностей", parent)

        layout = QHBoxLayout(self)
        for label, kind, tooltip in self._FAULTS:
            button = QPushButton(label)
            button.setToolTip(tooltip)
            button.clicked.connect(lambda _, k=kind: self.fault_requested.emit(k))
            layout.addWidget(button)
