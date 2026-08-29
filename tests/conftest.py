"""Общая настройка тестов.

Тесты интерфейса выполняются в режиме ``offscreen``: окна не показываются, но
виджеты создаются и обрабатывают события по-настоящему. Это позволяет проверять
UI в обычном прогоне тестов и в отсутствие графической подсистемы.
"""

from __future__ import annotations

import os
import time

import pytest

# Платформу нужно выбрать до создания QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_nvs(tmp_path, monkeypatch):
    """Изолирует хранилище конфигурации симулятора на время теста.

    Приложение хранит конфигурацию симулятора в каталоге временных файлов
    пользователя. Без подмены тесты писали бы в этот общий файл: прогон менял
    бы настройки пользователя и, что хуже, следующий прогон начинался бы не с
    заводских значений, а с оставленных предыдущим — тест переставал проходить
    без единого изменения в коде.
    """
    storage = tmp_path / "sim_nvs.json"

    # Патчатся оба имени: модуль-владелец и место, куда путь импортирован.
    monkeypatch.setattr("servo_configurator.simulation.nvs.default_nvs_path",
                        lambda: storage)
    monkeypatch.setattr("servo_configurator.transport.simulated.default_nvs_path",
                        lambda: storage)
    return storage


@pytest.fixture(scope="session")
def qapp():
    """Единственный на прогон экземпляр QApplication."""
    from PyQt6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


@pytest.fixture
def pump(qapp):
    """Прокручивает цикл событий Qt.

    Сигналы из рабочих потоков доставляются только при обработке событий,
    поэтому после каждой команды устройству нужно дать очереди разобраться.
    """
    def _pump(seconds: float = 0.3, *, until=None) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            qapp.processEvents()
            if until is not None and until():
                return True
            time.sleep(0.01)
        qapp.processEvents()
        return until() if until is not None else True

    return _pump
