"""Консольный прогон сценария приёмки через симулятор.

Позволяет проверить протокол, логику устройства и обработку ошибок без UI и без
оборудования. Запуск::

    python desktop_app/demo_cli.py

Шаги повторяют раздел 11 задания в той части, что не требует графического
интерфейса. Сценарий с реальной платой запускается тем же кодом — достаточно
указать порт: ``python desktop_app/demo_cli.py COM3``.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from servo_configurator.device import ServoDevice
from servo_configurator.protocol import (
    CommandError,
    CommandTimeout,
    DeviceState,
    Direction,
    Notification,
    Telemetry,
    TransportError,
    describe,
)
from servo_configurator.transport import (
    SerialTransport,
    SimulatedTransport,
)

_HOMING_WAIT_S = 20.0
_MOVE_WAIT_S = 8.0


def _use_utf8_console() -> None:
    """Переводит вывод в UTF-8.

    Консоль Windows по умолчанию работает в однобайтовой кодировке, в которой
    нет ни тире, ни стрелок из сообщений сценария.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


class ScenarioRunner:
    """Последовательный прогон сценария с печатью результатов."""

    def __init__(self) -> None:
        #: Заполняется после создания устройства: обработчики событий нужны
        #: конструктору ServoDevice, а сам он — этому объекту.
        self.device: ServoDevice | None = None
        self.transport = None
        self.homing_result: dict | None = None
        self.frames = 0

    # --- обработчики событий устройства ---

    def on_telemetry(self, frame: Telemetry) -> None:
        self.frames += 1
        if self.frames % 10 == 1:
            print(f"      pos={frame.pos:5d}  spd={frame.spd:5d}  load={frame.load:4d}  "
                  f"state={frame.state}")

    def on_event(self, notification: Notification) -> None:
        if notification.evt == "homing":
            self.homing_result = notification.data
        print(f"      событие {notification.evt}: {notification.data}")

    def on_link_lost(self, code: str, message: str) -> None:
        print(f"      СВЯЗЬ ПОТЕРЯНА: {describe(code)} — {message}")

    # --- шаги сценария ---

    def wait_for_homing(self, timeout: float = _HOMING_WAIT_S) -> dict:
        deadline = time.monotonic() + timeout
        while self.homing_result is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.homing_result is None:
            raise TimeoutError("устройство не сообщило результат Homing")
        return self.homing_result

    def wait_until_stopped(self, timeout: float = _MOVE_WAIT_S) -> None:
        """Ждёт завершения перемещения.

        Кадры, полученные до команды, игнорируются по номеру ``seq``: сразу
        после отправки привод ещё не тронулся, и последний известный кадр
        сообщает ``moving: false``. Без этой отсечки ожидание завершалось бы
        мгновенно, не дождавшись движения.
        """
        assert self.device is not None
        frame = self.device.last_telemetry
        baseline = frame.seq if frame is not None else -1

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.device.last_telemetry
            if (frame is not None and frame.seq != baseline
                    and not frame.moving
                    and frame.state is not DeviceState.POSITION):
                return
            time.sleep(0.05)


def step(number: int, title: str) -> None:
    print(f"\n[{number:2d}] {title}")


def run(runner: ScenarioRunner) -> None:
    device = runner.device
    assert device is not None

    step(3, "Подключение и handshake")
    info = device.connect()
    print(f"      устройство: {info['dev']}, прошивка {info['fw']}, протокол {info['proto']}")

    device.set_telemetry(True, period_ms=100)

    step(4, "Чтение текущей конфигурации")
    config, dirty = device.read_config()
    print(f"      homing: {config['homing']}")
    print(f"      несохранённые изменения: {dirty}")

    step(5, "Изменение параметров Homing")
    patch = {"homing": {"dir": "cw", "speed": 400, "load_threshold": 500}}
    device.write_config(patch)
    print(f"      применено: {patch['homing']}")

    step(6, "Запись параметров в энергонезависимую память")
    device.save_config()
    _, dirty = device.read_config()
    print(f"      сохранено, несохранённые изменения: {dirty}")

    step(7, "Запуск Homing")
    device.start_homing()
    result = runner.wait_for_homing()
    print(f"      результат: {result['result']}, позиция {result['pos']}, "
          f"время {result['elapsed_ms']} мс")

    step(8, "Установка рабочих параметров")
    device.write_config({"operating": {"speed": 1200, "load_limit": 700}})
    print("      скорость 1200 шаг/с, ограничение нагрузки 700")

    step(9, "Задание рабочего диапазона")
    device.write_config({"operating": {"pos_min": 0, "pos_max": 3000}})
    print("      диапазон [0, 3000]")

    step(10, "Позиционный режим")
    try:
        device.move_to(5000)
    except CommandError as exc:
        print(f"      попытка выйти за диапазон отклонена: {exc.code} — {exc.detail}")

    target = device.move_to(1500)
    print(f"      цель принята: {target}")
    runner.wait_until_stopped()
    frame = device.last_telemetry
    print(f"      достигнуто: {frame.pos if frame else '?'}")

    step(11, "Непрерывное вращение CW")
    device.motor_run(Direction.CW, speed=600)
    time.sleep(1.0)
    print(f"      позиция: {device.last_telemetry.pos if device.last_telemetry else '?'}")

    step(12, "Остановка")
    device.stop()
    print("      привод остановлен")

    step(13, "Непрерывное вращение CCW и остановка")
    device.motor_run(Direction.CCW, speed=600)
    time.sleep(1.0)
    print(f"      позиция: {device.last_telemetry.pos if device.last_telemetry else '?'}")
    device.stop()
    print("      привод остановлен")

    step(16, "Обработка потери связи")
    if isinstance(runner.transport, SimulatedTransport):
        runner.transport.faults.link_broken = True
        time.sleep(0.5)
        print(f"      состояние соединения после обрыва: "
              f"{'подключено' if device.is_connected else 'отключено'}")
    else:
        print("      пропущено: требует физического отключения USB")

    print(f"\nПринято кадров телеметрии: {runner.frames}")


def main(argv: list[str]) -> int:
    _use_utf8_console()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    port = argv[1] if len(argv) > 1 else None
    transport = SerialTransport(port) if port else SimulatedTransport(persist=False)
    print(f"Режим: {'реальное устройство' if port else 'симулятор (Demo)'}")

    runner = ScenarioRunner()
    device = ServoDevice(
        transport,
        on_telemetry=runner.on_telemetry,
        on_event=runner.on_event,
        on_link_lost=runner.on_link_lost,
    )
    runner.device = device
    runner.transport = transport

    try:
        run(runner)
    except (TransportError, CommandTimeout, CommandError, TimeoutError) as exc:
        print(f"\nСценарий прерван: {exc}")
        return 1
    finally:
        device.disconnect()

    print("\nСценарий завершён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
