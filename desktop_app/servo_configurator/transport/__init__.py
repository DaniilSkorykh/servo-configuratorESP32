"""Транспортный слой: доставка байтов до устройства.

Реальный Serial-порт и симулятор реализуют один интерфейс, поэтому выбор между
режимами Real и Demo сводится к подстановке другого объекта.
"""

from .base import PortInfo, Transport
from .serial_port import DEFAULT_BAUDRATE, SerialTransport, available_ports
from .simulated import SIMULATED_PORT, FaultInjection, SimulatedTransport, simulated_port

__all__ = [
    "DEFAULT_BAUDRATE",
    "SIMULATED_PORT",
    "FaultInjection",
    "PortInfo",
    "SerialTransport",
    "SimulatedTransport",
    "Transport",
    "available_ports",
    "simulated_port",
]
