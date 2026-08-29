"""Нагрузочные и устойчивостные проверки.

Обычные тесты отвечают на вопрос «работает ли функция». Здесь проверяется то,
что проявляется только со временем и под нагрузкой: не теряются ли кадры
телеметрии, не растут ли потоки и память при повторных подключениях, не
разъезжаются ли ответы при параллельных командах, восстанавливается ли
приложение после череды сбоев связи.

Тесты помечены маркером ``slow`` и исключены из обычного прогона:

    pytest -m slow          только нагрузочные
    pytest                  все, кроме нагрузочных
"""

from __future__ import annotations

import contextlib
import gc
import itertools
import logging
import statistics
import threading
import time
import tracemalloc

import pytest

from servo_configurator.device import ServoDevice
from servo_configurator.protocol import (
    Command,
    CommandError,
    CommandTimeout,
    DeviceState,
    Direction,
    Telemetry,
    TransportError,
)
from servo_configurator.simulation import ServoModel, SimulatedFirmware
from servo_configurator.transport import SimulatedTransport

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Инструменты наблюдения
# ---------------------------------------------------------------------------


class LogWatcher(logging.Handler):
    """Собирает записи журнала уровня WARNING и выше.

    Приложение спроектировано так, чтобы не падать при сбоях, — значит, о
    проблемах оно сообщает журналом. Молчаливый прогон и чистый журнал вместе
    означают, что ошибок действительно не было; проверять только отсутствие
    исключений недостаточно.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self.records.append(record)

    @property
    def errors(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.ERROR]

    @property
    def exceptions(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.exc_info is not None]

    def summary(self) -> str:
        return "; ".join(f"{r.name}: {r.getMessage()[:120]}" for r in self.records[:5])


@pytest.fixture
def logs():
    """Перехватывает журнал приложения на время теста."""
    watcher = LogWatcher()
    root = logging.getLogger("servo_configurator")
    root.addHandler(watcher)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield watcher
    finally:
        root.removeHandler(watcher)
        root.setLevel(previous)


@pytest.fixture
def thread_exceptions():
    """Ловит исключения, вылетевшие из рабочих потоков.

    Такое исключение не роняет процесс и не попадает в отчёт pytest — без
    отдельного перехвата оно осталось бы незамеченным.
    """
    captured: list[threading.ExceptHookArgs] = []
    original = threading.excepthook

    def hook(args):
        captured.append(args)
        original(args)

    threading.excepthook = hook
    try:
        yield captured
    finally:
        threading.excepthook = original


def make_device(**kwargs) -> tuple[ServoDevice, SimulatedTransport]:
    transport = SimulatedTransport(
        persist=False, firmware=SimulatedFirmware(servo=ServoModel(position=1500.0))
    )
    device = ServoDevice(transport, **kwargs)
    return device, transport


# ---------------------------------------------------------------------------
# Поток телеметрии
# ---------------------------------------------------------------------------


class TestTelemetryThroughput:
    """Телеметрия на максимальной частоте в течение длительного времени."""

    DURATION_S = 12.0
    PERIOD_MS = 20

    def test_stream_is_continuous_and_complete(self, logs, thread_exceptions):
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
            device.set_telemetry(True, self.PERIOD_MS)
            # Во время потока идёт движение: телеметрия покоящегося привода
            # не нагружает ни разбор, ни отрисовку.
            device.motor_run(Direction.CCW, speed=500)
            time.sleep(self.DURATION_S)
            device.stop()
        finally:
            device.disconnect()

        assert len(frames) > 300, f"получено слишком мало кадров: {len(frames)}"

        # Номера кадров обязаны идти подряд: разрыв означает потерю.
        numbers = [f.seq for f in frames]
        gaps = [
            (before, after)
            for before, after in itertools.pairwise(numbers)
            if after != before + 1
        ]
        assert not gaps, f"потеряны кадры: {gaps[:5]}"

        expected = self.PERIOD_MS / 1000.0
        intervals = [b - a for a, b in itertools.pairwise(arrival)]
        median = statistics.median(intervals)
        assert median < expected * 2.5, f"медианный интервал {median * 1000:.1f} мс"

        assert not logs.errors, logs.summary()
        assert not thread_exceptions

    def test_frames_carry_moving_values(self, logs):
        """Поток должен нести меняющиеся данные, а не повторять один кадр."""
        frames: list[Telemetry] = []
        device, _ = make_device(on_telemetry=frames.append)
        device.connect()
        try:
            device.set_telemetry(True, 20)
            device.motor_run(Direction.CCW, speed=800)
            time.sleep(2.0)
            device.stop()
        finally:
            device.disconnect()

        positions = {f.pos for f in frames}
        assert len(positions) > 20, "позиция не менялась"
        assert not logs.errors, logs.summary()


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


class TestCommandStorm:
    """Массовая отправка команд: сопоставление ответов и отсутствие деградации."""

    COMMANDS = 600

    def test_sequential_commands(self, logs, thread_exceptions):
        device, _ = make_device()
        device.connect()

        durations: list[float] = []
        try:
            for _ in range(self.COMMANDS):
                started = time.monotonic()
                info = device.ping()
                durations.append(time.monotonic() - started)
                assert info["dev"] == "ws-servo-esp32"
        finally:
            device.disconnect()

        median = statistics.median(durations)
        worst = max(durations)
        assert median < 0.05, f"медианное время команды {median * 1000:.1f} мс"
        assert worst < 1.0, f"худшее время команды {worst * 1000:.1f} мс"
        assert not logs.errors, logs.summary()
        assert not thread_exceptions

    def test_parallel_commands_are_not_mixed_up(self, logs, thread_exceptions):
        """Ответы обязаны попадать своим запросам при работе из многих потоков."""
        device, _ = make_device()
        device.connect()

        errors: list[Exception] = []
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                for _ in range(40):
                    config, _dirty = device.read_config()
                    # Ответ на get_config обязан содержать полную конфигурацию;
                    # подмена ответом на ping выдала бы себя отсутствием ключа.
                    value = config["homing"]["speed"]
                    with lock:
                        results.append(value)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        device.disconnect()

        assert not errors, f"ошибки в потоках: {errors[:3]}"
        assert len(results) == 8 * 40
        assert set(results) == {300}, "ответы перепутаны между запросами"
        assert not thread_exceptions

    def test_stop_is_served_during_command_flood(self, logs):
        """Аварийный останов не должен ждать очереди обычных команд."""
        device, transport = make_device()
        device.connect()

        stop_flag = threading.Event()

        def flood() -> None:
            while not stop_flag.is_set():
                try:
                    device.read_config()
                except Exception:
                    return

        device.motor_run(Direction.CCW, speed=600)
        workers = [threading.Thread(target=flood) for _ in range(4)]
        for worker in workers:
            worker.start()

        time.sleep(0.5)
        started = time.monotonic()
        device.stop(emergency=True)
        elapsed = time.monotonic() - started

        stop_flag.set()
        for worker in workers:
            worker.join(timeout=10)
        device.disconnect()

        # Аварийный останов запирает устройство до явного снятия.
        assert transport.firmware.state is DeviceState.ESTOP
        assert elapsed < 1.5, f"останов занял {elapsed:.2f} с под нагрузкой"


# ---------------------------------------------------------------------------
# Повторные подключения и ресурсы
# ---------------------------------------------------------------------------


class TestResourceUsage:
    """Утечки потоков и памяти при длительной работе."""

    CYCLES = 25

    def test_connect_disconnect_cycles_do_not_leak_threads(self, logs, thread_exceptions):
        gc.collect()
        baseline = threading.active_count()

        for _ in range(self.CYCLES):
            device, _ = make_device()
            device.connect()
            device.set_telemetry(True, 50)
            device.ping()
            device.disconnect()

        # Потокам нужно мгновение, чтобы завершиться после disconnect.
        deadline = time.monotonic() + 5.0
        while threading.active_count() > baseline + 2 and time.monotonic() < deadline:
            time.sleep(0.1)

        gc.collect()
        leaked = threading.active_count() - baseline
        assert leaked <= 2, (
            f"после {self.CYCLES} циклов осталось лишних потоков: {leaked} "
            f"({[t.name for t in threading.enumerate()][:8]})"
        )
        assert not logs.errors, logs.summary()
        assert not thread_exceptions

    def test_long_telemetry_run_does_not_grow_memory(self, logs):
        """Длительный поток телеметрии не должен наращивать потребление памяти."""
        received = 0

        def on_telemetry(frame: Telemetry) -> None:
            nonlocal received
            received += 1

        device, _ = make_device(on_telemetry=on_telemetry)
        device.connect()
        try:
            device.set_telemetry(True, 20)
            device.motor_run(Direction.CCW, speed=400)

            time.sleep(2.0)  # прогрев: разовые выделения не должны попасть в замер
            gc.collect()
            tracemalloc.start()
            snapshot_before = tracemalloc.take_snapshot()

            time.sleep(8.0)

            gc.collect()
            snapshot_after = tracemalloc.take_snapshot()
            tracemalloc.stop()

            device.stop()
        finally:
            device.disconnect()

        growth = sum(
            stat.size_diff for stat in snapshot_after.compare_to(snapshot_before, "filename")
        )
        assert received > 300, f"принято кадров: {received}"
        assert growth < 2_000_000, f"рост памяти {growth / 1024:.0f} КиБ за 8 с"
        assert not logs.errors, logs.summary()


# ---------------------------------------------------------------------------
# Устойчивость к сбоям
# ---------------------------------------------------------------------------


class TestChaos:
    """Череда сбоев связи: приложение обязано пережить каждый из них."""

    def test_repeated_link_losses_and_recovery(self, logs, thread_exceptions):
        losses: list[tuple[str, str]] = []

        for _ in range(10):
            device, transport = make_device(on_link_lost=lambda c, m: losses.append((c, m)))
            device.connect()
            device.set_telemetry(True, 20)
            time.sleep(0.1)

            transport.faults.link_broken = True
            deadline = time.monotonic() + 3.0
            while device.is_connected and time.monotonic() < deadline:
                time.sleep(0.02)

            assert not device.is_connected, "обрыв связи не обнаружен"
            device.disconnect()

        assert len(losses) == 10, f"сообщений об обрыве: {len(losses)}"
        assert not thread_exceptions

    def test_corrupted_stream_does_not_break_exchange(self, logs, thread_exceptions):
        """Постоянные помехи в линии не должны нарушать обмен командами."""
        device, transport = make_device()
        device.connect()

        succeeded = 0
        try:
            device.set_telemetry(True, 20)
            for _ in range(30):
                # Порча кадров идёт непрерывно, параллельно с командами.
                transport.faults.corrupt_frames = 2
                try:
                    device.ping()
                    succeeded += 1
                except CommandTimeout:
                    # Ответ мог попасть под порчу — это ожидаемо и допустимо.
                    pass
                time.sleep(0.02)
        finally:
            device.disconnect()

        assert succeeded >= 25, f"успешных обменов при помехах: {succeeded} из 30"
        assert not thread_exceptions

    def test_silence_recovers_after_fault_is_cleared(self, logs):
        device, transport = make_device()
        device.connect()
        try:
            transport.faults.silent = True
            with pytest.raises(CommandTimeout):
                device._client.request(Command.PING, {}, timeout=0.3)

            transport.faults.silent = False
            assert device.ping()["proto"] == 1
        finally:
            device.disconnect()

    def test_commands_after_link_loss_are_rejected_cleanly(self, logs, thread_exceptions):
        """После обрыва команды должны отказывать понятно, а не зависать."""
        device, transport = make_device()
        device.connect()
        device.set_telemetry(True, 20)
        time.sleep(0.1)

        transport.faults.link_broken = True
        deadline = time.monotonic() + 3.0
        while device.is_connected and time.monotonic() < deadline:
            time.sleep(0.02)

        for _ in range(5):
            started = time.monotonic()
            with pytest.raises((TransportError, CommandTimeout)):
                device.ping()
            assert time.monotonic() - started < 2.0, "команда зависла после обрыва"

        device.disconnect()
        assert not thread_exceptions

    def test_random_command_sequences_keep_device_consistent(self, logs, thread_exceptions):
        """Произвольный порядок команд не должен приводить в недопустимое состояние."""
        import random

        random.seed(20260829)
        device, transport = make_device()
        device.connect()

        actions = [
            lambda: device.ping(),
            lambda: device.read_config(),
            lambda: device.motor_run(Direction.CW, 400),
            lambda: device.motor_run(Direction.CCW, 400),
            lambda: device.stop(),
            lambda: device.stop(emergency=True),
            lambda: device.reset(),
            lambda: device.start_homing(),
            lambda: device.abort_homing(),
            lambda: device.move_to(1500),
            lambda: device.write_config({"homing": {"speed": random.randint(100, 900)}}),
        ]

        try:
            device.set_telemetry(True, 50)
            for _ in range(150):
                action = random.choice(actions)
                # Отказ по состоянию или диапазону — штатный ответ протокола.
                with contextlib.suppress(CommandError, ValueError):
                    action()
                time.sleep(0.01)

            # Что бы ни происходило, устройство обязано остаться управляемым.
            # Аварийный останов мог сработать в случайном порядке команд, и
            # выход из него возможен только явным снятием.
            device.stop()
            if transport.firmware.state is DeviceState.ESTOP:
                device.reset()
            assert transport.firmware.state is DeviceState.IDLE
            assert device.ping()["proto"] == 1
        finally:
            device.disconnect()

        assert not thread_exceptions


# ---------------------------------------------------------------------------
# Интерфейс под нагрузкой
# ---------------------------------------------------------------------------


class TestUiUnderLoad:
    """Отзывчивость интерфейса при непрерывном потоке телеметрии."""

    def test_ui_stays_responsive(self, qapp, pump, logs, thread_exceptions):
        from servo_configurator.ui.main_window import MainWindow

        window = MainWindow()
        try:
            window.connection_panel.connect_requested.emit(None)
            assert pump(5.0, until=lambda: window.manual_panel.move_button.isEnabled())

            window.service._device.set_telemetry(True, 20)
            window.service.motor_run(Direction.CCW, 600)

            # Замеряется время обработки событий: если разбор телеметрии или
            # отрисовка блокируют поток UI, задержка вырастет сразу.
            delays: list[float] = []
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                started = time.monotonic()
                qapp.processEvents()
                delays.append(time.monotonic() - started)
                time.sleep(0.005)

            window.service.stop()
            pump(0.5)

            worst = max(delays)
            median = statistics.median(delays)
            assert median < 0.02, f"медианная задержка UI {median * 1000:.1f} мс"
            assert worst < 0.5, f"худшая задержка UI {worst * 1000:.1f} мс"

            points = len(window.charts_panel._buffers["pos"].data()[0])
            assert points > 200, f"на графике точек: {points}"
        finally:
            window.service.shutdown()
            window.close()
            pump(0.2)

        assert not logs.errors, logs.summary()
        assert not thread_exceptions

    def test_repeated_window_lifecycles(self, qapp, pump, logs, thread_exceptions):
        """Многократное открытие и закрытие окна не оставляет следов."""
        from servo_configurator.ui.main_window import MainWindow

        gc.collect()
        baseline = threading.active_count()

        for _ in range(8):
            window = MainWindow()
            window.connection_panel.connect_requested.emit(None)
            pump(5.0, until=lambda w=window: w.manual_panel.move_button.isEnabled())
            window.service.motor_run(Direction.CCW, 500)
            pump(0.3)
            window.close()
            window.service.shutdown()
            pump(0.2)
            del window
            gc.collect()

        deadline = time.monotonic() + 5.0
        while threading.active_count() > baseline + 2 and time.monotonic() < deadline:
            time.sleep(0.1)

        leaked = threading.active_count() - baseline
        assert leaked <= 2, f"осталось лишних потоков: {leaked}"
        assert not logs.errors, logs.summary()
        assert not thread_exceptions


# ---------------------------------------------------------------------------
# Сценарий приёмки целиком
# ---------------------------------------------------------------------------


class TestAcceptanceScenario:
    """Все семнадцать шагов раздела 11 задания подряд, через интерфейс."""

    def test_full_scenario(self, qapp, pump, logs, thread_exceptions, tmp_path):
        from servo_configurator.ui.main_window import MainWindow

        window = MainWindow()
        steps: list[str] = []

        def done(step: str) -> None:
            steps.append(step)

        try:
            # 1. Запуск приложения
            assert window.isEnabled()
            done("1. приложение запущено")

            # 2. Обновление списка портов и выбор устройства
            window.connection_panel.refresh_ports()
            assert window.connection_panel.port_combo.count() >= 1
            done("2. список портов обновлён")

            # 3. Подключение
            window.connection_panel.connect_requested.emit(None)
            assert pump(6.0, until=lambda: window.manual_panel.move_button.isEnabled())
            done("3. подключение выполнено")

            # 4. Чтение конфигурации
            assert pump(3.0, until=lambda: window.homing_panel.form.values()["homing.speed"] == 300)
            done("4. конфигурация прочитана")

            # 5. Изменение параметров Homing
            form = window.homing_panel.form
            form._editors["homing.speed"].setValue(400)
            form._editors["homing.load_threshold"].setValue(500)
            combo = form._editors["homing.dir"]
            combo.setCurrentIndex(combo.findData("cw"))
            assert window.homing_panel.write_button.isEnabled()
            done("5. параметры Homing изменены")

            # 6. Запись параметров
            window.homing_panel.write_button.click()
            assert pump(5.0, until=lambda: form.values()["homing.speed"] == 400)
            done("6. параметры записаны")

            # 7. Homing
            window.homing_panel.set_running(True)
            window.service.start_homing()
            assert pump(25.0,
                        until=lambda: "Completed" in window.homing_panel.result_label.text())
            done("7. Homing завершён успешно")

            # 8. Рабочие параметры
            operating = window.operating_panel.form
            operating._editors["operating.speed"].setValue(1200)
            operating._editors["operating.load_limit"].setValue(700)
            window.operating_panel.write_button.click()
            assert pump(5.0, until=lambda: operating.values()["operating.speed"] == 1200)
            done("8. рабочие параметры установлены")

            # 9. Диапазон перемещения
            operating._editors["operating.pos_min"].setValue(0)
            operating._editors["operating.pos_max"].setValue(3000)
            window.operating_panel.write_button.click()
            assert pump(5.0, until=lambda: operating.values()["operating.pos_max"] == 3000)
            done("9. рабочий диапазон задан")

            # 10. Позиционный режим
            window.manual_panel.position_spin.setValue(1500)
            window.manual_panel.move_button.click()
            assert pump(15.0, until=lambda: abs(_ui_position(window) - 1500) < 40)
            done("10. перемещение в позицию выполнено")

            # 11. Непрерывное вращение CW
            window.manual_panel.ccw_button.click()
            assert pump(3.0, until=lambda: _firmware(window).state is DeviceState.MOTOR)
            pump(1.0)
            done("11. непрерывное вращение запущено")

            # 12. Останов
            window.manual_panel.stop_button.click()
            assert pump(3.0, until=lambda: _firmware(window).state is DeviceState.IDLE)
            done("12. привод остановлен")

            # 13. Вращение в обратную сторону и останов
            window.manual_panel.cw_button.click()
            assert pump(3.0, until=lambda: _firmware(window).state is DeviceState.MOTOR)
            pump(1.0)
            window.manual_panel.stop_button.click()
            assert pump(3.0, until=lambda: _firmware(window).state is DeviceState.IDLE)
            done("13. обратное вращение и останов")

            # 14. Наблюдение телеметрии
            assert window.telemetry_panel._values["pos"].text() not in ("", "—")
            assert window.telemetry_panel._values["spd"].text() not in ("", "—")
            assert window.telemetry_panel._values["load"].text() not in ("", "—")
            done("14. телеметрия отображается")

            # 15. Обновление графиков
            points = len(window.charts_panel._buffers["pos"].data()[0])
            assert points > 50, f"точек на графике: {points}"
            done(f"15. графики обновляются ({points} точек)")

            # 16. Потеря связи.
            # До этого шага журнал обязан быть чистым; сам обрыв устроен
            # намеренно, и сообщение о нём — правильная реакция, а не дефект.
            assert not logs.errors, f"ошибки до шага 16: {logs.summary()}"
            errors_before_break = len(logs.errors)

            window.demo_faults.fault_requested.emit("link")
            assert pump(6.0, until=lambda: not window.manual_panel.move_button.isEnabled())
            assert not window.service.is_connected
            done("16. потеря связи обработана")

            # 17. Перезапуск устройства и чтение сохранённой конфигурации
            storage = tmp_path / "nvs.json"
            first = ServoDevice(SimulatedTransport(persist=True, nvs_path=storage))
            first.connect()
            first.write_config({"homing": {"speed": 850}})
            first.save_config()
            first.disconnect()

            second = ServoDevice(SimulatedTransport(persist=True, nvs_path=storage))
            second.connect()
            restored, dirty = second.read_config()
            second.disconnect()

            assert restored["homing"]["speed"] == 850 and dirty is False
            done("17. конфигурация пережила перезапуск")

        finally:
            window.service.shutdown()
            window.close()
            pump(0.3)

        assert len(steps) == 17, f"пройдено шагов: {len(steps)}\n" + "\n".join(steps)

        # После шага 16 допустима ровно одна причина ошибок — обрыв связи,
        # который тест устроил намеренно.
        unexpected = [
            record
            for record in logs.errors[errors_before_break:]
            if "связь потеряна" not in record.getMessage().lower()
            and "потеря связи" not in record.getMessage().lower()
        ]
        assert not unexpected, f"неожиданные ошибки: {[r.getMessage() for r in unexpected]}"
        assert not thread_exceptions


def _ui_position(window) -> int:
    text = window.telemetry_panel._values["pos"].text()
    return int(text) if text.lstrip("-").isdigit() else -10000


def _firmware(window):
    return window.service.transport.firmware
