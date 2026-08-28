"""Блок ручного управления: позиционный режим, вращение, останов."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...protocol import Direction

#: Стиль кнопки аварийного останова: она должна находиться взглядом мгновенно.
_EMERGENCY_STYLE = """
QPushButton {
    background-color: #c0392b;
    color: white;
    font-weight: bold;
    padding: 10px;
}
QPushButton:disabled { background-color: #d8a49e; }
QPushButton:hover:!disabled { background-color: #a93226; }
"""


class ManualPanel(QGroupBox):
    """Ручная проверка настроек без сторонних утилит (п. 4.5 задания)."""

    move_requested = pyqtSignal(int, int)      # позиция, скорость
    motor_requested = pyqtSignal(object, int)  # направление, скорость
    stop_requested = pyqtSignal(bool)          # аварийный останов

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Ручное управление", parent)

        self.position_spin = QSpinBox()
        self.position_spin.setRange(0, 4095)
        self.position_spin.setValue(2048)
        self.position_spin.setAccelerated(True)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 4095)
        self.position_slider.setValue(2048)

        # Ползунок и поле показывают одно значение: правка любого меняет второе.
        self.position_slider.valueChanged.connect(self.position_spin.setValue)
        self.position_spin.valueChanged.connect(self.position_slider.setValue)

        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(1, 3000)
        self.speed_spin.setValue(1000)
        self.speed_spin.setSuffix(" шаг/с")
        self.speed_spin.setToolTip("Скорость для ручных команд")

        self.move_button = QPushButton("Переместить")
        self.move_button.clicked.connect(self._on_move_clicked)

        self.cw_button = QPushButton("◀ CW")
        self.cw_button.setToolTip("Непрерывное вращение по часовой стрелке")
        self.cw_button.clicked.connect(lambda: self._on_motor_clicked(Direction.CW))

        self.ccw_button = QPushButton("CCW ▶")
        self.ccw_button.setToolTip("Непрерывное вращение против часовой стрелки")
        self.ccw_button.clicked.connect(lambda: self._on_motor_clicked(Direction.CCW))

        self.stop_button = QPushButton("СТОП")
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(False))

        self.emergency_button = QPushButton("АВАРИЙНЫЙ ОСТАНОВ")
        self.emergency_button.setToolTip("Немедленно остановить привод и снять момент")
        self.emergency_button.setStyleSheet(_EMERGENCY_STYLE)
        self.emergency_button.clicked.connect(lambda: self.stop_requested.emit(True))

        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("Позиция:"))
        position_row.addWidget(self.position_spin)
        position_row.addWidget(self.position_slider, 1)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Скорость:"))
        speed_row.addWidget(self.speed_spin)
        speed_row.addWidget(self.move_button)
        speed_row.addStretch(1)

        motion = QGridLayout()
        motion.addWidget(self.cw_button, 0, 0)
        motion.addWidget(self.stop_button, 0, 1)
        motion.addWidget(self.ccw_button, 0, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(position_row)
        layout.addLayout(speed_row)
        layout.addLayout(motion)
        layout.addWidget(self.emergency_button)

        self.set_connected(False)

    # ------------------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        for widget in (
            self.position_spin, self.position_slider, self.speed_spin,
            self.move_button, self.cw_button, self.ccw_button,
            self.stop_button, self.emergency_button,
        ):
            widget.setEnabled(connected)

    def set_position_range(self, minimum: int, maximum: int) -> None:
        """Ограничивает ввод настроенным рабочим диапазоном.

        Устройство всё равно проверит границы, но задать заведомо недопустимую
        цель через интерфейс не получается вовсе — это понятнее, чем получить
        отказ после нажатия кнопки.
        """
        if minimum >= maximum:
            return
        self.position_spin.setRange(minimum, maximum)
        self.position_slider.setRange(minimum, maximum)

    def set_default_speed(self, speed: int) -> None:
        """Подставляет рабочую скорость из конфигурации, пока её не правили."""
        if not self.speed_spin.hasFocus():
            self.speed_spin.setValue(speed)

    def _on_move_clicked(self) -> None:
        self.move_requested.emit(self.position_spin.value(), self.speed_spin.value())

    def _on_motor_clicked(self, direction: Direction) -> None:
        self.motor_requested.emit(direction, self.speed_spin.value())
