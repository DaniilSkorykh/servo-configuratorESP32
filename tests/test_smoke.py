"""Проверка, что каркас проекта импортируется и окружение собрано корректно."""


def test_package_importable():
    import servo_configurator

    assert servo_configurator.__version__


def test_dependencies_available():
    import pyqtgraph  # noqa: F401
    import serial  # noqa: F401
    from PyQt6 import QtCore  # noqa: F401
