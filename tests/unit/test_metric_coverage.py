"""Step 38.5 tests: per-series metric coverage (raw datapoint presence against the full window)."""
from datetime import datetime, timedelta, timezone

import pytest

from agent.services.metric_coverage import (
    STATUS_COMPLETE,
    STATUS_EMPTY,
    STATUS_PARTIAL,
    calculate_series_coverage,
    count_distinct_timestamps,
    expected_datapoints,
)

THIRTY_DAYS_HOURLY = 30 * 24   # 720


# ------------------------------------------------------------------ expected count
def test_hourly_expected_count_is_window_days_times_24():
    assert expected_datapoints(30) == 720
    assert expected_datapoints(14) == 336
    assert expected_datapoints(1) == 24


def test_expected_count_follows_the_period():
    assert expected_datapoints(30, 300) == 30 * 288          # 5-minute period
    assert expected_datapoints(30, 86400) == 30              # daily period


@pytest.mark.parametrize("days", [0, -1, float("nan"), float("inf"), True, "30", None])
def test_invalid_window_days_rejected(days):
    with pytest.raises(ValueError):
        expected_datapoints(days)


@pytest.mark.parametrize("period", [0, -3600, 3600.0, True, "3600", None])
def test_invalid_period_rejected(period):
    with pytest.raises(ValueError):
        expected_datapoints(30, period)


def test_window_shorter_than_one_period_rejected():
    with pytest.raises(ValueError):
        expected_datapoints(0.01, 3600)


# ------------------------------------------------------------------ complete series
def test_complete_series_is_100_percent():
    c = calculate_series_coverage(720, 30)
    assert c.status == STATUS_COMPLETE
    assert (c.observed_datapoints, c.counted_datapoints, c.expected_datapoints, c.missing_datapoints) == (720, 720, 720, 0)
    assert c.coverage_pct == 100.0
    assert c.meets_minimum is None            # no minimum supplied


def test_complete_series_meets_the_default_style_minimum():
    c = calculate_series_coverage(720, 30, min_coverage_pct=80)
    assert c.meets_minimum is True


def test_extra_datapoint_at_the_window_edge_is_capped_not_over_100():
    c = calculate_series_coverage(721, 30, min_coverage_pct=80)
    assert c.observed_datapoints == 721 and c.counted_datapoints == 720
    assert c.coverage_pct == 100.0 and c.status == STATUS_COMPLETE and c.missing_datapoints == 0


# ------------------------------------------------------------------ partial series
def test_partial_series_reports_fraction_of_the_full_window():
    c = calculate_series_coverage(600, 30, min_coverage_pct=80)
    assert c.status == STATUS_PARTIAL
    assert c.missing_datapoints == 120
    assert c.coverage_pct == pytest.approx(83.3333, abs=1e-3)
    assert c.meets_minimum is True


def test_coverage_is_measured_against_the_full_window_not_the_span_with_data():
    # 10 fully-covered days out of a 30-day window is 33.3%, however dense those 10 days are.
    c = calculate_series_coverage(10 * 24, 30, min_coverage_pct=80)
    assert c.coverage_pct == pytest.approx(33.3333, abs=1e-3)
    assert c.meets_minimum is False


# ------------------------------------------------------------------ empty series
def test_empty_series_is_zero_percent_and_never_meets_a_minimum():
    c = calculate_series_coverage(0, 30, min_coverage_pct=80)
    assert c.status == STATUS_EMPTY
    assert c.coverage_pct == 0.0 and c.counted_datapoints == 0 and c.missing_datapoints == 720
    assert c.meets_minimum is False


# ------------------------------------------------------------------ insufficient coverage
def test_insufficient_coverage_below_the_minimum():
    c = calculate_series_coverage(500, 30, min_coverage_pct=80)
    assert c.status == STATUS_PARTIAL
    assert c.coverage_pct == pytest.approx(69.4444, abs=1e-3)
    assert c.meets_minimum is False


def test_minimum_is_inclusive_at_exactly_80_percent():
    assert calculate_series_coverage(576, 30, min_coverage_pct=80).meets_minimum is True    # 576 = 80% of 720
    assert calculate_series_coverage(575, 30, min_coverage_pct=80).meets_minimum is False


def test_minimum_comparison_is_exact_for_decimal_minimums():
    # 33.3% of 720 is 239.76: 240 datapoints pass, 239 do not.
    assert calculate_series_coverage(240, 30, min_coverage_pct=33.3).meets_minimum is True
    assert calculate_series_coverage(239, 30, min_coverage_pct=33.3).meets_minimum is False


def test_a_100_percent_minimum_needs_every_datapoint():
    assert calculate_series_coverage(720, 30, min_coverage_pct=100).meets_minimum is True
    assert calculate_series_coverage(719, 30, min_coverage_pct=100).meets_minimum is False


# ------------------------------------------------------------------ zero-fill must not count
def test_zero_filled_points_are_not_observations():
    # A zero-filled Lambda series has 720 values, but only 5 raw datapoints were returned by CloudWatch.
    # Coverage is computed from the raw count, so the zero-filled length never inflates it.
    zero_filled_length = 720
    raw_datapoints = 5
    c = calculate_series_coverage(raw_datapoints, 30, min_coverage_pct=80)
    assert zero_filled_length == c.expected_datapoints
    assert c.counted_datapoints == 5 and c.missing_datapoints == 715
    assert c.coverage_pct == pytest.approx(0.6944, abs=1e-3)
    assert c.meets_minimum is False


# ------------------------------------------------------------------ other windows / periods
def test_minimum_window_of_14_days():
    c = calculate_series_coverage(336, 14, min_coverage_pct=80)
    assert c.expected_datapoints == 336 and c.status == STATUS_COMPLETE


def test_non_hourly_period_changes_the_expected_count():
    c = calculate_series_coverage(4320, 30, period_seconds=600, min_coverage_pct=80)   # 30 x 144 = 4320
    assert c.expected_datapoints == 4320 and c.coverage_pct == 100.0


# ------------------------------------------------------------------ input validation
@pytest.mark.parametrize("observed", [-1, 1.5, "10", None, True, False])
def test_invalid_observed_count_rejected(observed):
    with pytest.raises(ValueError):
        calculate_series_coverage(observed, 30)


@pytest.mark.parametrize("minimum", [0, -5, 100.1, float("nan"), True, "80"])
def test_invalid_minimum_rejected(minimum):
    with pytest.raises(ValueError):
        calculate_series_coverage(720, 30, min_coverage_pct=minimum)


# ------------------------------------------------------------------ output and helpers
def test_as_dict_is_json_serializable_and_rounded():
    import json

    d = calculate_series_coverage(600, 30, min_coverage_pct=80).as_dict()
    json.dumps(d)
    assert d["coverage_pct"] == 83.33 and d["status"] == STATUS_PARTIAL and d["meets_minimum"] is True
    assert d["expected_datapoints"] == 720 and d["min_coverage_pct"] == 80


def test_result_is_immutable():
    c = calculate_series_coverage(10, 30)
    with pytest.raises(Exception):
        c.coverage_pct = 100.0


def test_count_distinct_timestamps_deduplicates_pages_and_ignores_none():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    page1 = [start + timedelta(hours=i) for i in range(5)]
    page2 = [start + timedelta(hours=i) for i in range(3, 8)]      # hours 3 and 4 returned again
    assert count_distinct_timestamps(page1 + page2 + [None]) == 8
    assert count_distinct_timestamps([]) == 0
