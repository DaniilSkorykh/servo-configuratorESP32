"""Модель сервопривода Feetech STS3215 для режима симуляции.

Модель воспроизводит не электромеханику привода, а те его свойства, от которых
зависит проверяемое поведение приложения:

* позиция меняется не мгновенно, а с ограниченной скоростью и ускорением —
  иначе графики телеметрии выродились бы в ступеньку;
* существуют механические упоры, при упирании в которые нагрузка растёт —
  без этого невозможно проверить Homing, который именно по росту нагрузки
  и определяет достижение упора;
* обратная связь зашумлена — иначе порог срабатывания по нагрузке проверялся бы
  на идеальных данных, каких на стенде не бывает.

Единицы совпадают с протоколом (раздел 4): позиция в шагах 0…4095,
скорость в шаг/с, нагрузка в 0.1 %.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum, auto

#: Полный диапазон энкодера STS3215.
ENCODER_MIN = 0
ENCODER_MAX = 4095


class MotionMode(Enum):
    """Внутренний режим движения модели."""

    HOLD = auto()
    """Момент удерживается, целевая позиция достигнута."""

    POSITION = auto()
    """Отработка целевой позиции."""

    WHEEL = auto()
    """Непрерывное вращение с заданной скоростью."""

    FREE = auto()
    """Момент снят, привод не сопротивляется внешнему воздействию."""


@dataclass(slots=True)
class ServoModel:
    """Состояние и поведение имитируемого привода.

    :param stop_min: положение нижнего механического упора. По умолчанию упор
        смещён внутрь диапазона энкодера: так проверяется, что Homing находит
        именно физический упор, а не край шкалы.
    """

    stop_min: float = 180.0
    stop_max: float = 3900.0
    position: float = 1500.0
    accel_steps_per_s2: float = 8000.0
    voltage_dv: int = 74
    temperature_c: int = 31
    seed: int = 20260828

    # --- внутреннее состояние ---
    mode: MotionMode = MotionMode.HOLD
    velocity: float = 0.0
    target_position: float = 1500.0
    commanded_speed: float = 1000.0
    load: float = 0.0
    stalled: bool = False
    _rng: random.Random | None = None

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self.position = self._clamp_to_stops(self.position)
        self.target_position = self.position

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    def move_to(self, position: float, speed: float) -> None:
        """Переводит привод в позиционный режим."""
        self.mode = MotionMode.POSITION
        self.target_position = position
        self.commanded_speed = max(1.0, abs(speed))

    def run(self, speed: float) -> None:
        """Включает непрерывное вращение; знак ``speed`` задаёт направление."""
        self.mode = MotionMode.WHEEL
        self.commanded_speed = speed

    def stop(self, *, release_torque: bool = False) -> None:
        """Останавливает движение; при ``release_torque`` снимает момент."""
        self.mode = MotionMode.FREE if release_torque else MotionMode.HOLD
        self.velocity = 0.0
        self.commanded_speed = 0.0
        self.target_position = self.position
        self.stalled = False

    def set_position_counter(self, position: float) -> None:
        """Назначает текущему физическому положению новое значение счётчика.

        Соответствует калибровке смещения в приводе: после Homing упор получает
        координату ``homing.zero_position``, и вместе с ним сдвигается вся шкала,
        включая механические упоры.
        """
        offset = position - self.position
        self.position += offset
        self.stop_min += offset
        self.stop_max += offset
        self.target_position = self.position

    # ------------------------------------------------------------------
    # Интегрирование
    # ------------------------------------------------------------------

    def update(self, dt: float) -> None:
        """Продвигает модель на ``dt`` секунд."""
        if dt <= 0.0:
            return

        desired = self._desired_velocity()
        self.velocity = self._approach(self.velocity, desired, self.accel_steps_per_s2 * dt)

        moved = self.position + self.velocity * dt
        self.stalled = self._hits_stop(moved)

        if self.stalled:
            # В упоре привод продолжает создавать момент, но не перемещается.
            self.position = self._clamp_to_stops(moved)
            self.velocity = 0.0
        else:
            self.position = moved

        self._update_load(dt, desired)

    def _desired_velocity(self) -> float:
        match self.mode:
            case MotionMode.WHEEL:
                return self.commanded_speed
            case MotionMode.POSITION:
                error = self.target_position - self.position
                if abs(error) <= _POSITION_TOLERANCE:
                    self.mode = MotionMode.HOLD
                    return 0.0
                # Скорость подхода ограничивается на финальном участке, иначе
                # привод проскакивал бы цель за один шаг интегрирования.
                approach = abs(error) / _APPROACH_TIME
                return min(self.commanded_speed, approach) * (1.0 if error > 0 else -1.0)
            case _:
                return 0.0

    def _update_load(self, dt: float, desired_velocity: float) -> None:
        if self.mode is MotionMode.FREE:
            target_load = 0.0
        elif self.stalled:
            # Упор: момент нарастает до предела за характерное время.
            target_load = _STALL_LOAD
        elif desired_velocity != 0.0:
            # Движение: нагрузка примерно пропорциональна скорости.
            target_load = _MOVING_LOAD_BASE + abs(self.velocity) * _LOAD_PER_STEP
        else:
            target_load = _HOLDING_LOAD

        rate = _LOAD_RISE_RATE if target_load > self.load else _LOAD_FALL_RATE
        self.load = self._approach(self.load, target_load, rate * dt)

    # ------------------------------------------------------------------
    # Обратная связь
    # ------------------------------------------------------------------

    def feedback(self) -> dict[str, int | bool]:
        """Возвращает показания в единицах регистров привода.

        Шум добавляется только к измеряемым величинам: так проверяется, что
        приложение не рассчитывает на идеально стабильные значения.
        """
        rng = self._rng
        assert rng is not None

        load = self.load + rng.gauss(0.0, _LOAD_NOISE)
        signed_load = round(load) * (1 if self.velocity >= 0 else -1)

        return {
            "pos": round(self.position) + rng.choice((-1, 0, 0, 0, 1)),
            "spd": round(self.velocity),
            "load": max(-1000, min(1000, signed_load)),
            "volt": self.voltage_dv + rng.choice((-1, 0, 0, 1)),
            "temp": self.temperature_c,
            "cur": round(abs(load) * _CURRENT_PER_LOAD),
            "moving": abs(self.velocity) > _MOVING_THRESHOLD,
        }

    @property
    def measured_load(self) -> float:
        """Модуль нагрузки без знака — по нему Homing определяет упор."""
        return abs(self.load)

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    def _hits_stop(self, position: float) -> bool:
        return position <= self.stop_min or position >= self.stop_max

    def _clamp_to_stops(self, position: float) -> float:
        return max(self.stop_min, min(self.stop_max, position))

    @staticmethod
    def _approach(current: float, target: float, max_delta: float) -> float:
        """Сдвигает ``current`` к ``target`` не более чем на ``max_delta``."""
        delta = target - current
        if abs(delta) <= max_delta:
            return target
        return current + max_delta * (1.0 if delta > 0 else -1.0)


#: Допуск позиционирования: привод считает цель достигнутой, шаг.
_POSITION_TOLERANCE = 3.0

#: Характерное время финального подхода к цели, с.
_APPROACH_TIME = 0.15

#: Нагрузка в упоре, 0.1 %.
_STALL_LOAD = 900.0

#: Нагрузка удержания без движения, 0.1 %.
_HOLDING_LOAD = 40.0

#: Постоянная составляющая нагрузки при движении, 0.1 %.
_MOVING_LOAD_BASE = 60.0

#: Прирост нагрузки на единицу скорости, 0.1 % на шаг/с.
_LOAD_PER_STEP = 0.05

#: Скорости нарастания и спада нагрузки, 0.1 % в секунду.
_LOAD_RISE_RATE = 2500.0
_LOAD_FALL_RATE = 1200.0

#: Среднеквадратичный шум измерения нагрузки, 0.1 %.
_LOAD_NOISE = 8.0

#: Пересчёт нагрузки в ток (единицы 6.5 мА) — грубая пропорция для правдоподобия.
_CURRENT_PER_LOAD = 0.4

#: Порог, ниже которого привод считает себя неподвижным, шаг/с.
_MOVING_THRESHOLD = 5.0
