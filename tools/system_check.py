"""Измерение характеристик системы и формирование отчёта о работоспособности.

В отличие от тестов, которые отвечают «прошло или нет», этот инструмент даёт
числа: сколько кадров телеметрии доходит, с какой задержкой отвечают команды,
растут ли потоки и память, насколько отзывчив интерфейс под нагрузкой.

Запуск::

    python tools/system_check.py                  полный прогон
    python tools/system_check.py --quick          укороченный
    python tools/system_check.py --report out.md  сохранить отчёт
"""

from __future__ import annotations

import argparse
import gc
import io
import logging
import statistics
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop_app"))

from servo_configurator.device import ServoDevice  # noqa: E402
from servo_configurator.protocol import Direction, Telemetry  # noqa: E402
from servo_configurator.simulation import ServoModel, SimulatedFirmware  # noqa: E402
from servo_configurator.transport import SimulatedTransport  # noqa: E402


@dataclass
class Measurement:
    """Одна измеренная характеристика."""

    name: str
    value: str
    verdict: str = "ok"
    note: str = ""


@dataclass
class Section:
    title: str
    measurements: list[Measurement] = field(default_factory=list)

    def add(self, name: str, value: str, verdict: str = "ok", note: str = "") -> None:
        self.measurements.append(Measurement(name, value, verdict, note))
        mark = {"ok": "  ok  ", "warn": " warn ", "fail": " FAIL "}[verdict]
        print(f"  [{mark}] {name}: {value}" + (f" — {note}" if note else ""))


class LogCollector(logging.Handler):
    """Считает записи журнала по уровням."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self.records.append(record)

    def count(self, level: int) -> int:
        return sum(1 for r in self.records if r.levelno == level)

    def messages(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= level]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def make_device(**kwargs) -> tuple[ServoDevice, SimulatedTransport]:
    transport = SimulatedTransport(
        persist=False, firmware=SimulatedFirmware(servo=ServoModel(position=1500.0))
    )
    return ServoDevice(transport, **kwargs), transport


# ---------------------------------------------------------------------------
# Измерения
# ---------------------------------------------------------------------------


def measure_telemetry(duration: float, period_ms: int) -> Section:
    section = Section(f"Телеметрия, период {period_ms} мс, {duration:.0f} с")

    frames: list[Telemetry] = []
    arrival: list[float] = []
    lock = threading.Lock()

    def on_telemetry(frame: Telemetry) -> None:
        with lock:
            frames.append(frame)
            arrival.append(time.monotonic())

    device, _ = make_device(on_telemetry=on_telemetry)
    device.connect()
    try:
        device.set_telemetry(True, period_ms)
        device.motor_run(Direction.CCW, speed=500)
        time.sleep(duration)
        device.stop()
    finally:
        device.disconnect()

    numbers = [f.seq for f in frames]
    lost = sum(
        after - before - 1
        for before, after in zip(numbers, numbers[1:])
        if after > before + 1
    )
    intervals = [(b - a) * 1000 for a, b in zip(arrival, arrival[1:])]

    rate = len(frames) / duration
    expected_rate = 1000 / period_ms

    section.add("Принято кадров", f"{len(frames)}")
    section.add("Частота", f"{rate:.1f} кадр/с", "ok" if rate > expected_rate * 0.8 else "warn",
                f"ожидалось ~{expected_rate:.0f}")
    section.add("Потеряно кадров", f"{lost}", "ok" if lost == 0 else "fail")
    if intervals:
        section.add(
            "Интервал между кадрами",
            f"p50 {statistics.median(intervals):.1f} мс · "
            f"p95 {percentile(intervals, 0.95):.1f} мс · "
            f"max {max(intervals):.1f} мс",
        )
    positions = {f.pos for f in frames}
    section.add("Различных значений позиции", f"{len(positions)}",
                "ok" if len(positions) > 20 else "warn", "данные меняются")
    return section


def measure_commands(count: int) -> Section:
    section = Section(f"Команды, {count} подряд")

    device, _ = make_device()
    device.connect()
    durations: list[float] = []
    failures = 0
    try:
        started_all = time.monotonic()
        for _ in range(count):
            started = time.monotonic()
            try:
                device.ping()
            except Exception:  # noqa: BLE001
                failures += 1
            durations.append((time.monotonic() - started) * 1000)
        total = time.monotonic() - started_all
    finally:
        device.disconnect()

    section.add("Отказов", f"{failures}", "ok" if failures == 0 else "fail")
    section.add("Время отклика",
                f"p50 {statistics.median(durations):.2f} мс · "
                f"p95 {percentile(durations, 0.95):.2f} мс · "
                f"max {max(durations):.2f} мс")
    section.add("Пропускная способность", f"{count / total:.0f} команд/с")
    return section


def measure_parallel(threads: int, per_thread: int) -> Section:
    section = Section(f"Параллельные команды, {threads} потоков по {per_thread}")

    device, _ = make_device()
    device.connect()

    mismatches = 0
    errors = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal mismatches, errors
        for _ in range(per_thread):
            try:
                config, _dirty = device.read_config()
                if config.get("homing", {}).get("speed") != 300:
                    with lock:
                        mismatches += 1
            except Exception:  # noqa: BLE001
                with lock:
                    errors += 1

    started = time.monotonic()
    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join(timeout=60)
    elapsed = time.monotonic() - started
    device.disconnect()

    section.add("Перепутанных ответов", f"{mismatches}", "ok" if mismatches == 0 else "fail")
    section.add("Ошибок", f"{errors}", "ok" if errors == 0 else "fail")
    section.add("Выполнено", f"{threads * per_thread} команд за {elapsed:.1f} с")
    return section


def measure_resources(cycles: int, soak_seconds: float) -> Section:
    section = Section("Ресурсы")

    gc.collect()
    baseline_threads = threading.active_count()

    for _ in range(cycles):
        device, _ = make_device()
        device.connect()
        device.set_telemetry(True, 50)
        device.ping()
        device.disconnect()

    deadline = time.monotonic() + 5.0
    while threading.active_count() > baseline_threads and time.monotonic() < deadline:
        time.sleep(0.1)
    gc.collect()

    leaked = threading.active_count() - baseline_threads
    section.add(f"Потоков после {cycles} циклов подключения",
                f"{leaked:+d} к базовым {baseline_threads}",
                "ok" if leaked <= 2 else "fail")

    received = 0

    def on_telemetry(frame: Telemetry) -> None:
        nonlocal received
        received += 1

    device, _ = make_device(on_telemetry=on_telemetry)
    device.connect()
    try:
        device.set_telemetry(True, 20)
        device.motor_run(Direction.CCW, speed=400)
        time.sleep(2.0)

        gc.collect()
        tracemalloc.start()
        before = tracemalloc.take_snapshot()
        counted_from = received

        time.sleep(soak_seconds)

        gc.collect()
        after = tracemalloc.take_snapshot()
        tracemalloc.stop()
        device.stop()
    finally:
        device.disconnect()

    growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    processed = received - counted_from
    section.add(f"Рост памяти за {soak_seconds:.0f} с непрерывной работы",
                f"{growth / 1024:+.0f} КиБ",
                "ok" if growth < 2_000_000 else "warn",
                f"обработано {processed} кадров")
    return section


def measure_faults() -> Section:
    section = Section("Устойчивость к сбоям")

    losses = 0
    for _ in range(10):
        device, transport = make_device(on_link_lost=lambda c, m: None)
        device.connect()
        device.set_telemetry(True, 20)
        time.sleep(0.05)
        transport.faults.link_broken = True

        deadline = time.monotonic() + 3.0
        while device.is_connected and time.monotonic() < deadline:
            time.sleep(0.02)
        if not device.is_connected:
            losses += 1
        device.disconnect()

    section.add("Обнаружено обрывов связи", f"{losses} из 10",
                "ok" if losses == 10 else "fail")

    device, transport = make_device()
    device.connect()
    succeeded = 0
    try:
        device.set_telemetry(True, 20)
        for _ in range(20):
            transport.faults.corrupt_frames = 2
            try:
                device.ping()
                succeeded += 1
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.02)
    finally:
        device.disconnect()

    section.add("Обменов при помехах в линии", f"{succeeded} из 20 успешны",
                "ok" if succeeded >= 15 else "warn",
                "неуспешные — кадр попал под порчу")

    device, transport = make_device()
    device.connect()
    try:
        transport.faults.silent = True
        silent_ok = False
        try:
            device._client.request(  # noqa: SLF001 - проверка таймаута
                __import__("servo_configurator.protocol", fromlist=["Command"]).Command.PING,
                {}, timeout=0.3,
            )
        except Exception:  # noqa: BLE001
            silent_ok = True

        transport.faults.silent = False
        recovered = device.ping().get("proto") == 1
    finally:
        device.disconnect()

    section.add("Молчание устройства", "обнаружено по таймауту" if silent_ok else "не обнаружено",
                "ok" if silent_ok else "fail")
    section.add("Восстановление после молчания", "обмен продолжен" if recovered else "не удалось",
                "ok" if recovered else "fail")
    return section


def measure_ui(duration: float) -> Section:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from servo_configurator.ui.main_window import MainWindow

    section = Section(f"Интерфейс под нагрузкой, {duration:.0f} с")

    application = QApplication.instance() or QApplication([])
    window = MainWindow()

    def pump(seconds: float, until=None) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            application.processEvents()
            if until is not None and until():
                return True
            time.sleep(0.01)
        return until() if until is not None else True

    try:
        window.connection_panel.connect_requested.emit(None)
        connected = pump(6.0, until=lambda: window.manual_panel.move_button.isEnabled())
        section.add("Подключение через интерфейс", "выполнено" if connected else "не удалось",
                    "ok" if connected else "fail")
        if not connected:
            return section

        window.service._device.set_telemetry(True, 20)  # noqa: SLF001
        window.service.motor_run(Direction.CCW, 600)

        delays: list[float] = []
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            started = time.monotonic()
            application.processEvents()
            delays.append((time.monotonic() - started) * 1000)
            time.sleep(0.005)

        window.service.stop()
        pump(0.5)

        section.add("Задержка обработки событий",
                    f"p50 {statistics.median(delays):.2f} мс · "
                    f"p95 {percentile(delays, 0.95):.2f} мс · "
                    f"max {max(delays):.2f} мс",
                    "ok" if statistics.median(delays) < 20 else "warn")
        points = len(window.charts_panel._buffers["pos"].data()[0])  # noqa: SLF001
        section.add("Точек на графике", f"{points}", "ok" if points > 100 else "warn")
        section.add("Показания телеметрии",
                    f"позиция {window.telemetry_panel._values['pos'].text()}, "  # noqa: SLF001
                    f"состояние отображается")
    finally:
        window.service.shutdown()
        window.close()
        pump(0.3)

    return section


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------


def build_report(sections: list[Section], collector: LogCollector, elapsed: float) -> str:
    lines = [
        "# Отчёт о работоспособности системы",
        "",
        f"Сформирован: {datetime.now():%Y-%m-%d %H:%M}",
        f"Длительность прогона: {elapsed:.0f} с",
        "",
    ]

    failures = [
        m for section in sections for m in section.measurements if m.verdict == "fail"
    ]
    warnings = [
        m for section in sections for m in section.measurements if m.verdict == "warn"
    ]

    lines += [
        "## Итог",
        "",
        f"- проверок выполнено: {sum(len(s.measurements) for s in sections)}",
        f"- отклонений: {len(failures)}",
        f"- предупреждений: {len(warnings)}",
        f"- записей журнала уровня ERROR: {collector.count(logging.ERROR)}",
        f"- записей журнала уровня WARNING: {collector.count(logging.WARNING)}",
        "",
    ]

    if failures:
        lines += ["**Отклонения:**", ""]
        lines += [f"- {m.name}: {m.value}" for m in failures]
        lines.append("")

    for section in sections:
        lines += [f"## {section.title}", "", "| Показатель | Значение | Итог |", "| --- | --- | --- |"]
        for m in section.measurements:
            mark = {"ok": "ok", "warn": "внимание", "fail": "отклонение"}[m.verdict]
            value = m.value + (f" ({m.note})" if m.note else "")
            lines.append(f"| {m.name} | {value} | {mark} |")
        lines.append("")

    errors = collector.messages(logging.ERROR)
    if errors:
        lines += ["## Записи журнала уровня ERROR", ""]
        lines += [f"- {message}" for message in errors[:20]]
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Проверка работоспособности системы")
    parser.add_argument("--quick", action="store_true", help="укороченный прогон")
    parser.add_argument("--report", type=Path, default=None, help="файл для отчёта")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    collector = LogCollector()
    logging.getLogger("servo_configurator").addHandler(collector)

    scale = 0.3 if args.quick else 1.0
    started = time.monotonic()
    sections: list[Section] = []

    for title, function in [
        ("Телеметрия 20 мс", lambda: measure_telemetry(15 * scale, 20)),
        ("Телеметрия 50 мс", lambda: measure_telemetry(10 * scale, 50)),
        ("Команды", lambda: measure_commands(int(600 * scale) or 50)),
        ("Параллельные команды", lambda: measure_parallel(8, int(40 * scale) or 5)),
        ("Ресурсы", lambda: measure_resources(int(25 * scale) or 5, 15 * scale)),
        ("Сбои", measure_faults),
        ("Интерфейс", lambda: measure_ui(12 * scale)),
    ]:
        print(f"\n[{title}]")
        sections.append(function())

    elapsed = time.monotonic() - started
    report = build_report(sections, collector, elapsed)

    print("\n" + "=" * 70)
    failures = sum(
        1 for section in sections for m in section.measurements if m.verdict == "fail"
    )
    warnings = sum(
        1 for section in sections for m in section.measurements if m.verdict == "warn"
    )
    print(f"Проверок: {sum(len(s.measurements) for s in sections)}, "
          f"отклонений: {failures}, предупреждений: {warnings}")
    print(f"Журнал: ERROR {collector.count(logging.ERROR)}, "
          f"WARNING {collector.count(logging.WARNING)}")
    print(f"Время: {elapsed:.0f} с")

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"Отчёт сохранён: {args.report}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
