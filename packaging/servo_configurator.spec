# -*- mode: python ; coding: utf-8 -*-
"""Сборка приложения в один исполняемый файл (PyInstaller).

Запуск из корня репозитория::

    pyinstaller packaging/servo_configurator.spec

Результат: ``dist/servo-configurator.exe`` — запускается без установки Python.
"""

from pathlib import Path

# SPECPATH задаёт PyInstaller; путь к пакету считается от корня репозитория.
ROOT = Path(SPECPATH).resolve().parent
APP_DIR = ROOT / "desktop_app"

analysis = Analysis(
    [str(APP_DIR / "main.py")],
    pathex=[str(APP_DIR)],
    binaries=[],
    datas=[],
    # Модули, которые PyInstaller не находит сам: они подгружаются по имени
    # внутри pyqtgraph и pyserial, а не обычным импортом.
    hiddenimports=[
        "serial.tools.list_ports",
        "pyqtgraph.graphicsItems.PlotItem",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Не нужны приложению и заметно утяжеляют сборку.
    excludes=[
        "matplotlib",
        "scipy",
        "pandas",
        "tkinter",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtBluetooth",
        "PyQt6.QtQuick",
        "PyQt6.QtQml",
        "PyQt6.Qt3DCore",
        "PyQt6.QtMultimedia",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="servo-configurator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Консоль оставлена намеренно: в неё идёт журнал, а он нужен при разборе
    # неполадок связи на стенде.
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
