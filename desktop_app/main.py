"""Точка входа desktop-приложения.

Запуск::

    python desktop_app/main.py

Приложение работает без оборудования: в списке портов первым идёт вариант
``DEMO`` — встроенный симулятор устройства.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from pathlib import Path

# Пакет лежит рядом с этим файлом, поэтому запуск возможен как из корня
# репозитория, так и из каталога desktop_app.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtWidgets import QApplication

from servo_configurator import __version__
from servo_configurator.protocol import Direction
from servo_configurator.ui.main_window import MainWindow

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(verbose: bool, log_file: Path | None) -> None:
    """Настраивает вывод журнала в консоль и, при необходимости, в файл.

    Потоки вывода переводятся в UTF-8: консоль Windows по умолчанию работает в
    однобайтовой кодировке, в которой нет ни тире, ни стрелок из сообщений, и
    logging печатает «Logging error» вместо текста. Режим ``replace`` оставляет
    сообщение читаемым даже там, где часть символов не отображается.
    """
    for stream in (sys.stdout, sys.stderr):
        # Поток может быть подменён или не поддерживать перенастройку —
        # это не повод прерывать запуск.
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")

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
    parser.add_argument("--self-test", action="store_true",
                        help="проверить работоспособность на симуляторе и выйти")
    return parser.parse_args(argv[1:])


def run_self_test(application: QApplication, window: MainWindow) -> int:
    """Подключается к симулятору, проверяет обмен и завершает работу.

    Нужен, чтобы убедиться в работоспособности установленной сборки, не
    подключая оборудование: упакованному приложению может не хватить
    библиотеки, о чём обычный запуск сообщит лишь пустым окном.
    """
    import time

    checks: list[tuple[str, bool]] = []

    def wait(seconds: float, condition) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            application.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return False

    window.connection_panel.connect_requested.emit(None)
    connected = wait(10.0, lambda: window.manual_panel.move_button.isEnabled())
    checks.append(("подключение к симулятору", connected))

    if connected:
        checks.append((
            "конфигурация прочитана",
            wait(5.0, lambda: window.homing_panel.form.values()["homing.speed"] > 0),
        ))
        window.service.motor_run(Direction.CCW, 500)
        checks.append((
            "телеметрия поступает",
            wait(5.0, lambda: window.telemetry_panel._values["pos"].text() not in ("", "—")),
        ))
        checks.append((
            "графики наполняются",
            wait(5.0, lambda: len(window.charts_panel._buffers["pos"].data()[0]) > 20),
        ))
        window.service.stop()
        wait(1.0, lambda: False)

    window.service.shutdown()
    window.close()

    for name, passed in checks:
        print(f"[{'ok' if passed else 'СБОЙ'}] {name}")

    failed = [name for name, passed in checks if not passed]
    print("Самопроверка пройдена" if not failed else f"Не пройдено: {', '.join(failed)}")
    return 0 if not failed else 1


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    logging.getLogger(__name__).info("запуск конфигуратора %s", __version__)

    application = QApplication(argv)
    application.setApplicationName("Servo Configurator")
    application.setApplicationVersion(__version__)

    window = MainWindow()

    if args.self_test:
        return run_self_test(application, window)

    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
