"""Графики телеметрии в реальном времени.

Ключевое решение — разделить приём данных и перерисовку. Кадры телеметрии
складываются в кольцевые буферы по мере поступления, а графики обновляются по
таймеру с фиксированной частотой. Если рисовать на каждый кадр, при частой
телеметрии интерфейс начинает подтормаживать, а при редкой — обновляется
рывками; здесь же нагрузка на отрисовку не зависит от периода телеметрии.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...protocol import Telemetry

#: Частота перерисовки, кадров в секунду.
_REFRESH_FPS = 20

#: Ёмкость буфера. При телеметрии 20 Гц её хватает на пять минут записи —
#: с запасом к максимальному окну отображения.
_BUFFER_CAPACITY = 6000

#: Цвета кривых: различимы и в светлой, и в тёмной теме.
_POSITION_COLOR = "#1f77b4"
_SPEED_COLOR = "#2ca02c"
_LOAD_COLOR = "#d62728"


class _RingBuffer:
    """Кольцевой буфер точек графика.

    Массив фиксированного размера вместо списка с обрезкой: при 20 Гц за час
    работы список пришлось бы постоянно перевыделять, а здесь память выделяется
    один раз.
    """

    def __init__(self, capacity: int = _BUFFER_CAPACITY) -> None:
        self._time = np.zeros(capacity, dtype=np.float64)
        self._value = np.zeros(capacity, dtype=np.float64)
        self._capacity = capacity
        self._size = 0
        self._head = 0

    def append(self, time_s: float, value: float) -> None:
        self._time[self._head] = time_s
        self._value[self._head] = value
        self._head = (self._head + 1) % self._capacity
        self._size = min(self._size + 1, self._capacity)

    def clear(self) -> None:
        self._size = 0
        self._head = 0

    def data(self) -> tuple[np.ndarray, np.ndarray]:
        """Точки в хронологическом порядке."""
        if self._size < self._capacity:
            return self._time[: self._size], self._value[: self._size]
        # Буфер заполнен: начало данных — сразу за позицией записи.
        order = np.r_[self._head : self._capacity, 0 : self._head]
        return self._time[order], self._value[order]


class ChartsPanel(QGroupBox):
    """Три графика телеметрии против времени (п. 4.7 задания)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Графики", parent)

        pg.setConfigOptions(antialias=True)

        self._buffers = {
            "pos": _RingBuffer(),
            "spd": _RingBuffer(),
            "load": _RingBuffer(),
        }
        self._start_time: float | None = None
        self._latest_time = 0.0
        self._dirty = False

        self.window_spin = QSpinBox()
        self.window_spin.setRange(5, 300)
        self.window_spin.setValue(30)
        self.window_spin.setSuffix(" с")
        self.window_spin.setToolTip("Ширина отображаемого окна времени")

        self.autoscroll_check = QCheckBox("Следить за текущим моментом")
        self.autoscroll_check.setChecked(True)
        self.autoscroll_check.setToolTip(
            "Снимите, чтобы рассмотреть предыдущий участок записи"
        )

        self.clear_button = QPushButton("Очистить")
        self.clear_button.clicked.connect(self.clear)

        controls = QHBoxLayout()
        controls.addWidget(self.window_spin)
        controls.addWidget(self.autoscroll_check)
        controls.addWidget(self.clear_button)
        controls.addStretch(1)

        self._layout_widget = pg.GraphicsLayoutWidget()
        self._layout_widget.setMinimumHeight(360)

        self._curves = {}
        self._plots = {}
        specs = [
            ("pos", "Позиция", "шаг", _POSITION_COLOR),
            ("spd", "Скорость", "шаг/с", _SPEED_COLOR),
            ("load", "Нагрузка", "0.1 %", _LOAD_COLOR),
        ]

        previous = None
        for row, (key, title, unit, color) in enumerate(specs):
            plot = self._layout_widget.addPlot(row=row, col=0, title=title)
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("left", title, units=unit)
            plot.setMenuEnabled(False)
            # Без этого pyqtgraph переводит шкалу в «кшаг» и показывает 0…3
            # вместо 0…3200: для позиции в шагах такая запись только мешает.
            plot.getAxis("left").enableAutoSIPrefix(False)
            plot.getAxis("bottom").enableAutoSIPrefix(False)
            if previous is not None:
                # Общая ось времени: прокрутка одного графика двигает остальные.
                plot.setXLink(previous)
            if row == len(specs) - 1:
                plot.setLabel("bottom", "Время", units="с")

            self._curves[key] = plot.plot(pen=pg.mkPen(color, width=2))
            self._plots[key] = plot
            previous = plot

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self._layout_widget, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000 // _REFRESH_FPS)

    # ------------------------------------------------------------------

    def add_telemetry(self, frame: Telemetry) -> None:
        """Принимает кадр; перерисовка произойдёт по таймеру.

        Время берётся из отметки устройства: она равномернее момента получения
        кадра приложением, на который влияют задержки буферизации USB.
        """
        timestamp = frame.ts / 1000.0
        if self._start_time is None:
            self._start_time = timestamp

        elapsed = timestamp - self._start_time
        # Перезагрузка контроллера обнуляет его счётчик времени: продолжать
        # рисовать в прошлое нельзя, запись начинается заново.
        if elapsed < self._latest_time:
            self.clear()
            self._start_time = timestamp
            elapsed = 0.0

        self._latest_time = elapsed
        self._buffers["pos"].append(elapsed, frame.pos)
        self._buffers["spd"].append(elapsed, frame.spd)
        self._buffers["load"].append(elapsed, frame.load)
        self._dirty = True

    def clear(self) -> None:
        """Стирает накопленные данные."""
        for buffer in self._buffers.values():
            buffer.clear()
        self._start_time = None
        self._latest_time = 0.0
        self._dirty = True

    def _refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False

        for key, curve in self._curves.items():
            times, values = self._buffers[key].data()
            curve.setData(times, values)

        if self.autoscroll_check.isChecked():
            window = float(self.window_spin.value())
            left = max(0.0, self._latest_time - window)
            # Достаточно сдвинуть один график: остальные связаны по оси X.
            self._plots["pos"].setXRange(left, max(window, self._latest_time),
                                         padding=0)
