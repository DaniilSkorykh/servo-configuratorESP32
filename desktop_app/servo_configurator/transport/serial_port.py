"""Транспорт поверх реального Serial/COM-порта (pyserial)."""

from __future__ import annotations

import logging

import serial
from serial.tools import list_ports

from ..protocol import LinkError, TransportError
from .base import PortInfo, Transport

logger = logging.getLogger(__name__)

#: Скорость обмена ПК ↔ ESP32 (раздел 1 протокола, download UART платы Waveshare).
DEFAULT_BAUDRATE = 115200


def available_ports() -> list[PortInfo]:
    """Возвращает список доступных портов, вероятные платы — первыми.

    Сортировка ставит наверх порты с VID известных USB-UART мостов: в типичной
    системе это избавляет от выбора среди виртуальных COM-портов Bluetooth.
    """
    ports = [
        PortInfo(
            device=port.device,
            description=port.description or "",
            hwid=port.hwid or "",
            vid=port.vid,
            pid=port.pid,
        )
        for port in list_ports.comports()
    ]
    return sorted(ports, key=lambda p: (not p.is_likely_esp32, p.device))


class SerialTransport(Transport):
    """Канал до устройства через физический COM-порт."""

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        self._port = port
        self._baudrate = baudrate
        self._serial: serial.Serial | None = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def description(self) -> str:
        return f"{self._port} @ {self._baudrate}"

    def open(self) -> None:
        if self.is_open:
            return
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0,          # неблокирующее чтение, ожидание — в read()
                write_timeout=1.0,  # запись не должна подвешивать поток навсегда
            )
        except (serial.SerialException, OSError) as exc:
            raise TransportError(
                f"не удалось открыть порт {self._port}: {exc}", LinkError.PORT
            ) from exc

        # После открытия порта ESP32 может перезагрузиться по DTR/RTS, и в буфере
        # оседают загрузочные сообщения. Сбрасываем их, чтобы сборщик кадров не
        # начинал работу с заведомого мусора.
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except (serial.SerialException, OSError):
            logger.debug("не удалось очистить буферы порта %s", self._port)

        logger.info("порт %s открыт на %d бод", self._port, self._baudrate)

    def close(self) -> None:
        if self._serial is None:
            return
        try:
            self._serial.close()
        except (serial.SerialException, OSError) as exc:
            # Закрытие уже отсутствующего устройства не должно мешать приложению
            # корректно перейти в отключённое состояние.
            logger.debug("ошибка при закрытии порта %s: %s", self._port, exc)
        finally:
            self._serial = None
            logger.info("порт %s закрыт", self._port)

    def read(self, timeout: float) -> bytes:
        port = self._require_open()
        try:
            # read(1) с таймаутом ждёт первый байт, не сжигая процессор на опросе;
            # затем разом забираем всё, что уже пришло следом.
            port.timeout = timeout
            first = port.read(1)
            if not first:
                return b""
            waiting = port.in_waiting
            return first + port.read(waiting) if waiting else first
        except (serial.SerialException, OSError, TypeError) as exc:
            # TypeError возникает, когда pyserial обращается к дескриптору
            # исчезнувшего устройства — на Windows это обычный исход выдёргивания USB.
            raise TransportError(
                f"связь с {self._port} потеряна: {exc}", LinkError.DISCONNECTED
            ) from exc

    def write(self, data: bytes) -> None:
        port = self._require_open()
        try:
            port.write(data)
        except (serial.SerialTimeoutException, serial.SerialException, OSError) as exc:
            raise TransportError(
                f"не удалось записать в {self._port}: {exc}", LinkError.DISCONNECTED
            ) from exc

    def _require_open(self) -> serial.Serial:
        if self._serial is None or not self._serial.is_open:
            raise TransportError(f"порт {self._port} не открыт", LinkError.DISCONNECTED)
        return self._serial
