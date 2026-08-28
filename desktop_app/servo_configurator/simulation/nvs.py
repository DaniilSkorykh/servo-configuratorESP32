"""Эмуляция энергонезависимой памяти ESP32 (NVS / Preferences).

Конфигурация симулятора хранится в файле, а не в оперативной памяти процесса.
Это сделано ради пункта 17 сценария приёмки — «перезапустить ESP32 и убедиться,
что сохранённая конфигурация читается обратно»: в Demo-режиме перезапуск
приложения играет роль перезагрузки контроллера, и проверка остаётся настоящей.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def default_nvs_path() -> Path:
    """Путь к файлу эмулируемой NVS в каталоге временных файлов пользователя."""
    return Path(tempfile.gettempdir()) / "servo_configurator" / "sim_nvs.json"


class SimulatedNvs:
    """Хранилище конфигурации симулятора с семантикой NVS.

    :param path: файл хранилища; ``None`` — хранить только в памяти
        (используется в тестах, чтобы прогоны не влияли друг на друга).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._memory: dict[str, Any] | None = None

    def load(self) -> dict[str, Any] | None:
        """Читает сохранённую конфигурацию; ``None``, если её нет или она повреждена."""
        if self._path is None:
            return self._memory

        try:
            with self._path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            # Повреждённое хранилище равносильно его отсутствию: прошивка в этом
            # случае поднимется на значениях по умолчанию, а не откажет в старте.
            logger.warning("хранилище %s повреждено, используются значения по умолчанию: %s",
                           self._path, exc)
            return None

        return data if isinstance(data, dict) else None

    def save(self, config: dict[str, Any]) -> None:
        """Сохраняет конфигурацию.

        :raises OSError: запись не удалась — прошивка ответит ``E_NVS``.
        """
        if self._path is None:
            self._memory = json.loads(json.dumps(config))
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Запись во временный файл с последующей заменой: обрыв на середине не
        # оставит наполовину записанный конфиг, как это делает настоящая NVS.
        temporary = self._path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self._path)

    def clear(self) -> None:
        """Стирает хранилище."""
        self._memory = None
        if self._path is not None:
            self._path.unlink(missing_ok=True)
