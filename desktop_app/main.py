"""Точка входа desktop-приложения.

Запуск::

    python desktop_app/main.py

Приложение работает без оборудования: в списке портов первым идёт вариант
``DEMO`` — встроенный симулятор устройства.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Пакет лежит рядом с этим файлом, поэтому запуск возможен как из корня
# репозитория, так и из каталога desktop_app.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from servo_configurator import __version__  # noqa: E402
from servo_configurator.ui.main_window import MainWindow  # noqa: E402

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(verbose: bool, log_file: Path | None) -> None:
    """Настраивает вывод журнала в консоль и, при необходимости, в файл."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        handlers=handlers,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Конфигуратор сервопривода Feetech STS3215 (Waveshare ESP32)"
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="подробный журнал")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="дополнительно писать журнал в файл")
    return parser.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    logging.getLogger(__name__).info("запуск конфигуратора %s", __version__)

    application = QApplication(argv)
    application.setApplicationName("Servo Configurator")
    application.setApplicationVersion(__version__)

    window = MainWindow()
    window.show()

    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
