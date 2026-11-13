"""Phase 2 tests: usage-pattern detection and data-sufficiency rules (pure, no AWS)."""
import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from agent.services.usage_patterns import (
    THRESHOLD_LIMITS,
    MetricsWindow,
    PatternThresholds,
    SeriesRule,
    assess_sufficiency,
    build_series,
    classify_pattern,
    coverage_pct,
    eta_squared_by_hour,
    percentile,
    span_days,
    summarize,
    validate_threshold,
)

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
CPU_RULE = SeriesRule(idle_level=5.0, idle_stat="p95")
COUNT_RULE = SeriesRule(idle_level=0.0, idle_stat="max")
WINDOW = MetricsWindow()
TH = PatternThresholds()


def hourly(fn, days):
    """Hourly series of `days` days: fn(hour_index, datetime) -> value (None = gap)."""
    out = []
    for i in range(int(days * 24)):
        ts = T0 + timedelta(hours=i)
        v = fn(i, ts)
        if v is not None:
            out.append((ts, float(v)))
    return out


def lcg(seed):
    """Deterministic pseudo-random sequence in [0, 1)."""
    state = seed
    while True:
        state = (1103515245 * state + 12345) % (2 ** 31)
        yield state / 2 ** 31


def end_of(days):
    return T0 + timedelta(days=days)


# ------------------------------------------------------------------ primitives
def test_percentile_interpolates():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(vals, 0) == 1.0
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 100) == 5.0
    assert percentile(vals, 25) == 2.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None


def test_build_series_keeps_gaps_for_gauges():
    pts = [(T0, 10.0), (T0 + timedelta(hours=3), 20.0)]
    s = build_series(pts, window_end=end_of(1), fill_zero=False)
    assert [v for _, v in s] == [10.0, 20.0]


def test_build_series_zero_fills_counts_to_window_end():
    pts = [(T0, 5.0), (T0 + timedelta(hours=3), 7.0)]
    s = build_series(pts, window_end=T0 + timedelta(hours=6), fill_zero=True)
    assert [v for _, v in s] == [5.0, 0.0, 0.0, 7.0, 0.0, 0.0]


def test_build_series_ignores_nan_and_none_and_empty():
    assert build_series([(T0, float("nan")), (T0, None)], window_end=end_of(1), fill_zero=True) == []
    assert build_series([], window_end=end_of(1), fill_zero=False) == []


def test_build_series_normalizes_timestamps_to_the_hour():
    s = build_series([(T0 + timedelta(minutes=17), 1.0)], window_end=end_of(1), fill_zero=False)
    assert s[0][0] == T0


def test_summarize_empty_and_values():
    assert summarize([]) == {"count": 0}
    s = summarize([(T0, 1.0), (T0 + timedelta(hours=1), 3.0)])
    assert s["count"] == 2 and s["mean"] == 2.0 and s["max"] == 3.0 and s["min"] == 1.0


# ----------------------------------------------------- window and sufficiency
def test_metrics_window_defaults_are_the_approved_values():
    assert (WINDOW.preferred_days, WINDOW.minimum_days) == (30, 14)


@pytest.mark.parametrize("preferred,minimum", [(10, 14), (30, 0)])
def test_metrics_window_validation(preferred, minimum):
    with pytest.raises(ValueError):
        MetricsWindow(preferred_days=preferred, minimum_days=minimum)


@pytest.mark.parametrize(
    "days,expected",
    [(30, "preferred"), (29.5, "minimum"), (14, "minimum"), (13.9, "insufficient"), (1, "insufficient")],
)
def test_sufficiency_boundaries(days, expected):
    series = hourly(lambda i, t: 10, days)
    assert assess_sufficiency(series, WINDOW)["status"] == expected


def test_sufficiency_reports_observed_and_required_days():
    suff = assess_sufficiency(hourly(lambda i, t: 10, 20), WINDOW)
    assert suff == {"status": "minimum", "observed_days": 20.0, "preferred_days": 30, "minimum_days": 14}


def test_span_days_and_empty():
    assert span_days([]) == 0.0
    assert span_days(hourly(lambda i, t: 1, 2)) == pytest.approx(2.0)


def test_coverage_pct_detects_gaps():
    full = hourly(lambda i, t: 1, 10)
    half = hourly(lambda i, t: 1 if (i < 12 or i % 2 == 0) else None, 10)
    assert coverage_pct(full) == 100.0
    assert 50 < coverage_pct(half) < 56
    assert coverage_pct([]) is None


# --------------------------------------------------------------- classification
def test_idle_cpu():
    r = classify_pattern(hourly(lambda i, t: 1 + (i % 3), 30), CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "idle"
    assert r["data_sufficiency"]["status"] == "preferred"


def test_steady_cpu():
    r = classify_pattern(hourly(lambda i, t: 40 + (i % 5), 30), CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "steady"
    assert r["stats"]["cv"] <= TH.steady_max_cv


def test_diurnal_business_hours_cpu():
    r = classify_pattern(hourly(lambda i, t: 70 if 9 <= t.hour < 17 else 8, 30), CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "diurnal"
    assert r["stats"]["hour_of_day_eta_squared"] >= TH.diurnal_min_eta_squared
    assert r["stats"]["top_hours_utc"] == [9, 10, 11] or all(9 <= h < 17 for h in r["stats"]["top_hours_utc"])


def test_bursty_cpu_with_irregular_spikes():
    rnd = lcg(7)
    r = classify_pattern(hourly(lambda i, t: 90 if next(rnd) < 0.03 else 6, 30), CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "bursty"
    assert r["stats"]["peak_to_mean"] >= TH.bursty_min_peak_to_mean


def test_variable_cpu_noise_without_cycle():
    rnd = lcg(11)
    r = classify_pattern(hourly(lambda i, t: 1 + 98 * next(rnd), 30), CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "variable"


def test_steady_lambda_invocations():
    r = classify_pattern(hourly(lambda i, t: 500, 30), COUNT_RULE, WINDOW, TH)
    assert r["pattern"] == "steady"


def test_lambda_single_spike_is_bursty_not_variable_or_idle():
    series = build_series(
        [(T0, 5000.0), (T0 + timedelta(hours=1), 0.0)], window_end=end_of(30), fill_zero=True
    )
    r = classify_pattern(series, COUNT_RULE, WINDOW, TH)
    assert r["pattern"] == "diurnal" or r["pattern"] == "bursty"
    assert r["pattern"] != "idle"


def test_lambda_with_no_activity_after_first_datapoint_is_idle():
    series = build_series([(T0, 0.0)], window_end=end_of(30), fill_zero=True)
    r = classify_pattern(series, COUNT_RULE, WINDOW, TH)
    assert r["pattern"] == "idle"
    assert r["data_sufficiency"]["status"] == "preferred"


def test_insufficient_history_overrides_any_pattern():
    r = classify_pattern(hourly(lambda i, t: 40, 10), CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "insufficient_data"
    assert r["data_sufficiency"]["status"] == "insufficient"
    assert "14-day minimum" in r["reasons"][0]
    assert r["stats"]["count"] == 240  # stats still reported for transparency


def test_no_data_is_insufficient():
    r = classify_pattern([], CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "insufficient_data"
    assert "No datapoints" in r["reasons"][0]


def test_exactly_minimum_window_is_usable():
    r = classify_pattern(hourly(lambda i, t: 40 + (i % 5), 14), CPU_RULE, WINDOW, TH)
    assert r["pattern"] == "steady"
    assert r["data_sufficiency"]["status"] == "minimum"


def test_low_coverage_is_flagged_but_pattern_still_reported():
    series = hourly(lambda i, t: 40 + (i % 5) if (i // 24) % 2 == 0 else None, 30)
    r = classify_pattern(series, CPU_RULE, WINDOW, TH)
    assert "low_data_coverage" in r["flags"]
    assert r["pattern"] in {"steady", "diurnal", "bursty", "variable", "idle"}


def test_full_coverage_has_no_flag():
    r = classify_pattern(hourly(lambda i, t: 40 + (i % 5), 30), CPU_RULE, WINDOW, TH)
    assert r["flags"] == []


def test_thresholds_are_overridable_and_echoed_in_the_result():
    series = hourly(lambda i, t: 20 + (i % 4) * 15, 30)  # cv ~0.5
    strict = classify_pattern(series, CPU_RULE, WINDOW, PatternThresholds(steady_max_cv=0.1))
    loose = classify_pattern(series, CPU_RULE, WINDOW, PatternThresholds(steady_max_cv=0.9))
    assert loose["pattern"] == "steady"
    assert strict["pattern"] != "steady"
    assert strict["thresholds_used"]["steady_max_cv"] == 0.1
    assert loose["thresholds_used"]["steady_max_cv"] == 0.9


def test_default_thresholds_are_reported():
    r = classify_pattern(hourly(lambda i, t: 40, 30), CPU_RULE, WINDOW, TH)
    assert r["thresholds_used"] == {
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


def test_eta_squared_constant_series_is_zero_and_cyclic_is_high():
    assert eta_squared_by_hour(hourly(lambda i, t: 5, 5)) == 0.0
    assert eta_squared_by_hour(hourly(lambda i, t: t.hour, 5)) == pytest.approx(1.0)
    assert eta_squared_by_hour([]) == 0.0


def test_series_rule_validation():
    with pytest.raises(ValueError):
        SeriesRule(idle_level=5.0, idle_stat="median")


def test_result_is_json_serializable():
    r = classify_pattern(hourly(lambda i, t: 70 if 9 <= t.hour < 17 else 8, 30), CPU_RULE, WINDOW, TH)
    json.dumps(r)


def test_classification_never_contains_recommendation_or_savings_fields():
    r = classify_pattern(hourly(lambda i, t: 40, 30), CPU_RULE, WINDOW, TH)
    text = json.dumps(r).casefold()
    for forbidden in ("saving", "recommend", "confidence"):
        assert forbidden not in text


# ------------------------------------------- configurable idle thresholds (Phase 2)
EC2_RULE = SeriesRule(idle_level_key="idle_ec2_cpu_pct", idle_stat="p95", peak_guard=True)
RDS_RULE = SeriesRule(idle_level_key="idle_rds_cpu_pct", idle_stat="p95", peak_guard=True)
LAMBDA_RULE = SeriesRule(idle_level_key="idle_lambda_invocations_per_hour", idle_stat="max")
NAT_RULE = SeriesRule(idle_level_key="idle_nat_bytes_per_hour", idle_stat="max")
NON_IDLE = {"steady", "diurnal", "bursty", "variable"}


def flat(days, value):
    return hourly(lambda i, t: value, days)


def spiky(days, base, spike, spike_hours=(100, 300)):
    return hourly(lambda i, t: spike if i in spike_hours else base, days)


def test_default_idle_thresholds_are_the_original_levels():
    d = PatternThresholds()
    assert (d.idle_ec2_cpu_pct, d.idle_rds_cpu_pct) == (5.0, 5.0)
    assert (d.idle_lambda_invocations_per_hour, d.idle_nat_bytes_per_hour) == (0.0, 0.0)
    assert d.idle_cpu_peak_pct == 30.0


def test_thresholds_used_includes_every_threshold_including_idle_ones():
    custom = PatternThresholds(
        steady_max_cv=0.4,
        idle_ec2_cpu_pct=7.0,
        idle_rds_cpu_pct=8.0,
        idle_lambda_invocations_per_hour=2.0,
        idle_nat_bytes_per_hour=1500.0,
        idle_cpu_peak_pct=45.0,
    )
    r = classify_pattern(flat(30, 40), EC2_RULE, WINDOW, custom, peak_series=flat(30, 41))
    assert set(r["thresholds_used"]) == set(THRESHOLD_LIMITS) == {f.name for f in fields(PatternThresholds)}
    assert r["thresholds_used"] == custom.as_dict()


def test_thresholds_used_is_reported_even_without_enough_data():
    assert classify_pattern([], EC2_RULE, WINDOW, TH)["thresholds_used"] == TH.as_dict()
    short = classify_pattern(flat(5, 2), EC2_RULE, WINDOW, TH, peak_series=flat(5, 2))
    assert short["pattern"] == "insufficient_data"
    assert short["thresholds_used"] == TH.as_dict()


@pytest.mark.parametrize(
    "rule,field,raised,series_value",
    [
        (EC2_RULE, "idle_ec2_cpu_pct", 10.0, 8),
        (RDS_RULE, "idle_rds_cpu_pct", 10.0, 8),
        (LAMBDA_RULE, "idle_lambda_invocations_per_hour", 3.0, 3),
        (NAT_RULE, "idle_nat_bytes_per_hour", 1000.0, 800),
    ],
)
def test_each_idle_threshold_drives_its_own_rule(rule, field, raised, series_value):
    series = flat(30, series_value)
    peak = flat(30, series_value)  # equal to the average: well below the CPU peak guard
    default = classify_pattern(series, rule, WINDOW, TH, peak_series=peak)
    custom = classify_pattern(series, rule, WINDOW, PatternThresholds(**{field: raised}), peak_series=peak)
    assert default["pattern"] == "steady"  # above the default idle level
    assert custom["pattern"] == "idle"
    assert custom["rule"]["idle_level"] == raised
    assert custom["rule"]["idle_level_key"] == field
    assert custom["thresholds_used"][field] == raised


def test_idle_thresholds_do_not_leak_between_resource_types():
    series = flat(30, 8)
    th = PatternThresholds(idle_ec2_cpu_pct=10.0)
    assert classify_pattern(series, EC2_RULE, WINDOW, th, peak_series=series)["pattern"] == "idle"
    assert classify_pattern(series, RDS_RULE, WINDOW, th, peak_series=series)["pattern"] == "steady"


def test_idle_level_boundary_is_inclusive():
    at = classify_pattern(flat(30, 5.0), EC2_RULE, WINDOW, TH, peak_series=flat(30, 5.0))
    above = classify_pattern(flat(30, 5.01), EC2_RULE, WINDOW, TH, peak_series=flat(30, 5.01))
    assert at["pattern"] == "idle"
    assert above["pattern"] == "steady"


def test_series_rule_resolves_its_level_from_the_threshold_which_wins_over_a_fixed_level():
    th = PatternThresholds(idle_ec2_cpu_pct=12.0)
    assert SeriesRule(idle_level_key="idle_ec2_cpu_pct").resolve_idle_level(th) == 12.0
    assert SeriesRule(idle_level=50.0, idle_level_key="idle_ec2_cpu_pct").resolve_idle_level(th) == 12.0
    assert SeriesRule(idle_level=50.0).resolve_idle_level(th) == 50.0


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"idle_level_key": "not_a_threshold"}, {"idle_level": 1.0, "idle_stat": "median"}],
)
def test_series_rule_rejects_invalid_definitions(kwargs):
    with pytest.raises(ValueError):
        SeriesRule(**kwargs)


def test_legacy_fixed_level_rule_keeps_working_without_the_guard():
    r = classify_pattern(flat(30, 2), CPU_RULE, WINDOW, TH)  # no peak_series, no peak_guard
    assert r["pattern"] == "idle"
    assert r["flags"] == []
    assert "peak_cpu_pct" not in r["stats"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("steady_max_cv", -0.1),
        ("steady_min_active_fraction", 1.1),
        ("diurnal_min_eta_squared", -1),
        ("bursty_min_peak_to_mean", 0.9),
        ("low_coverage_pct", 100.1),
        ("idle_ec2_cpu_pct", 100.5),
        ("idle_rds_cpu_pct", -1),
        ("idle_lambda_invocations_per_hour", -1),
        ("idle_nat_bytes_per_hour", -0.5),
        ("idle_cpu_peak_pct", 101),
        ("idle_ec2_cpu_pct", float("nan")),
        ("idle_ec2_cpu_pct", float("inf")),
        ("idle_ec2_cpu_pct", True),
        ("idle_ec2_cpu_pct", "5"),
        ("idle_ec2_cpu_pct", None),
    ],
)
def test_invalid_threshold_values_are_rejected(name, value):
    with pytest.raises(ValueError):
        PatternThresholds(**{name: value})
    with pytest.raises(ValueError):
        validate_threshold(name, value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("idle_ec2_cpu_pct", 0),
        ("idle_ec2_cpu_pct", 100),
        ("idle_cpu_peak_pct", 0),
        ("idle_cpu_peak_pct", 100),
        ("idle_nat_bytes_per_hour", 10 ** 12),
    ],
)
def test_threshold_boundaries_are_accepted(name, value):
    assert getattr(PatternThresholds(**{name: value}), name) == value


def test_validate_threshold_rejects_unknown_names_and_returns_floats():
    with pytest.raises(ValueError):
        validate_threshold("idle_gpu_pct", 1)
    assert validate_threshold("idle_ec2_cpu_pct", 7) == 7.0
    assert isinstance(validate_threshold("idle_ec2_cpu_pct", 7), float)


# ------------------------------------------------- high-CPU protection (EC2 / RDS)
@pytest.mark.parametrize("rule", [EC2_RULE, RDS_RULE])
def test_high_cpu_peak_blocks_idle_even_when_p95_is_low(rule):
    avg = flat(30, 2)
    peak = spiky(30, base=4, spike=85)
    guarded = classify_pattern(avg, rule, WINDOW, TH, peak_series=peak)
    assert guarded["pattern"] in NON_IDLE
    assert "idle_blocked_by_cpu_peak" in guarded["flags"]
    assert guarded["stats"]["peak_cpu_pct"] == 85.0
    assert any("peak CPU 85.0% exceeds 30.0%" in r for r in guarded["reasons"])
    # The same data without the guard is idle, so the protection is what changed the outcome.
    unguarded = classify_pattern(avg, SeriesRule(idle_level_key=rule.idle_level_key), WINDOW, TH)
    assert unguarded["pattern"] == "idle"


def test_a_blocked_resource_continues_through_the_normal_classification_order():
    # p95 of the average series is 1 (idle by p95 alone) but one hour averaged 60% with a 100% peak.
    avg = hourly(lambda i, t: 60 if i == 100 else 1, 30)
    peak = hourly(lambda i, t: 100 if i == 100 else 3, 30)
    guarded = classify_pattern(avg, EC2_RULE, WINDOW, TH, peak_series=peak)
    assert guarded["pattern"] == "bursty"
    assert "idle_blocked_by_cpu_peak" in guarded["flags"]
    assert guarded["stats"]["peak_cpu_pct"] == 100.0
    assert classify_pattern(avg, CPU_RULE, WINDOW, TH)["pattern"] == "idle"


def test_peak_exactly_at_the_peak_threshold_is_still_idle_and_just_above_is_not():
    avg = flat(30, 2)
    at = classify_pattern(avg, EC2_RULE, WINDOW, TH, peak_series=spiky(30, 4, 30.0))
    above = classify_pattern(avg, EC2_RULE, WINDOW, TH, peak_series=spiky(30, 4, 30.01))
    assert at["pattern"] == "idle"
    assert "idle_blocked_by_cpu_peak" not in at["flags"]
    assert at["stats"]["peak_cpu_pct"] == 30.0
    assert above["pattern"] in NON_IDLE
    assert "idle_blocked_by_cpu_peak" in above["flags"]


def test_low_peak_keeps_the_resource_idle_and_reports_the_peak():
    r = classify_pattern(flat(30, 2), EC2_RULE, WINDOW, TH, peak_series=spiky(30, 4, 20))
    assert r["pattern"] == "idle"
    assert r["flags"] == []
    assert r["stats"]["peak_cpu_pct"] == 20.0


def test_peak_threshold_is_configurable():
    avg = flat(30, 2)
    peak40 = spiky(30, 4, 40)
    assert classify_pattern(avg, EC2_RULE, WINDOW, TH, peak_series=peak40)["pattern"] in NON_IDLE

    relaxed = classify_pattern(avg, EC2_RULE, WINDOW, PatternThresholds(idle_cpu_peak_pct=50.0), peak_series=peak40)
    assert relaxed["pattern"] == "idle"
    assert relaxed["thresholds_used"]["idle_cpu_peak_pct"] == 50.0

    strict = classify_pattern(avg, EC2_RULE, WINDOW, PatternThresholds(idle_cpu_peak_pct=10.0),
                              peak_series=spiky(30, 4, 20))
    assert strict["pattern"] in NON_IDLE
    assert strict["thresholds_used"]["idle_cpu_peak_pct"] == 10.0


@pytest.mark.parametrize("peak", [None, []])
def test_missing_peak_data_means_not_idle_and_is_flagged(peak):
    r = classify_pattern(flat(30, 2), EC2_RULE, WINDOW, TH, peak_series=peak)
    assert r["pattern"] in NON_IDLE
    assert "idle_peak_unverified" in r["flags"]
    assert "peak_cpu_pct" not in r["stats"]
    assert any("Peak CPU data is unavailable" in reason for reason in r["reasons"])


def test_guard_does_not_run_when_p95_is_already_above_the_idle_level():
    r = classify_pattern(flat(30, 40), EC2_RULE, WINDOW, TH, peak_series=None)
    assert r["pattern"] == "steady"
    assert r["flags"] == []


def test_guard_applies_only_to_rules_that_enable_it():
    r = classify_pattern(flat(30, 0), LAMBDA_RULE, WINDOW, TH, peak_series=flat(30, 99))
    assert r["pattern"] == "idle"
    assert r["flags"] == []
    assert "peak_cpu_pct" not in r["stats"]


def test_insufficient_history_stays_insufficient_with_a_peak_series():
    r = classify_pattern(flat(10, 2), EC2_RULE, WINDOW, TH, peak_series=flat(10, 99))
    assert r["pattern"] == "insufficient_data"
    assert r["flags"] == []
    assert "peak_cpu_pct" not in r["stats"]


def test_guarded_result_is_json_serializable():
    r = classify_pattern(flat(30, 2), EC2_RULE, WINDOW, TH, peak_series=spiky(30, 4, 85))
    json.dumps(r)
