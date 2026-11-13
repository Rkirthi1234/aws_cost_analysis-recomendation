"""Usage-pattern detection over CloudWatch time series (pure functions, no AWS calls).

This module only *describes* how a resource is used. It never recommends a
migration, assigns a confidence level or estimates savings; those belong to a
later phase.

Series are hourly (``PERIOD_SECONDS``). Two kinds of metrics exist:

* gauge-like (CPU, durations): a missing hour means the resource reported nothing
  (stopped, not yet created), so gaps are left as gaps and reflected in
  ``coverage_pct``;
* count-like (invocations, bytes): a missing hour means zero activity, so the
  series is zero-filled from the first datapoint up to the end of the window.
"""
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

PERIOD_SECONDS = 3600
_HOUR = timedelta(seconds=PERIOD_SECONDS)

Point = Tuple[datetime, float]

PATTERN_IDLE = "idle"
PATTERN_STEADY = "steady"
PATTERN_DIURNAL = "diurnal"
PATTERN_BURSTY = "bursty"
PATTERN_VARIABLE = "variable"
PATTERN_INSUFFICIENT = "insufficient_data"

SUFFICIENCY_PREFERRED = "preferred"
SUFFICIENCY_MINIMUM = "minimum"
SUFFICIENCY_INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class MetricsWindow:
    """Approved metrics window: 30 days preferred, 14 days minimum."""

    preferred_days: int = 30
    minimum_days: int = 14

    def __post_init__(self) -> None:
        if self.minimum_days < 1:
            raise ValueError("minimum_days must be >= 1")
        if self.preferred_days < self.minimum_days:
            raise ValueError("preferred_days must be >= minimum_days")

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class PatternThresholds:
    """Pattern thresholds.

    PROPOSED DEFAULTS pending approval. They are deliberately explicit,
    overridable and echoed in every result (``thresholds_used``), like the
    high-cost minimum cost threshold.
    """

    steady_max_cv: float = 0.5               # std / mean of the hourly series
    steady_min_active_fraction: float = 0.9  # share of hours above the idle level
    diurnal_min_eta_squared: float = 0.5     # share of variance explained by hour of day
    bursty_min_peak_to_mean: float = 3.0     # max / mean of the hourly series
    low_coverage_pct: float = 80.0           # gauge series: flag when fewer hours reported

    # Idle levels. "Idle" means the series statistic named by the resource's SeriesRule
    # (p95 for CPU, max for counts) is at or below the level.
    idle_ec2_cpu_pct: float = 5.0                    # p95 of hourly average CPU %, EC2
    idle_rds_cpu_pct: float = 5.0                    # p95 of hourly average CPU %, RDS
    idle_lambda_invocations_per_hour: float = 0.0    # max invocations in any hour, Lambda
    idle_nat_bytes_per_hour: float = 0.0             # max BytesOutToDestination in any hour, NAT
    # High-CPU protection (EC2/RDS): a resource whose highest hourly peak CPU % (Maximum
    # statistic) is above this is never classified idle on a low p95 alone.
    idle_cpu_peak_pct: float = 30.0

    def __post_init__(self) -> None:
        for f in fields(self):
            validate_threshold(f.name, getattr(self, f.name))

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)


# name -> (minimum, maximum or None). Used to validate every threshold, however it is supplied.
THRESHOLD_LIMITS: Dict[str, Tuple[float, Optional[float]]] = {
    "steady_max_cv": (0.0, None),
    "steady_min_active_fraction": (0.0, 1.0),
    "diurnal_min_eta_squared": (0.0, 1.0),
    "bursty_min_peak_to_mean": (1.0, None),
    "low_coverage_pct": (0.0, 100.0),
    "idle_ec2_cpu_pct": (0.0, 100.0),
    "idle_rds_cpu_pct": (0.0, 100.0),
    "idle_lambda_invocations_per_hour": (0.0, None),
    "idle_nat_bytes_per_hour": (0.0, None),
    "idle_cpu_peak_pct": (0.0, 100.0),
}


def validate_threshold(name: str, value: Any) -> float:
    """Return ``value`` as a float, or raise ValueError if it is not a valid setting for ``name``."""
    if name not in THRESHOLD_LIMITS:
        raise ValueError(f"unknown threshold '{name}'")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    low, high = THRESHOLD_LIMITS[name]
    if value < low or (high is not None and value > high):
        bound = f"at least {low}" if high is None else f"between {low} and {high}"
        raise ValueError(f"{name} must be {bound}, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class SeriesRule:
    """How to read one metric when classifying it.

    ``idle_stat`` is the statistic compared with ``idle_level``: "p95" for
    percentage gauges (CPU), "max" for counts where any activity matters.
    An hour is "active" when its value is greater than ``idle_level``.

    The idle level is either a fixed ``idle_level`` or, preferably, the name of a
    ``PatternThresholds`` field in ``idle_level_key`` so it is configurable; when
    both are given the threshold wins. ``peak_guard`` turns on high-CPU protection:
    the series is not called idle when its peak (see ``classify_pattern``'s
    ``peak_series``) exceeds ``PatternThresholds.idle_cpu_peak_pct``.
    """

    idle_level: Optional[float] = None
    idle_stat: str = "p95"
    idle_level_key: Optional[str] = None
    peak_guard: bool = False

    def __post_init__(self) -> None:
        if self.idle_stat not in ("p95", "max"):
            raise ValueError("idle_stat must be 'p95' or 'max'")
        if self.idle_level is None and self.idle_level_key is None:
            raise ValueError("SeriesRule needs idle_level or idle_level_key")
        if self.idle_level_key is not None and self.idle_level_key not in THRESHOLD_LIMITS:
            raise ValueError(f"idle_level_key '{self.idle_level_key}' is not a PatternThresholds field")

    def resolve_idle_level(self, thresholds: "PatternThresholds") -> float:
        if self.idle_level_key is not None:
            return float(getattr(thresholds, self.idle_level_key))
        return float(self.idle_level)


# ------------------------------------------------------------------ primitives
def _hour(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile of an already sorted sequence (q in 0..100)."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q / 100.0
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    frac = pos - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def build_series(points: Sequence[Point], *, window_end: datetime, fill_zero: bool) -> List[Point]:
    """Turn raw datapoints into an hourly series.

    ``fill_zero`` zero-fills every missing hour from the first datapoint to
    ``window_end`` (exclusive); otherwise gaps are kept as gaps.
    """
    by_hour: Dict[datetime, float] = {}
    for ts, value in points:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        by_hour[_hour(ts)] = float(value)
    if not by_hour:
        return []
    ordered = sorted(by_hour)
    if not fill_zero:
        return [(h, by_hour[h]) for h in ordered]

    end = _hour(window_end)
    last = max(ordered[-1], end - _HOUR)
    series: List[Point] = []
    h = ordered[0]
    while h <= last:
        series.append((h, by_hour.get(h, 0.0)))
        h += _HOUR
    return series


def summarize(series: Sequence[Point]) -> Dict[str, Any]:
    values = sorted(v for _, v in series)
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": _r(sum(values) / len(values)),
        "min": _r(values[0]),
        "p50": _r(percentile(values, 50)),
        "p95": _r(percentile(values, 95)),
        "p99": _r(percentile(values, 99)),
        "max": _r(values[-1]),
    }


def span_days(series: Sequence[Point]) -> float:
    """Days of history covered, first to last hour inclusive."""
    if not series:
        return 0.0
    return (series[-1][0] - series[0][0]).total_seconds() / 86400.0 + PERIOD_SECONDS / 86400.0


def assess_sufficiency(series: Sequence[Point], window: MetricsWindow) -> Dict[str, Any]:
    days = span_days(series)
    eps = 1e-9
    if days + eps >= window.preferred_days:
        status = SUFFICIENCY_PREFERRED
    elif days + eps >= window.minimum_days:
        status = SUFFICIENCY_MINIMUM
    else:
        status = SUFFICIENCY_INSUFFICIENT
    return {
        "status": status,
        "observed_days": _r(days, 2),
        "preferred_days": window.preferred_days,
        "minimum_days": window.minimum_days,
    }


def coverage_pct(series: Sequence[Point]) -> Optional[float]:
    if not series:
        return None
    expected = round(span_days(series) * 24)
    return _r(min(100.0, len(series) / expected * 100.0), 1) if expected else None


def hour_of_day_profile(series: Sequence[Point]) -> Dict[int, float]:
    groups: Dict[int, List[float]] = defaultdict(list)
    for ts, v in series:
        groups[ts.hour].append(v)
    return {h: sum(vs) / len(vs) for h, vs in sorted(groups.items())}


def eta_squared_by_hour(series: Sequence[Point]) -> float:
    """Share of total variance explained by the hour of day (0..1)."""
    values = [v for _, v in series]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    ss_total = sum((v - mean) ** 2 for v in values)
    if ss_total == 0:
        return 0.0
    groups: Dict[int, List[float]] = defaultdict(list)
    for ts, v in series:
        groups[ts.hour].append(v)
    ss_between = sum(len(vs) * ((sum(vs) / len(vs)) - mean) ** 2 for vs in groups.values())
    return ss_between / ss_total


# --------------------------------------------------------------- classification
def classify_pattern(
    series: Sequence[Point],
    rule: SeriesRule,
    window: MetricsWindow,
    thresholds: PatternThresholds,
    peak_series: Optional[Sequence[Point]] = None,
) -> Dict[str, Any]:
    """Describe the usage pattern of one hourly series. Descriptive only.

    ``peak_series`` is the hourly *Maximum* statistic of the same metric; it is only
    read when ``rule.peak_guard`` is set (high-CPU protection for EC2/RDS).
    """
    idle_level = rule.resolve_idle_level(thresholds)
    result: Dict[str, Any] = {
        "pattern": PATTERN_INSUFFICIENT,
        "reasons": [],
        "flags": [],
        "data_sufficiency": assess_sufficiency(series, window),
        "stats": summarize(series),
        "rule": {
            "idle_level": idle_level,
            "idle_stat": rule.idle_stat,
            "idle_level_key": rule.idle_level_key,
            "peak_guard": rule.peak_guard,
        },
        "thresholds_used": thresholds.as_dict(),
    }
    suff = result["data_sufficiency"]

    if not series:
        result["reasons"].append("No datapoints were returned for the metrics window.")
        return result
    if suff["status"] == SUFFICIENCY_INSUFFICIENT:
        result["reasons"].append(
            f"Observed history of {suff['observed_days']} days is below the "
            f"{window.minimum_days}-day minimum."
        )
        return result

    values = sorted(v for _, v in series)
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    p95 = percentile(values, 95)
    idle_value = p95 if rule.idle_stat == "p95" else values[-1]
    active_fraction = sum(1 for v in values if v > idle_level) / n
    cv = std / mean if mean > 0 else None
    peak_to_mean = values[-1] / mean if mean > 0 else None
    eta2 = eta_squared_by_hour(series)
    cov = coverage_pct(series)

    result["stats"].update({
        "std": _r(std),
        "cv": _r(cv),
        "active_hour_fraction": _r(active_fraction),
        "peak_to_mean": _r(peak_to_mean),
        "hour_of_day_eta_squared": _r(eta2),
        "coverage_pct": cov,
    })
    profile = hour_of_day_profile(series)
    if profile:
        top = sorted(profile, key=lambda h: (-profile[h], h))[:3]
        result["stats"]["top_hours_utc"] = sorted(top)

    if cov is not None and cov < thresholds.low_coverage_pct:
        result["flags"].append("low_data_coverage")
        result["reasons"].append(
            f"Only {cov}% of hours reported data (resource may have been stopped or intermittent)."
        )

    # High-CPU protection: a low p95 alone must not make a resource with significant peaks idle.
    # If the peak cannot be verified (no peak data) the resource is not called idle either.
    peak_blocked = False
    if rule.peak_guard and idle_value <= idle_level:
        peak = max((v for _, v in (peak_series or [])), default=None)
        if peak is None:
            peak_blocked = True
            result["flags"].append("idle_peak_unverified")
            result["reasons"].append("Peak CPU data is unavailable, so the resource is not classified as idle.")
        else:
            result["stats"]["peak_cpu_pct"] = _r(peak)
            if peak > thresholds.idle_cpu_peak_pct:
                peak_blocked = True
                result["flags"].append("idle_blocked_by_cpu_peak")
                result["reasons"].append(
                    f"{rule.idle_stat} of the hourly series ({_r(idle_value)}) is at or below the idle level "
                    f"{idle_level}, but peak CPU {_r(peak)}% exceeds {thresholds.idle_cpu_peak_pct}%, "
                    "so it is not classified as idle."
                )

    if idle_value <= idle_level and not peak_blocked:
        result["pattern"] = PATTERN_IDLE
        result["reasons"].append(
            f"{rule.idle_stat} of the hourly series ({_r(idle_value)}) is at or below the idle level {idle_level}."
        )
    elif (cv is not None and cv <= thresholds.steady_max_cv
          and active_fraction >= thresholds.steady_min_active_fraction):
        result["pattern"] = PATTERN_STEADY
        result["reasons"].append(
            f"Coefficient of variation {_r(cv)} <= {thresholds.steady_max_cv} and "
            f"{_r(active_fraction)} of hours active."
        )
    elif eta2 >= thresholds.diurnal_min_eta_squared:
        result["pattern"] = PATTERN_DIURNAL
        result["reasons"].append(
            f"Hour of day explains {_r(eta2)} of variance (>= {thresholds.diurnal_min_eta_squared})."
        )
    elif peak_to_mean is not None and peak_to_mean >= thresholds.bursty_min_peak_to_mean:
        result["pattern"] = PATTERN_BURSTY
        result["reasons"].append(
            f"The peak hour is {_r(peak_to_mean)}x the mean (>= {thresholds.bursty_min_peak_to_mean}) without a daily cycle."
        )
    else:
        result["pattern"] = PATTERN_VARIABLE
        result["reasons"].append("Does not match idle, steady, diurnal or bursty definitions.")
    return result


def _r(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)
