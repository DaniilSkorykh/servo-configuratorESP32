"""Транспорт-симулятор: канал до эмулируемой прошивки вместо реального порта.

Реализует тот же интерфейс :class:`~.base.Transport`, что и Serial-порт, поэтому
переключение Real / Demo не требует изменений ни в одном слое выше транспорта.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..protocol import LinkError, LineAssembler, TransportError
from ..simulation.firmware import SimulatedFirmware
from ..simulation.nvs import SimulatedNvs, default_nvs_path
from .base import PortInfo, Transport

logger = logging.getLogger(__name__)

#: Имя виртуального порта в выпадающем списке UI.
SIMULATED_PORT = "DEMO"

#: Шаг внутреннего цикла ожидания, с. Достаточно мелкий, чтобы телеметрия с
#: периодом 20 мс выходила без заметного дрожания, и достаточно крупный, чтобы
#: ожидание не превращалось в активный опрос.
_TICK_INTERVAL = 0.005


def simulated_port() -> PortInfo:
    """Описание виртуального порта для списка портов."""
    return PortInfo(device=SIMULATED_PORT, description="Симулятор устройства (Demo)")


@dataclass
class FaultInjection:
    """Управление имитацией неисправностей.

    Позволяет проверить обработку ошибок из раздела 7 задания, не выдёргивая
    физический кабель: сценарии воспроизводятся кнопкой в UI и в тестах.
    """

    link_broken: bool = False
    """Разрыв связи: чтение и запись завершаются ошибкой, как при отключении USB."""

    silent: bool = False
    """Устройство «молчит»: канал исправен, но ответы не приходят."""

    corrupt_frames: int = 0
    """Сколько ближайших кадров исказить, чтобы проверить устойчивость разбора."""


class SimulatedTransport(Transport):
    """Канал до :class:`~..simulation.firmware.SimulatedFirmware`.

    :param persist: сохранять ли конфигурацию между запусками приложения.
        При ``True`` перезапуск приложения играет роль перезагрузки ESP32 и
        позволяет проверить пункт 17 сценария приёмки.
    :param nvs_path: файл хранилища; по умолчанию — общий для приложения.
        Учитывается только при ``persist``.
    :param clock: источник времени в секундах; подменяется в тестах.
    """

    def __init__(
        self,
        *,
        persist: bool = True,
        nvs_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        firmware: SimulatedFirmware | None = None,
    ) -> None:
        self._clock = clock
        storage = (nvs_path or default_nvs_path()) if persist else None
        self._nvs = SimulatedNvs(storage)
        self._firmware = firmware or SimulatedFirmware(
            load_config=self._nvs.load,
            save_config=self._nvs.save,
        )

        self.faults = FaultInjection()

        self._assembler = LineAssembler()
        self._outgoing = bytearray()
        self._lock = threading.Lock()
        self._is_open = False
        self._started_at = 0.0

    @property
    def firmware(self) -> SimulatedFirmware:
        """Эмулируемая прошивка — для тестов и отладочных сценариев."""
        return self._firmware

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def description(self) -> str:
        return "Симулятор устройства (Demo)"

    def open(self) -> None:
        if self._is_open:
            return
        if self.faults.link_broken:
            raise TransportError("симулятор: канал разорван", LinkError.PORT)

        self._started_at = self._clock()
        self._assembler.reset()
        with self._lock:
            self._outgoing.clear()
            self._append(self._firmware.boot(now_ms=0))
        self._is_open = True
        logger.info("симулятор запущен")

    def close(self) -> None:
        self._is_open = False
        with self._lock:
            self._outgoing.clear()
        logger.info("симулятор остановлен")

    def read(self, timeout: float) -> bytes:
        self._require_open()
        deadline = self._clock() + timeout

        while True:
            self._advance()

            with self._lock:
                if self._outgoing:
                    data = bytes(self._outgoing)
                    self._outgoing.clear()
                    return data

            remaining = deadline - self._clock()
            if remaining <= 0:
                return b""
            time.sleep(min(_TICK_INTERVAL, remaining))

    def write(self, data: bytes) -> None:
        self._require_open()
        self._advance()

        for line in self._assembler.feed(data):
            with self._lock:
                self._append(self._firmware.handle_line(line))

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _require_open(self) -> None:
        if not self._is_open:
            raise TransportError("симулятор не запущен", LinkError.DISCONNECTED)
        if self.faults.link_broken:
            self._is_open = False
            raise TransportError("симулятор: связь потеряна", LinkError.DISCONNECTED)

    def _advance(self) -> None:
        """Продвигает время прошивки и складывает её сообщения в буфер выдачи."""
        now_ms = int((self._clock() - self._started_at) * 1000)
        with self._lock:
            self._append(self._firmware.tick(now_ms))

    def _append(self, messages: list[str]) -> None:
        """Добавляет сообщения в буфер, применяя имитацию неисправностей.

        Вызывается под захваченным замком.
        """
        if self.faults.silent:
            return

        for message in messages:
            if self.faults.corrupt_frames > 0:
                self.faults.corrupt_frames -= 1
                # Обрезанный кадр — то, что приходит при потере байтов в линии.
                message = message[: max(1, len(message) // 2)]
            self._outgoing.extend(message.encode("utf-8") + b"\n")
