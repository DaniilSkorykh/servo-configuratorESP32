"""Форма параметров, построенная по схеме конфигурации.

Поля создаются из :data:`~...protocol.schema.PARAMS`: границы спинбоксов,
подписи, единицы и всплывающие пояснения берутся оттуда же, откуда их берёт
валидация. Добавить параметр в приложение — значит дописать строку в схему;
править UI при этом не нужно, и рассинхронизация границ невозможна.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QSpinBox,
    QWidget,
)

from ...protocol import PARAMS_BY_PATH, EnumParam, IntParam, get_value

#: Подписи вариантов для полей с фиксированным набором значений.
_CHOICE_LABELS = {
    "cw": "CW — по часовой стрелке",
    "ccw": "CCW — против часовой стрелки",
}


class ParamForm(QWidget):
    """Набор полей для перечисленных путей параметров."""

    #: Пользователь изменил любое поле формы.
    edited = pyqtSignal()

    def __init__(self, paths: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editors: dict[str, QSpinBox | QComboBox] = {}
        # Значения, полученные от устройства: по ним определяется, что изменилось.
        self._device_values: dict[str, Any] = {}

        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        for path in paths:
            param = PARAMS_BY_PATH[path]
            editor = self._create_editor(param)
            editor.setToolTip(param.description)
            self._editors[path] = editor

            label = f"{param.label}, {param.unit}" if getattr(param, "unit", "") else param.label
            layout.addRow(label, editor)

    def _create_editor(self, param: IntParam | EnumParam) -> QSpinBox | QComboBox:
        if isinstance(param, IntParam):
            spin = QSpinBox()
            spin.setRange(param.minimum, param.maximum)
            spin.setValue(param.default)
            spin.setAccelerated(True)
            spin.valueChanged.connect(self.edited)
            return spin

        combo = QComboBox()
        for choice in param.choices:
            combo.addItem(_CHOICE_LABELS.get(choice, choice), choice)
        combo.setCurrentIndex(combo.findData(param.default))
        combo.currentIndexChanged.connect(self.edited)
        return combo

    # ------------------------------------------------------------------
    # Обмен значениями
    # ------------------------------------------------------------------

    def load(self, config: dict[str, Any]) -> None:
        """Заполняет форму значениями из конфигурации устройства.

        Сигналы полей на время заполнения блокируются: иначе программная
        установка значений выглядела бы как правка пользователя и форма сразу
        помечалась бы изменённой.
        """
        for path, editor in self._editors.items():
            value = get_value(config, path)
            if value is None:
                continue

            self._device_values[path] = value
            editor.blockSignals(True)
            try:
                if isinstance(editor, QSpinBox):
                    editor.setValue(int(value))
                else:
                    index = editor.findData(value)
                    if index >= 0:
                        editor.setCurrentIndex(index)
            finally:
                editor.blockSignals(False)

    def values(self) -> dict[str, Any]:
        """Текущие значения полей по путям параметров."""
        return {
            path: (editor.value() if isinstance(editor, QSpinBox) else editor.currentData())
            for path, editor in self._editors.items()
        }

    def changed_values(self) -> dict[str, Any]:
        """Только те значения, что отличаются от прочитанных с устройства.

        Патч из изменённых полей короче полной конфигурации и не переписывает
        параметры, которые пользователь не трогал, — например, изменённые в этот
        момент из другой части интерфейса.
        """
        return {
            path: value
            for path, value in self.values().items()
            if self._device_values.get(path) != value
        }

    def has_changes(self) -> bool:
        return bool(self.changed_values())

    def set_enabled(self, enabled: bool) -> None:
        for editor in self._editors.values():
            editor.setEnabled(enabled)
