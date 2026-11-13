"""Phase 2 tests: loading usage-pattern settings from config.yaml (pure, no AWS)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.services.config_service import ConfigService
from agent.services.usage_pattern_config import load_usage_pattern_settings
from agent.services.usage_patterns import (
    MetricsWindow,
    PatternThresholds,
    SeriesRule,
    classify_pattern,
)

LOGGER = "agent.services.usage_pattern_config"
EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "agent" / "config.yaml.example"

DEFAULT_THRESHOLDS = {
    "steady_max_cv": 0.5,
    "steady_min_active_fraction": 0.9,
    "diurnal_min_eta_squared": 0.5,
    "bursty_min_peak_to_mean": 3.0,
    "low_coverage_pct": 80.0,
    "idle_ec2_cpu_pct": 5.0,
    "idle_rds_cpu_pct": 5.0,
    "idle_lambda_invocations_per_hour": 0.0,
    "idle_nat_bytes_per_hour": 0.0,
    "idle_cpu_peak_pct": 30.0,
}


class _ConfigHolder:
    """Stands in for ConfigService: only its ``config`` mapping is read."""

    def __init__(self, config):
        self.config = config


def section(**kwargs):
    return {"usage_patterns": kwargs}


def write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return ConfigService(str(path))


# ------------------------------------------------------------ absent configuration
@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"aws": {"region": "us-east-1"}},
        {"usage_patterns": None},
        "not a mapping",
        42,
        [],
        _ConfigHolder(None),
        _ConfigHolder({}),
        _ConfigHolder({"usage_patterns": None}),
    ],
)
def test_absent_configuration_gives_safe_defaults(config):
    s = load_usage_pattern_settings(config)
    assert s.source == "defaults"
    assert s.warnings == ()
    assert s.window == MetricsWindow()
    assert s.thresholds == PatternThresholds()


def test_calling_without_arguments_gives_defaults():
    assert load_usage_pattern_settings().source == "defaults"


def test_default_values_are_pinned():
    s = load_usage_pattern_settings(None)
    assert s.window.as_dict() == {"preferred_days": 30, "minimum_days": 14}
    assert s.thresholds.as_dict() == DEFAULT_THRESHOLDS


def test_empty_section_uses_default_values_without_warnings():
    s = load_usage_pattern_settings({"usage_patterns": {}})
    assert s.source == "config"
    assert s.warnings == ()
    assert s.window == MetricsWindow()
    assert s.thresholds == PatternThresholds()


def test_empty_subsections_use_defaults_without_warnings():
    s = load_usage_pattern_settings(section(window={}, thresholds={}))
    assert s.warnings == ()
    assert s.window == MetricsWindow() and s.thresholds == PatternThresholds()


# ------------------------------------------------------------------- overrides
def test_every_threshold_and_window_field_can_be_overridden():
    overrides = {
        "steady_max_cv": 0.4,
        "steady_min_active_fraction": 0.8,
        "diurnal_min_eta_squared": 0.6,
        "bursty_min_peak_to_mean": 4.0,
        "low_coverage_pct": 70.0,
        "idle_ec2_cpu_pct": 6.0,
        "idle_rds_cpu_pct": 7.0,
        "idle_lambda_invocations_per_hour": 2.0,
        "idle_nat_bytes_per_hour": 1024.0,
        "idle_cpu_peak_pct": 45.0,
    }
    assert set(overrides) == set(DEFAULT_THRESHOLDS)  # fails if a threshold is added without a test
    assert all(overrides[k] != DEFAULT_THRESHOLDS[k] for k in overrides)

    s = load_usage_pattern_settings(
        section(window={"preferred_days": 60, "minimum_days": 21}, thresholds=overrides)
    )
    assert s.source == "config"
    assert s.warnings == ()
    assert s.window.as_dict() == {"preferred_days": 60, "minimum_days": 21}
    assert s.thresholds.as_dict() == overrides


def test_partial_override_keeps_every_other_default():
    s = load_usage_pattern_settings(section(thresholds={"idle_ec2_cpu_pct": 12.5}))
    expected = dict(DEFAULT_THRESHOLDS, idle_ec2_cpu_pct=12.5)
    assert s.thresholds.as_dict() == expected
    assert s.window == MetricsWindow()
    assert s.warnings == ()


def test_partial_window_override_keeps_the_other_default():
    only_preferred = load_usage_pattern_settings(section(window={"preferred_days": 45}))
    assert only_preferred.window == MetricsWindow(preferred_days=45, minimum_days=14)
    only_minimum = load_usage_pattern_settings(section(window={"minimum_days": 7}))
    assert only_minimum.window == MetricsWindow(preferred_days=30, minimum_days=7)
    assert only_preferred.warnings == only_minimum.warnings == ()


def test_integer_values_are_accepted_and_stored_as_floats():
    s = load_usage_pattern_settings(
        section(thresholds={"idle_ec2_cpu_pct": 10, "idle_nat_bytes_per_hour": 2048})
    )
    assert s.thresholds.idle_ec2_cpu_pct == 10.0
    assert isinstance(s.thresholds.idle_ec2_cpu_pct, float)
    assert isinstance(s.thresholds.idle_nat_bytes_per_hour, float)


@pytest.mark.parametrize(
    "name,value",
    [
        ("idle_ec2_cpu_pct", 0),
        ("idle_ec2_cpu_pct", 100),
        ("idle_cpu_peak_pct", 0),
        ("idle_cpu_peak_pct", 100),
        ("idle_lambda_invocations_per_hour", 0),
        ("bursty_min_peak_to_mean", 1),
    ],
)
def test_boundary_values_are_accepted(name, value):
    s = load_usage_pattern_settings(section(thresholds={name: value}))
    assert s.warnings == ()
    assert getattr(s.thresholds, name) == value


# -------------------------------------------------------- invalid configuration
@pytest.mark.parametrize(
    "name,value",
    [
        ("idle_ec2_cpu_pct", -1),
        ("idle_ec2_cpu_pct", 150),
        ("idle_rds_cpu_pct", "high"),
        ("idle_cpu_peak_pct", True),
        ("idle_cpu_peak_pct", None),
        ("idle_nat_bytes_per_hour", -5),
        ("idle_lambda_invocations_per_hour", float("inf")),
        ("steady_max_cv", float("nan")),
        ("steady_min_active_fraction", 2),
        ("bursty_min_peak_to_mean", 0.5),
        ("low_coverage_pct", [80]),
        ("diurnal_min_eta_squared", {"a": 1}),
    ],
)
def test_invalid_threshold_keeps_its_default_warns_and_never_raises(name, value, caplog):
    good_name, good_value = ("low_coverage_pct", 55.0) if name != "low_coverage_pct" else ("steady_max_cv", 0.4)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        s = load_usage_pattern_settings(section(thresholds={name: value, good_name: good_value}))
    assert getattr(s.thresholds, name) == getattr(PatternThresholds(), name)
    assert getattr(s.thresholds, good_name) == good_value
    assert len(s.warnings) == 1 and name in s.warnings[0]
    assert any(name in r.getMessage() for r in caplog.records)


def test_unknown_keys_are_ignored_with_warnings():
    s = load_usage_pattern_settings(
        section(
            thresholds={"idle_ec2_cpu_pct": 9.0, "idle_gpu_pct": 3},
            window={"preferred_days": 40, "weeks": 2},
            extra=1,
        )
    )
    assert s.thresholds.idle_ec2_cpu_pct == 9.0
    assert s.window.preferred_days == 40
    text = " | ".join(s.warnings)
    assert "idle_gpu_pct" in text and "weeks" in text and "usage_patterns.extra" in text
    assert len(s.warnings) == 3


@pytest.mark.parametrize("bad_section", ["oops", 5, ["a"], True])
def test_non_mapping_section_falls_back_to_defaults(bad_section):
    s = load_usage_pattern_settings({"usage_patterns": bad_section})
    assert s.source == "config"
    assert s.window == MetricsWindow() and s.thresholds == PatternThresholds()
    assert len(s.warnings) == 1 and "usage_patterns" in s.warnings[0]


@pytest.mark.parametrize("key", ["thresholds", "window"])
@pytest.mark.parametrize("bad", ["x", 3, ["a"]])
def test_non_mapping_subsection_falls_back_to_defaults(key, bad):
    s = load_usage_pattern_settings(section(**{key: bad}))
    assert s.window == MetricsWindow() and s.thresholds == PatternThresholds()
    assert len(s.warnings) == 1 and key in s.warnings[0]


@pytest.mark.parametrize(
    "raw",
    [
        {"preferred_days": 0},
        {"preferred_days": -5},
        {"minimum_days": 0},
        {"preferred_days": 30.0},
        {"preferred_days": "30"},
        {"minimum_days": True},
        {"preferred_days": None},
    ],
)
def test_invalid_window_values_keep_the_default_window(raw):
    s = load_usage_pattern_settings(section(window=raw))
    assert s.window == MetricsWindow()
    assert len(s.warnings) == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"preferred_days": 10},                      # default minimum (14) is above it
        {"preferred_days": 20, "minimum_days": 25},
        {"minimum_days": 45},                        # above the default preferred (30)
    ],
)
def test_inconsistent_window_falls_back_to_the_default_window(raw):
    s = load_usage_pattern_settings(section(window=raw))
    assert s.window == MetricsWindow()
    assert any("Invalid window" in w for w in s.warnings)


def test_one_invalid_setting_does_not_discard_the_valid_ones():
    s = load_usage_pattern_settings(
        section(
            window={"preferred_days": 60, "minimum_days": "x"},
            thresholds={"idle_ec2_cpu_pct": 9.0, "idle_rds_cpu_pct": -3},
        )
    )
    assert s.window == MetricsWindow(preferred_days=60, minimum_days=14)
    assert s.thresholds.idle_ec2_cpu_pct == 9.0
    assert s.thresholds.idle_rds_cpu_pct == 5.0
    assert len(s.warnings) == 2


def test_garbage_everywhere_never_raises_and_returns_defaults():
    config = {
        "usage_patterns": {
            "window": {"preferred_days": [], "minimum_days": {}},
            "thresholds": {1: 2, None: 3, "idle_ec2_cpu_pct": "x"},
            5: 6,
        }
    }
    s = load_usage_pattern_settings(config)
    assert s.window == MetricsWindow() and s.thresholds == PatternThresholds()
    assert s.warnings
    json.dumps(s.as_dict())  # still serializable


def test_valid_settings_log_nothing_and_invalid_ones_log_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        load_usage_pattern_settings(section(thresholds={"idle_ec2_cpu_pct": 9.0}))
        assert caplog.records == []
        load_usage_pattern_settings(section(thresholds={"idle_ec2_cpu_pct": -1}))
    assert len(caplog.records) == 1
    assert "idle_ec2_cpu_pct" in caplog.records[0].getMessage()


def test_settings_as_dict_is_json_serializable_and_complete():
    s = load_usage_pattern_settings(section(thresholds={"idle_ec2_cpu_pct": -1}))
    d = json.loads(json.dumps(s.as_dict()))
    assert set(d) == {"source", "window", "thresholds", "warnings"}
    assert d["thresholds"] == DEFAULT_THRESHOLDS
    assert len(d["warnings"]) == 1


# ------------------------------------------------- real config.yaml via ConfigService
def test_loads_from_a_real_config_yaml_through_config_service(tmp_path):
    svc = write_config(
        tmp_path,
        "aws:\n"
        "  region: us-east-1\n"
        "usage_patterns:\n"
        "  window:\n"
        "    preferred_days: 45\n"
        "    minimum_days: 21\n"
        "  thresholds:\n"
        "    idle_ec2_cpu_pct: 8\n"
        "    idle_rds_cpu_pct: 7.5\n"
        "    idle_lambda_invocations_per_hour: 2\n"
        "    idle_nat_bytes_per_hour: 1048576\n"
        "    idle_cpu_peak_pct: 40\n",
    )
    s = load_usage_pattern_settings(svc)
    assert s.source == "config" and s.warnings == ()
    assert s.window == MetricsWindow(preferred_days=45, minimum_days=21)
    assert s.thresholds.idle_ec2_cpu_pct == 8.0
    assert s.thresholds.idle_rds_cpu_pct == 7.5
    assert s.thresholds.idle_lambda_invocations_per_hour == 2.0
    assert s.thresholds.idle_nat_bytes_per_hour == 1048576.0
    assert s.thresholds.idle_cpu_peak_pct == 40.0
    assert s.thresholds.steady_max_cv == 0.5  # untouched default


def test_real_config_yaml_without_the_section_gives_defaults(tmp_path):
    s = load_usage_pattern_settings(write_config(tmp_path, "aws:\n  region: us-east-1\n"))
    assert s.source == "defaults" and s.warnings == ()
    assert s.thresholds == PatternThresholds() and s.window == MetricsWindow()


def test_empty_config_yaml_gives_defaults(tmp_path):
    svc = write_config(tmp_path, "")
    assert svc.config is None  # yaml.safe_load of an empty file
    assert load_usage_pattern_settings(svc).source == "defaults"


def test_real_config_yaml_with_bad_values_never_raises(tmp_path):
    svc = write_config(
        tmp_path,
        "usage_patterns:\n"
        "  window:\n"
        "    preferred_days: thirty\n"
        "  thresholds:\n"
        "    idle_ec2_cpu_pct: .nan\n"
        "    idle_rds_cpu_pct: -4\n"
        "    idle_cpu_peak_pct: yes\n"
        "    idle_nat_bytes_per_hour: 2048\n",
    )
    s = load_usage_pattern_settings(svc)
    assert s.window == MetricsWindow()
    assert s.thresholds == PatternThresholds(idle_nat_bytes_per_hour=2048.0)
    assert len(s.warnings) == 4


def test_real_config_yaml_with_a_scalar_section_never_raises(tmp_path):
    s = load_usage_pattern_settings(write_config(tmp_path, "usage_patterns: oops\n"))
    assert s.thresholds == PatternThresholds() and s.window == MetricsWindow()
    assert len(s.warnings) == 1


def test_shipped_example_config_loads_without_warnings():
    s = load_usage_pattern_settings(ConfigService(str(EXAMPLE_CONFIG)))
    assert s.warnings == ()
    assert s.window == MetricsWindow() and s.thresholds == PatternThresholds()


# ------------------------------------------------ settings drive the classifier
def test_loaded_settings_drive_classification_sufficiency_and_thresholds_used():
    s = load_usage_pattern_settings(
        section(
            window={"preferred_days": 10, "minimum_days": 7},
            thresholds={"idle_ec2_cpu_pct": 10, "idle_cpu_peak_pct": 25},
        )
    )
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    series = [(t0 + timedelta(hours=i), 8.0) for i in range(10 * 24)]
    peak = [(ts, 9.0) for ts, _ in series]
    rule = SeriesRule(idle_level_key="idle_ec2_cpu_pct", idle_stat="p95", peak_guard=True)

    configured = classify_pattern(series, rule, s.window, s.thresholds, peak_series=peak)
    assert configured["pattern"] == "idle"
    assert configured["data_sufficiency"]["status"] == "preferred"
    assert configured["thresholds_used"] == s.thresholds.as_dict()
    assert configured["thresholds_used"]["idle_ec2_cpu_pct"] == 10.0
    assert configured["thresholds_used"]["idle_cpu_peak_pct"] == 25.0

    # With built-in defaults the same 10 days are below the 14-day minimum.
    default = classify_pattern(series, rule, MetricsWindow(), PatternThresholds(), peak_series=peak)
    assert default["pattern"] == "insufficient_data"
