"""Абстракция транспорта — граница, за которой скрыт способ доставки байтов.

Выше по стеку код работает только с этим интерфейсом и не знает, идут ли байты
в реальный COM-порт или в симулятор. Именно поэтому переключение Real / Demo
не затрагивает ни логику приложения, ни UI: подменяется одна реализация.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PortInfo:
    """Описание доступного порта для выпадающего списка в UI."""

    device: str
    """Системное имя: ``COM3``, ``/dev/ttyUSB0``."""

    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None

    @property
    def is_likely_esp32(self) -> bool:
        """Похож ли порт на плату с USB-UART мостом ESP32.

        Waveshare Servo Driver собран на распространённых мостах CP210x, CH34x и
        FTDI. Совпадение по VID — подсказка для автовыбора порта, а не гарантия:
        окончательную проверку даёт handshake командой ``ping``.
        """
        return self.vid in _BRIDGE_VENDOR_IDS

    @property
    def label(self) -> str:
        """Строка для отображения пользователю."""
        return f"{self.device} — {self.description}" if self.description else self.device


#: VID распространённых USB-UART мостов: Silicon Labs, WCH, FTDI, Espressif.
_BRIDGE_VENDOR_IDS = frozenset({0x10C4, 0x1A86, 0x0403, 0x303A})


class Transport(ABC):
    """Двусторонний байтовый канал до устройства."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """Открыт ли канал."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Короткое описание канала для строки состояния."""

    @abstractmethod
    def open(self) -> None:
        """Открывает канал.

        :raises TransportError: канал недоступен (порт занят, не существует,
            недостаточно прав).
        """

    @abstractmethod
    def close(self) -> None:
        """Закрывает канал. Повторный вызов безопасен."""

    @abstractmethod
    def read(self, timeout: float) -> bytes:
        """Читает доступные байты, ожидая не дольше ``timeout`` секунд.

        Возвращает пустую строку байтов, если за отведённое время ничего не
        пришло: это штатная ситуация, а не ошибка.

        :raises TransportError: канал оборвался (устройство отключено физически).
        """

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Отправляет байты устройству.

        :raises TransportError: запись не удалась.
        """

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
