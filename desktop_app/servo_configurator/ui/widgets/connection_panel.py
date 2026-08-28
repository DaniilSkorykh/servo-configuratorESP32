"""Блок подключения: выбор порта, обновление списка, состояние связи."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...transport import SIMULATED_PORT, available_ports, simulated_port


class ConnectionPanel(QGroupBox):
    """Выбор порта и управление соединением."""

    #: Запрошено подключение; ``None`` — режим симуляции.
    connect_requested = pyqtSignal(object)

    #: Запрошено отключение.
    disconnect_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Подключение", parent)
        self._connected = False

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(240)

        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.clicked.connect(self.refresh_ports)

        self.connect_button = QPushButton("Подключить")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self._on_connect_clicked)

        self.status_label = QLabel()

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Порт:"))
        controls.addWidget(self.port_combo, 1)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.connect_button)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)

        self.refresh_ports()
        self.set_connected(False)

    # ------------------------------------------------------------------

    def refresh_ports(self) -> None:
        """Перечитывает список портов, сохраняя текущий выбор.

        Симулятор стоит первым: он доступен всегда и не зависит от того,
        подключено ли оборудование.
        """
        previous = self.port_combo.currentData()
        self.port_combo.clear()

        simulated = simulated_port()
        self.port_combo.addItem(simulated.label, None)

        for port in available_ports():
            self.port_combo.addItem(port.label, port.device)

        index = self.port_combo.findData(previous)
        self.port_combo.setCurrentIndex(max(0, index))

    def selected_port(self) -> str | None:
        """Выбранный порт; ``None`` — режим симуляции."""
        return self.port_combo.currentData()

    def set_connected(self, connected: bool, description: str = "") -> None:
        """Обновляет вид панели под состояние соединения."""
        self._connected = connected
        self.connect_button.setText("Отключить" if connected else "Подключить")
        self.port_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected)

        if connected:
            self._set_status(f"Подключено — {description}", "#1a7f37")
        else:
            self._set_status(description or "Не подключено", "#8a8a8a")

    def set_error(self, message: str) -> None:
        self._set_status(message, "#c0392b")

    def set_busy(self, busy: bool) -> None:
        self.connect_button.setEnabled(not busy)

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
        else:
            port = self.selected_port()
            self._set_status(
                f"Подключение к {port or SIMULATED_PORT}…", "#8a6d1f"
            )
            self.connect_requested.emit(port)
