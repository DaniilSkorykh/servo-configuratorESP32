"""Блок телеметрии: числовые показания и состояние устройства."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QWidget,
)

from ...protocol import DeviceState, Telemetry, describe

#: Подписи состояний и их цвета.
_STATE_LABELS = {
    DeviceState.IDLE: ("Idle — готов", "#1a7f37"),
    DeviceState.HOMING: ("Homing — поиск упора", "#8a6d1f"),
    DeviceState.POSITION: ("Position — перемещение", "#1f6f8a"),
    DeviceState.MOTOR: ("Motor — непрерывное вращение", "#1f6f8a"),
    DeviceState.FAULT: ("Fault — ошибка, требуется СТОП", "#c0392b"),
}

_PLACEHOLDER = "—"


class TelemetryPanel(QGroupBox):
    """Текущие показания привода (п. 4.6 задания)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Телеметрия", parent)

        self._values: dict[str, QLabel] = {}

        layout = QGridLayout(self)
        fields = [
            ("pos", "Позиция", "шаг"),
            ("spd", "Скорость", "шаг/с"),
            ("load", "Нагрузка", "0.1 %"),
            ("cur", "Ток", "6.5 мА"),
            ("volt", "Напряжение", "В"),
            ("temp", "Температура", "°C"),
        ]

        for row, (key, label, unit) in enumerate(fields):
            column = (row % 2) * 3
            line = row // 2

            layout.addWidget(QLabel(f"{label}:"), line, column)

            value = QLabel(_PLACEHOLDER)
            value.setStyleSheet("font-family: monospace; font-weight: bold;")
            layout.addWidget(value, line, column + 1)
            self._values[key] = value

            layout.addWidget(QLabel(unit), line, column + 2)

        self.state_label = QLabel("Не подключено")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label, 3, 0, 1, 6)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #c0392b;")
        layout.addWidget(self.error_label, 4, 0, 1, 6)

        layout.setColumnStretch(2, 1)
        layout.setColumnStretch(5, 1)

    # ------------------------------------------------------------------

    def update_telemetry(self, frame: Telemetry) -> None:
        """Обновляет показания по очередному кадру."""
        self._values["pos"].setText(str(frame.pos))
        self._values["spd"].setText(str(frame.spd))
        self._values["load"].setText(str(frame.load))
        self._values["cur"].setText(_optional(frame.cur))
        # Напряжение приходит в десятых долях вольта.
        self._values["volt"].setText(
            f"{frame.volt / 10:.1f}" if frame.volt is not None else _PLACEHOLDER
        )
        self._values["temp"].setText(_optional(frame.temp))

        self.show_state(frame.state, homed=frame.homed)
        self.error_label.setText(describe(frame.err) if frame.err else "")

    def show_state(self, state: DeviceState, *, homed: bool | None = None) -> None:
        text, color = _STATE_LABELS.get(state, (str(state), "#8a8a8a"))
        if homed is not None and not homed:
            text = f"{text}; Homing не выполнен"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color}; font-weight: bold;")

    def clear(self) -> None:
        """Сбрасывает показания при отключении, чтобы не выдавать их за текущие."""
        for value in self._values.values():
            value.setText(_PLACEHOLDER)
        self.state_label.setText("Не подключено")
        self.state_label.setStyleSheet("color: #8a8a8a;")
        self.error_label.setText("")


def _optional(value: int | None) -> str:
    return str(value) if value is not None else _PLACEHOLDER
