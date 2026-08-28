"""Точка входа desktop-приложения.

На этапе 0 — проверка каркаса и окружения. Реальное окно появится на этапе 3.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Пакет лежит рядом с этим файлом, поэтому запуск возможен как `python desktop_app/main.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from servo_configurator import __version__  # noqa: E402


def main() -> int:
    print(f"servo_configurator {__version__} — каркас проекта готов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
