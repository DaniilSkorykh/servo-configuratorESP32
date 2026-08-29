"""Блоки настройки: параметры Homing и рабочие параметры.

Оба построены на :class:`~.param_form.ParamForm`, поэтому отличаются только
набором путей параметров и составом кнопок.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...protocol import HomingResult, set_value
from .param_form import ParamForm

#: Параметры процедуры поиска механического упора (п. 4.3 задания).
HOMING_PARAMS = [
    "homing.dir",
    "homing.speed",
    "homing.load_threshold",
    "homing.timeout_ms",
    "homing.max_travel",
    "homing.zero_position",
]

#: Параметры штатной работы (п. 4.4 задания).
OPERATING_PARAMS = [
    "operating.speed",
    "operating.load_limit",
    "operating.pos_min",
    "operating.pos_max",
    "operating.accel",
    "safety.link_timeout_ms",
]

#: Как показывать исход Homing: подпись и цвет.
_HOMING_LABELS = {
    HomingResult.COMPLETED: ("Completed — упор найден", "#1a7f37"),
    HomingResult.TIMEOUT: ("Timeout — упор не найден за отведённое время", "#c0392b"),
    HomingResult.ABORTED: ("Aborted — процедура прервана", "#8a6d1f"),
    HomingResult.ERROR: ("Error — процедура завершилась ошибкой", "#c0392b"),
}


class _ConfigPanel(QGroupBox):
    """Общая часть блоков настройки: форма плюс чтение и запись."""

    #: Запрошена запись изменённых параметров.
    write_requested = pyqtSignal(dict)

    #: Запрошено чтение конфигурации с устройства.
    read_requested = pyqtSignal()

    def __init__(self, title: str, paths: list[str], parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.form = ParamForm(paths)
        self.form.edited.connect(self._update_write_button)

        self.read_button = QPushButton("Прочитать")
        self.read_button.clicked.connect(self.read_requested)

        self.write_button = QPushButton("Записать")
        self.write_button.clicked.connect(self._on_write_clicked)

        self._buttons = QHBoxLayout()
        self._buttons.addWidget(self.read_button)
        self._buttons.addWidget(self.write_button)
        self._buttons.addStretch(1)

        self._layout = QVBoxLayout(self)
        self._layout.addWidget(self.form)
        self._layout.addLayout(self._buttons)

        self.set_connected(False)

    def load_config(self, config: dict[str, Any]) -> None:
        self.form.load(config)
        self._update_write_button()

    def set_connected(self, connected: bool) -> None:
        self.form.set_enabled(connected)
        self.read_button.setEnabled(connected)
        self._connected = connected
        self._update_write_button()

    def _update_write_button(self) -> None:
        # Запись предлагается только когда есть что записывать: так видно,
        # применены ли уже введённые значения.
        self.write_button.setEnabled(self._connected and self.form.has_changes())

    def _on_write_clicked(self) -> None:
        patch: dict[str, Any] = {}
        for path, value in self.form.changed_values().items():
            set_value(patch, path, value)
        if patch:
            self.write_requested.emit(patch)


class HomingPanel(_ConfigPanel):
    """Настройка Homing, запуск процедуры и её результат."""

    start_requested = pyqtSignal()
    abort_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Homing — калибровка нуля", HOMING_PARAMS, parent)

        self.start_button = QPushButton("Запустить Homing")
        self.start_button.clicked.connect(self.start_requested)

        self.abort_button = QPushButton("Прервать")
        self.abort_button.clicked.connect(self.abort_requested)

        self._blocked = False
        self.result_label = QLabel("Не выполнялся")
        self.result_label.setWordWrap(True)

        actions = QHBoxLayout()
        actions.addWidget(self.start_button)
        actions.addWidget(self.abort_button)
        actions.addStretch(1)

        self._layout.addLayout(actions)
        self._layout.addWidget(self.result_label)

        self.set_running(False)

    def set_connected(self, connected: bool) -> None:
        super().set_connected(connected)
        if hasattr(self, "start_button"):
            self.start_button.setEnabled(connected)
            self.abort_button.setEnabled(False)

    def set_blocked(self, blocked: bool) -> None:
        """Запрещает запуск процедуры, пока активен аварийный останов."""
        self._blocked = blocked
        self.start_button.setEnabled(self._connected and not blocked)

    def set_running(self, running: bool) -> None:
        """Отражает выполнение процедуры."""
        self.start_button.setEnabled(
            self._connected and not running and not self._blocked
        )
        self.abort_button.setEnabled(running)
        if running:
            self._set_result("Running — идёт поиск упора", "#8a6d1f")

    def show_result(self, result: str, position: int, elapsed_ms: int) -> None:
        text, color = _HOMING_LABELS.get(result, (f"Результат: {result}", "#c0392b"))
        if result == HomingResult.COMPLETED:
            text = f"{text}; позиция {position}, {elapsed_ms} мс"
        else:
            text = f"{text} ({elapsed_ms} мс)"
        self._set_result(text, color)

    def _set_result(self, text: str, color: str) -> None:
        self.result_label.setText(text)
        self.result_label.setStyleSheet(f"color: {color};")


class OperatingPanel(_ConfigPanel):
    """Рабочие параметры и сохранение конфигурации в память устройства."""

    save_requested = pyqtSignal()
    restore_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Рабочие параметры", OPERATING_PARAMS, parent)

        self.save_button = QPushButton("Сохранить в устройстве")
        self.save_button.setToolTip(
            "Записать конфигурацию в энергонезависимую память: "
            "она сохранится после перезагрузки контроллера"
        )
        self.save_button.clicked.connect(self.save_requested)

        self.restore_button = QPushButton("Значения по умолчанию")
        self.restore_button.clicked.connect(self.restore_requested)

        self.dirty_label = QLabel()
        self.dirty_label.setWordWrap(True)

        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.restore_button)
        actions.addStretch(1)

        self._layout.addLayout(actions)
        self._layout.addWidget(self.dirty_label)

    def set_connected(self, connected: bool) -> None:
        super().set_connected(connected)
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(connected)
            self.restore_button.setEnabled(connected)

    def set_dirty(self, dirty: bool) -> None:
        """Показывает, есть ли на устройстве несохранённые изменения."""
        if dirty:
            self.dirty_label.setText(
                "Есть изменения, не сохранённые в памяти устройства — "
                "они будут потеряны при перезагрузке"
            )
            self.dirty_label.setStyleSheet("color: #8a6d1f;")
        else:
            self.dirty_label.setText("Конфигурация сохранена в устройстве")
            self.dirty_label.setStyleSheet("color: #8a8a8a;")
