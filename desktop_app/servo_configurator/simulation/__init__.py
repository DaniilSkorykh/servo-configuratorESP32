"""Режим симуляции: модель привода и эмуляция прошивки ESP32.

Позволяет пройти весь сценарий работы приложения без физического оборудования
(раздел 8 задания) и служит эталоном поведения при переносе логики в прошивку.
"""

from .firmware import DEVICE_NAME, FIRMWARE_VERSION, SimulatedFirmware
from .nvs import SimulatedNvs, default_nvs_path
from .servo_model import ENCODER_MAX, ENCODER_MIN, MotionMode, ServoModel

__all__ = [
    "DEVICE_NAME",
    "ENCODER_MAX",
    "ENCODER_MIN",
    "FIRMWARE_VERSION",
    "MotionMode",
    "ServoModel",
    "SimulatedFirmware",
    "SimulatedNvs",
    "default_nvs_path",
]
