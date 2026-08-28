"""Схема конфигурации устройства — единый источник правды.

Здесь описаны все настраиваемые параметры: их тип, диапазон, значение по умолчанию,
единица измерения и подпись. Из этого описания берут данные:

* UI — границы и шаг спинбоксов, подписи полей;
* валидация — проверка перед отправкой на устройство;
* тесты — генерация корректных и заведомо некорректных конфигураций.

Дублировать диапазоны в этих трёх местах нельзя: рассинхронизация приведёт к тому,
что UI позволит ввести значение, которое прошивка отвергнет. Поэтому диапазоны
живут только здесь, а `docs/PROTOCOL.md` описывает ту же таблицу для прошивки.

См. раздел 7 `docs/PROTOCOL.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Версия схемы конфигурации. Прошивка сравнивает её со значением из NVS.
CONFIG_VERSION = 1


@dataclass(frozen=True)
class IntParam:
    """Целочисленный параметр конфигурации."""

    path: str
    """Путь вида ``"homing.speed"``."""

    label: str
    minimum: int
    maximum: int
    default: int
    unit: str = ""
    description: str = ""

    def validate(self, value: Any) -> str | None:
        """Возвращает текст ошибки либо ``None``, если значение допустимо."""
        # bool — подкласс int, но допускать True вместо 1 в конфигурации не следует.
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{self.path}: ожидалось целое число, получено {type(value).__name__}"
        if not self.minimum <= value <= self.maximum:
            return f"{self.path}: {value} вне диапазона [{self.minimum}, {self.maximum}]"
        return None


@dataclass(frozen=True)
class EnumParam:
    """Параметр с фиксированным набором значений."""

    path: str
    label: str
    choices: tuple[str, ...]
    default: str
    description: str = ""

    def validate(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return f"{self.path}: ожидалась строка, получено {type(value).__name__}"
        if value not in self.choices:
            return f"{self.path}: {value!r} не входит в {list(self.choices)}"
        return None


Param = IntParam | EnumParam


#: Полный список параметров в порядке отображения в UI.
PARAMS: tuple[Param, ...] = (
    IntParam(
        "servo.id", "ID сервопривода", 0, 253, 1,
        description="Адрес привода на TTL-шине",
    ),
    EnumParam(
        "homing.dir", "Направление поиска", ("cw", "ccw"), "cw",
        description="Направление движения при поиске механического упора",
    ),
    IntParam(
        "homing.speed", "Скорость поиска", 1, 3000, 300, "шаг/с",
        description="Скорость движения во время Homing",
    ),
    IntParam(
        "homing.load_threshold", "Порог нагрузки", 50, 1000, 450, "0.1 %",
        description="Нагрузка, при превышении которой фиксируется упор",
    ),
    IntParam(
        "homing.timeout_ms", "Таймаут Homing", 1000, 60000, 10000, "мс",
        description="Предельное время выполнения процедуры",
    ),
    IntParam(
        "homing.max_travel", "Максимальный путь", 100, 8192, 4096, "шаг",
        description="Предельное перемещение во время поиска упора",
    ),
    IntParam(
        "homing.zero_position", "Позиция нуля", 0, 4095, 0, "шаг",
        description="Позиция, назначаемая упору после успешного Homing",
    ),
    IntParam(
        "operating.speed", "Рабочая скорость", 1, 3000, 1000, "шаг/с",
        description="Скорость перемещения в позиционном режиме по умолчанию",
    ),
    IntParam(
        "operating.load_limit", "Ограничение нагрузки", 50, 1000, 600, "0.1 %",
        description="Предельная нагрузка на приводе",
    ),
    IntParam(
        "operating.pos_min", "Минимальная позиция", 0, 4095, 0, "шаг",
        description="Нижняя граница рабочего диапазона",
    ),
    IntParam(
        "operating.pos_max", "Максимальная позиция", 0, 4095, 4095, "шаг",
        description="Верхняя граница рабочего диапазона",
    ),
    IntParam(
        "operating.accel", "Ускорение", 0, 255, 50,
        description="Плавность старта и остановки; 0 — без ограничения",
    ),
    IntParam(
        "safety.link_timeout_ms", "Watchdog связи", 0, 10000, 1000, "мс",
        description="Остановка привода при молчании ПК; 0 отключает",
    ),
)

PARAMS_BY_PATH: dict[str, Param] = {p.path: p for p in PARAMS}


def _split(path: str) -> tuple[str, str]:
    section, _, name = path.partition(".")
    return section, name


def get_value(config: dict[str, Any], path: str) -> Any:
    """Читает значение по пути ``"section.name"``; ``None``, если пути нет."""
    section, name = _split(path)
    branch = config.get(section)
    if not isinstance(branch, dict):
        return None
    return branch.get(name)


def set_value(config: dict[str, Any], path: str, value: Any) -> None:
    """Записывает значение по пути ``"section.name"``, создавая секцию при необходимости."""
    section, name = _split(path)
    config.setdefault(section, {})[name] = value


def default_config() -> dict[str, Any]:
    """Конфигурация со значениями по умолчанию."""
    config: dict[str, Any] = {"version": CONFIG_VERSION}
    for param in PARAMS:
        set_value(config, param.path, param.default)
    return config


def merge_config(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Накладывает частичный ``patch`` на ``base``, не изменяя аргументы.

    Слияние идёт на глубину одной секции: этого достаточно, потому что схема
    конфигурации плоская внутри секций и такой останется (см. ограничение на
    вложенность JSON в разделе 1 протокола).
    """
    result = {key: dict(value) if isinstance(value, dict) else value
              for key, value in base.items()}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def validate_config(config: dict[str, Any], *, partial: bool = False) -> list[str]:
    """Проверяет конфигурацию и возвращает список ошибок (пустой — всё верно).

    :param partial: если ``True``, отсутствующие параметры не считаются ошибкой
        (режим проверки патча для ``set_config``). Межполевые ограничения при этом
        проверяются только для полностью присутствующих пар.
    """
    errors: list[str] = []

    for param in PARAMS:
        value = get_value(config, param.path)
        if value is None:
            if not partial:
                errors.append(f"{param.path}: параметр отсутствует")
            continue
        if (error := param.validate(value)) is not None:
            errors.append(error)

    errors.extend(_validate_cross_field(config))
    return errors


def _validate_cross_field(config: dict[str, Any]) -> list[str]:
    """Проверяет ограничения, связывающие несколько параметров.

    Каждая проверка выполняется, только если оба её значения присутствуют и
    являются целыми: иначе о нарушении уже сообщила проверка диапазона, и второе
    сообщение о той же причине лишь запутает пользователя.
    """
    errors: list[str] = []

    def ints(*paths: str) -> tuple[int, ...] | None:
        values = tuple(get_value(config, path) for path in paths)
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return values  # type: ignore[return-value]
        return None

    if (pair := ints("operating.pos_min", "operating.pos_max")) is not None:
        pos_min, pos_max = pair
        if pos_min >= pos_max:
            errors.append(
                f"operating.pos_min ({pos_min}) должна быть строго меньше "
                f"operating.pos_max ({pos_max})"
            )

    if (triple := ints("homing.zero_position", "operating.pos_min",
                       "operating.pos_max")) is not None:
        zero, pos_min, pos_max = triple
        if not pos_min <= zero <= pos_max:
            errors.append(
                f"homing.zero_position ({zero}) должна лежать в рабочем диапазоне "
                f"[{pos_min}, {pos_max}]"
            )

    if (pair := ints("homing.load_threshold", "operating.load_limit")) is not None:
        threshold, limit = pair
        if threshold > limit:
            errors.append(
                f"homing.load_threshold ({threshold}) не должен превышать "
                f"operating.load_limit ({limit})"
            )

    return errors
