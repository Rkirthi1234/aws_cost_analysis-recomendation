"""Per-series metric coverage (Step 38.5).

Pure functions. No AWS calls, no I/O. The metrics collector uses these functions
before zero-filling and exposes raw coverage alongside its existing summaries.

WHAT COVERAGE MEANS
    coverage = (raw datapoints actually returned) / (datapoints expected for the FULL window)

    * The numerator is the count of RAW CloudWatch datapoints, taken BEFORE any zero-filling.
      Datapoints that were filled in with 0 to complete a series are missing observations, not
      measurements, and must never be counted. The caller passes the raw count (see
      ``count_distinct_timestamps`` for de-duplicating datapoints merged from several pages).
    * The denominator is the whole expected window, not the span that happens to contain data.
      For hourly metrics that is ``window_days x 24``.
    * The counted numerator is capped at the expected count, so coverage never exceeds 100%
      (a window edge can return one extra datapoint); the uncapped raw count is still reported.

NOTE FOR COUNT METRICS
    For count-type series (for example Lambda invocations, NAT bytes) CloudWatch omits periods with
    no activity, so a sparse-but-valid series has low raw coverage. This module reports that
    honestly; deciding how such series are treated is a policy decision for the decision engine.
"""
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

SECONDS_PER_DAY = 86400
DEFAULT_PERIOD_SECONDS = 3600          # hourly series: window_days x 24 expected datapoints

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_EMPTY = "empty"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def expected_datapoints(window_days: float, period_seconds: int = DEFAULT_PERIOD_SECONDS) -> int:
    """Datapoints a series would have if every period in the full window were observed.

    ``expected_datapoints(30)`` is 720 for hourly metrics (30 x 24).
    """
    if not _is_number(window_days) or window_days <= 0:
        raise ValueError(f"window_days must be a positive number, got {window_days!r}")
    if isinstance(period_seconds, bool) or not isinstance(period_seconds, int) or period_seconds <= 0:
        raise ValueError(f"period_seconds must be a positive integer, got {period_seconds!r}")
    expected = math.floor(window_days * SECONDS_PER_DAY / period_seconds)
    if expected < 1:
        raise ValueError(
            f"window_days={window_days!r} is shorter than one period of {period_seconds} seconds"
        )
    return expected


def count_distinct_timestamps(timestamps: Iterable[Any]) -> int:
    """Number of distinct raw datapoint timestamps (``None`` ignored).

    Use it to turn datapoints merged from several ``GetMetricData`` pages into the raw count that
    ``calculate_series_coverage`` expects, so a datapoint returned twice is counted once.
    """
    return len({t for t in timestamps if t is not None})


@dataclass(frozen=True)
class SeriesCoverage:
    """Coverage of one metric series against the full expected window."""

    observed_datapoints: int          # raw datapoints supplied by the caller (uncapped)
    counted_datapoints: int           # observed, capped at expected
    expected_datapoints: int
    missing_datapoints: int
    coverage_pct: float               # counted / expected x 100, in [0, 100]
    status: str                       # complete | partial | empty
    window_days: float
    period_seconds: int
    min_coverage_pct: Optional[float] = None
    meets_minimum: Optional[bool] = None   # None when no minimum was supplied

    def as_dict(self) -> Dict[str, Any]:
        return {
            "observed_datapoints": self.observed_datapoints,
            "counted_datapoints": self.counted_datapoints,
            "expected_datapoints": self.expected_datapoints,
            "missing_datapoints": self.missing_datapoints,
            "coverage_pct": round(self.coverage_pct, 2),
            "status": self.status,
            "window_days": self.window_days,
            "period_seconds": self.period_seconds,
            "min_coverage_pct": self.min_coverage_pct,
            "meets_minimum": self.meets_minimum,
        }


def calculate_series_coverage(
    observed_datapoints: int,
    window_days: float,
    period_seconds: int = DEFAULT_PERIOD_SECONDS,
    min_coverage_pct: Optional[float] = None,
) -> SeriesCoverage:
    """Coverage of one series.

    Args:
        observed_datapoints: count of RAW datapoints returned for the series, before zero-filling.
        window_days: length of the full expected measurement window in days.
        period_seconds: metric period; 3600 gives ``window_days x 24`` expected datapoints.
        min_coverage_pct: optional minimum (0 < value <= 100). When given, ``meets_minimum`` is set;
            the comparison is exact (``coverage >= minimum`` passes at equality).

    Raises:
        ValueError: for a negative, non-integer or boolean count, or an invalid window, period or
            minimum. Invalid input is never coerced.
    """
    if isinstance(observed_datapoints, bool) or not isinstance(observed_datapoints, int) or observed_datapoints < 0:
        raise ValueError(f"observed_datapoints must be a non-negative integer, got {observed_datapoints!r}")
    if min_coverage_pct is not None and (not _is_number(min_coverage_pct) or not 0 < min_coverage_pct <= 100):
        raise ValueError(f"min_coverage_pct must be a number greater than 0 and at most 100, got {min_coverage_pct!r}")

    expected = expected_datapoints(window_days, period_seconds)
    counted = min(observed_datapoints, expected)
    coverage = counted * 100 / expected

    if counted == 0:
        status = STATUS_EMPTY
    elif counted == expected:
        status = STATUS_COMPLETE
    else:
        status = STATUS_PARTIAL

    meets: Optional[bool] = None
    if min_coverage_pct is not None:
        # Exact decimal comparison so a minimum such as 33.3 is not affected by float rounding.
        meets = Decimal(counted) * 100 >= Decimal(str(min_coverage_pct)) * Decimal(expected)

    return SeriesCoverage(
        observed_datapoints=observed_datapoints,
        counted_datapoints=counted,
        expected_datapoints=expected,
        missing_datapoints=expected - counted,
        coverage_pct=coverage,
        status=status,
        window_days=window_days,
        period_seconds=period_seconds,
        min_coverage_pct=min_coverage_pct,
        meets_minimum=meets,
    )
