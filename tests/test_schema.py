"""Тесты схемы конфигурации: диапазоны, слияние, межполевые ограничения."""

from __future__ import annotations

import pytest

from servo_configurator.protocol import (
    CONFIG_VERSION,
    PARAMS,
    default_config,
    get_value,
    merge_config,
    set_value,
    validate_config,
)


class TestDefaults:
    def test_defaults_are_valid(self):
        assert validate_config(default_config()) == []

    def test_version_is_present(self):
        assert default_config()["version"] == CONFIG_VERSION

    @pytest.mark.parametrize("param", PARAMS, ids=lambda p: p.path)
    def test_every_parameter_has_value(self, param):
        assert get_value(default_config(), param.path) is not None

    @pytest.mark.parametrize("param", PARAMS, ids=lambda p: p.path)
    def test_default_passes_own_validation(self, param):
        assert param.validate(param.default) is None

    def test_defaults_are_independent_copies(self):
        first = default_config()
        first["homing"]["speed"] = 999
        assert default_config()["homing"]["speed"] != 999


class TestRangeValidation:
    def test_value_below_minimum(self):
        config = default_config()
        set_value(config, "homing.speed", 0)
        assert any("homing.speed" in e for e in validate_config(config))

    def test_value_above_maximum(self):
        config = default_config()
        set_value(config, "operating.accel", 256)
        assert any("operating.accel" in e for e in validate_config(config))

    def test_boundaries_are_inclusive(self):
        config = default_config()
        set_value(config, "homing.speed", 1)
        set_value(config, "operating.accel", 255)
        assert validate_config(config) == []

    def test_wrong_type_is_rejected(self):
        config = default_config()
        set_value(config, "homing.speed", "fast")
        assert any("целое число" in e for e in validate_config(config))

    def test_bool_is_not_accepted_as_int(self):
        """True прошёл бы проверку диапазона как 1 — это скрыло бы ошибку в UI."""
        config = default_config()
        set_value(config, "homing.speed", True)
        assert validate_config(config) != []

    def test_enum_rejects_unknown_choice(self):
        config = default_config()
        set_value(config, "homing.dir", "sideways")
        assert any("homing.dir" in e for e in validate_config(config))

    def test_missing_parameter_is_reported(self):
        config = default_config()
        del config["homing"]["speed"]
        assert any("отсутствует" in e for e in validate_config(config))

    def test_missing_parameter_is_allowed_in_partial_mode(self):
        assert validate_config({"homing": {"speed": 400}}, partial=True) == []

    def test_partial_mode_still_checks_ranges(self):
        assert validate_config({"homing": {"speed": 99999}}, partial=True) != []


class TestCrossFieldValidation:
    def test_pos_min_must_be_below_pos_max(self):
        config = default_config()
        set_value(config, "operating.pos_min", 3000)
        set_value(config, "operating.pos_max", 1000)
        assert any("pos_min" in e for e in validate_config(config))

    def test_equal_positions_are_rejected(self):
        config = default_config()
        set_value(config, "operating.pos_min", 2000)
        set_value(config, "operating.pos_max", 2000)
        set_value(config, "homing.zero_position", 2000)
        assert any("pos_min" in e for e in validate_config(config))

    def test_zero_position_must_be_inside_working_range(self):
        config = default_config()
        set_value(config, "operating.pos_min", 1000)
        set_value(config, "operating.pos_max", 3000)
        set_value(config, "homing.zero_position", 500)
        assert any("zero_position" in e for e in validate_config(config))

    def test_load_threshold_must_not_exceed_limit(self):
        config = default_config()
        set_value(config, "homing.load_threshold", 900)
        set_value(config, "operating.load_limit", 600)
        assert any("load_threshold" in e for e in validate_config(config))

    def test_cross_field_check_is_skipped_when_value_absent(self):
        """Патч с одной границей не должен ругаться на отсутствующую вторую."""
        assert validate_config({"operating": {"pos_min": 100}}, partial=True) == []

    def test_no_duplicate_complaint_for_out_of_range_value(self):
        """Значение вне диапазона порождает ровно одну ошибку, а не две."""
        config = default_config()
        set_value(config, "operating.pos_max", 99999)
        errors = validate_config(config)
        assert len(errors) == 1
        assert "pos_max" in errors[0]


class TestMerge:
    def test_patch_overrides_only_given_fields(self):
        merged = merge_config(default_config(), {"homing": {"speed": 400}})
        assert merged["homing"]["speed"] == 400
        assert merged["homing"]["dir"] == default_config()["homing"]["dir"]

    def test_other_sections_survive(self):
        merged = merge_config(default_config(), {"homing": {"speed": 400}})
        assert merged["operating"] == default_config()["operating"]

    def test_source_is_not_mutated(self):
        base = default_config()
        merge_config(base, {"homing": {"speed": 400}})
        assert base["homing"]["speed"] == default_config()["homing"]["speed"]

    def test_result_is_deep_copy(self):
        base = default_config()
        merged = merge_config(base, {})
        merged["homing"]["speed"] = 12345
        assert base["homing"]["speed"] != 12345

    def test_empty_patch_changes_nothing(self):
        assert merge_config(default_config(), {}) == default_config()

    def test_new_section_is_added(self):
        merged = merge_config(default_config(), {"extra": {"field": 1}})
        assert merged["extra"] == {"field": 1}


class TestPathAccess:
    def test_get_missing_path_returns_none(self):
        assert get_value({}, "homing.speed") is None

    def test_get_from_non_dict_section_returns_none(self):
        assert get_value({"homing": 5}, "homing.speed") is None

    def test_set_creates_missing_section(self):
        config: dict = {}
        set_value(config, "homing.speed", 400)
        assert config == {"homing": {"speed": 400}}


def test_parameter_paths_are_unique():
    paths = [param.path for param in PARAMS]
    assert len(paths) == len(set(paths))
