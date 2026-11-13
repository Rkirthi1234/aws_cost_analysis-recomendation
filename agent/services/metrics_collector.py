"""CloudWatch metrics collection for usage-pattern detection (read-only).

Uses only ``cloudwatch:GetMetricData``. It never creates alarms, dashboards or
metric streams. GetMetricData is billed by AWS per metric requested, so every
result reports ``metrics_requested`` and callers cap how many resources are
queried.

Hourly datapoints are requested for the preferred window (default 30 days);
sufficiency is judged against the approved minimum (default 14 days).
"""
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import ClientError

from agent.services.readonly_guard import ReadOnlyViolation
from agent.services.metric_coverage import calculate_series_coverage
from agent.services.usage_patterns import (
    PERIOD_SECONDS,
    MetricsWindow,
    PatternThresholds,
    Point,
    SeriesRule,
    build_series,
    classify_pattern,
    summarize,
)

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 100   # metric queries per GetMetricData call (hourly x 30d = 72k points < 100,800 limit)
DEFAULT_MAX_PAGES = 20
_DENIED_CODES = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")


@dataclass(frozen=True)
class MetricSpec:
    key: str
    metric_name: str
    stat: str
    fill_zero: bool = False   # True for count-like metrics where a missing hour means zero


@dataclass(frozen=True)
class ResourceMetrics:
    namespace: str
    dimension: str
    specs: Tuple[MetricSpec, ...]
    primary: str              # metric key used for pattern classification
    rule: SeriesRule
    peak: Optional[str] = None  # metric key of the hourly Maximum series used by high-CPU protection


RESOURCE_METRICS: Dict[str, ResourceMetrics] = {
    "ec2": ResourceMetrics(
        namespace="AWS/EC2",
        dimension="InstanceId",
        specs=(
            MetricSpec("cpu_avg", "CPUUtilization", "Average"),
            MetricSpec("cpu_max", "CPUUtilization", "Maximum"),
            MetricSpec("network_in_bytes", "NetworkIn", "Sum"),
            MetricSpec("network_out_bytes", "NetworkOut", "Sum"),
        ),
        primary="cpu_avg",
        rule=SeriesRule(idle_level_key="idle_ec2_cpu_pct", idle_stat="p95", peak_guard=True),
        peak="cpu_max",
    ),
    "lambda": ResourceMetrics(
        namespace="AWS/Lambda",
        dimension="FunctionName",
        specs=(
            MetricSpec("invocations", "Invocations", "Sum", fill_zero=True),
            MetricSpec("errors", "Errors", "Sum", fill_zero=True),
            MetricSpec("throttles", "Throttles", "Sum", fill_zero=True),
            MetricSpec("duration_avg_ms", "Duration", "Average"),
            MetricSpec("duration_max_ms", "Duration", "Maximum"),
            MetricSpec("concurrent_executions_max", "ConcurrentExecutions", "Maximum", fill_zero=True),
        ),
        primary="invocations",
        rule=SeriesRule(idle_level_key="idle_lambda_invocations_per_hour", idle_stat="max"),
    ),
    "rds": ResourceMetrics(
        namespace="AWS/RDS",
        dimension="DBInstanceIdentifier",
        specs=(
            MetricSpec("cpu_avg", "CPUUtilization", "Average"),
            MetricSpec("cpu_max", "CPUUtilization", "Maximum"),
            MetricSpec("connections_avg", "DatabaseConnections", "Average"),
            MetricSpec("connections_max", "DatabaseConnections", "Maximum"),
            MetricSpec("freeable_memory_min_bytes", "FreeableMemory", "Minimum"),
            MetricSpec("read_iops_avg", "ReadIOPS", "Average"),
            MetricSpec("write_iops_avg", "WriteIOPS", "Average"),
        ),
        primary="cpu_avg",
        rule=SeriesRule(idle_level_key="idle_rds_cpu_pct", idle_stat="p95", peak_guard=True),
        peak="cpu_max",
    ),
    "nat_gateway": ResourceMetrics(
        namespace="AWS/NATGateway",
        dimension="NatGatewayId",
        specs=(
            MetricSpec("bytes_out_to_destination", "BytesOutToDestination", "Sum", fill_zero=True),
            MetricSpec("bytes_in_from_destination", "BytesInFromDestination", "Sum", fill_zero=True),
        ),
        primary="bytes_out_to_destination",
        rule=SeriesRule(idle_level_key="idle_nat_bytes_per_hour", idle_stat="max"),
    ),
}


class MetricsCollector:
    """Fetches hourly CloudWatch metrics and classifies each resource's usage pattern."""

    def __init__(
        self,
        session_manager: Any,
        window: Optional[MetricsWindow] = None,
        thresholds: Optional[PatternThresholds] = None,
        *,
        now_fn: Optional[Callable[[], datetime]] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ):
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._sm = session_manager
        self._window = window or MetricsWindow()
        self._thresholds = thresholds or PatternThresholds()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._chunk_size = chunk_size
        self._max_pages = max_pages

    @property
    def window(self) -> MetricsWindow:
        return self._window

    # ------------------------------------------------------------------ public
    def collect(
        self,
        account_id: str,
        region: Optional[str],
        resource_type: str,
        resource_ids: Sequence[str],
        *,
        created_at: Optional[Dict[str, datetime]] = None,
    ) -> Dict[str, Any]:
        """Collect metrics and usage patterns for the given resources.

        Returns a JSON-serializable dict; AWS failures are reported in
        ``errors`` and never raised.
        """
        if resource_type not in RESOURCE_METRICS:
            raise ValueError(f"Unsupported resource_type '{resource_type}'")
        spec = RESOURCE_METRICS[resource_type]
        ids = list(dict.fromkeys(resource_ids))

        end = _floor_hour(self._now_fn())
        start = end - timedelta(days=self._window.preferred_days)
        out: Dict[str, Any] = {
            "resource_type": resource_type,
            "window": {
                **self._window.as_dict(),
                "start": start.isoformat(),
                "end_exclusive": end.isoformat(),
                "period_seconds": PERIOD_SECONDS,
            },
            "thresholds_used": self._thresholds.as_dict(),
            "primary_metric": spec.primary,
            "metrics_requested": len(ids) * len(spec.specs),
            "api_calls": 0,
            "resources": {},
            "errors": [],
            "warnings": [],
        }
        if not ids:
            out["summary"] = self._summary({})
            return out

        queries: List[Dict[str, Any]] = []
        qmap: Dict[str, Tuple[str, str]] = {}
        for rid in ids:
            for ms in spec.specs:
                qid = f"m{len(queries)}"
                qmap[qid] = (rid, ms.key)
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": spec.namespace,
                            "MetricName": ms.metric_name,
                            "Dimensions": [{"Name": spec.dimension, "Value": rid}],
                        },
                        "Period": PERIOD_SECONDS,
                        "Stat": ms.stat,
                    },
                    "ReturnData": True,
                })

        raw: Dict[Tuple[str, str], List[Point]] = defaultdict(list)
        fetch_ok = self._fetch(account_id, region, queries, qmap, start, end, raw, out)

        results: Dict[str, Any] = {}
        for rid in ids:
            if not fetch_ok:
                results[rid] = {
                    "metrics_available": False,
                    "usage_pattern": {"pattern": "insufficient_data",
                                      "reasons": ["CloudWatch metrics could not be retrieved (see errors)."]},
                }
                continue
            metrics: Dict[str, Any] = {}
            coverage: Dict[str, Any] = {}
            primary_series: List[Point] = []
            peak_series: Optional[List[Point]] = None
            for ms in spec.specs:
                points = raw.get((rid, ms.key), [])
                # Coverage counts raw, finite observations in this exact window, before zero filling.
                observed = {t for t, v in points if start <= t < end
                            and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
                coverage[ms.key] = calculate_series_coverage(
                    len(observed), self._window.preferred_days, PERIOD_SECONDS
                ).as_dict()
                series = build_series(points, window_end=end, fill_zero=ms.fill_zero)
                metrics[ms.key] = summarize(series)
                if ms.key == spec.primary:
                    primary_series = series
                if spec.peak and ms.key == spec.peak:
                    peak_series = series
            entry: Dict[str, Any] = {
                "metrics_available": True,
                "metrics": metrics,
                "metric_coverage": coverage,
                "collection_complete": not out["warnings"],
                "usage_pattern": classify_pattern(
                    primary_series, spec.rule, self._window, self._thresholds, peak_series=peak_series
                ),
            }
            created = (created_at or {}).get(rid)
            if created is not None:
                entry["resource_age_days"] = round((end - created).total_seconds() / 86400.0, 2)
            results[rid] = entry

        out["resources"] = results
        out["summary"] = self._summary(results)
        return out

    # --------------------------------------------------------------- internals
    def _fetch(
        self,
        account_id: str,
        region: Optional[str],
        queries: List[Dict[str, Any]],
        qmap: Dict[str, Tuple[str, str]],
        start: datetime,
        end: datetime,
        raw: Dict[Tuple[str, str], List[Point]],
        out: Dict[str, Any],
    ) -> bool:
        try:
            cw = self._sm.get_client(account_id, "cloudwatch", region)
        except ReadOnlyViolation:
            raise  # a guard violation must surface, never be downgraded to a collection error
        except Exception as e:
            out["errors"].append({"api": "get_client", "code": type(e).__name__, "message": str(e)})
            return False

        partial = False
        for i in range(0, len(queries), self._chunk_size):
            chunk = queries[i:i + self._chunk_size]
            token = None
            for _ in range(self._max_pages):
                params: Dict[str, Any] = {
                    "MetricDataQueries": chunk,
                    "StartTime": start,
                    "EndTime": end,
                    "ScanBy": "TimestampAscending",
                }
                if token:
                    params["NextToken"] = token
                try:
                    resp = cw.get_metric_data(**params)
                except ReadOnlyViolation:
                    raise
                except ClientError as e:
                    out["errors"].append(_client_error(e))
                    return False
                except Exception as e:
                    # Network/timeout/endpoint failures (botocore errors etc.): report them and let the
                    # caller keep the inventory it already collected instead of failing the account.
                    out["errors"].append({"api": "get_metric_data", "code": type(e).__name__, "message": str(e)})
                    return False
                out["api_calls"] += 1
                token = resp.get("NextToken")
                for result in resp.get("MetricDataResults", []):
                    key = qmap.get(result.get("Id"))
                    if key is None:
                        continue
                    raw[key].extend(zip(result.get("Timestamps", []), result.get("Values", [])))
                    status = result.get("StatusCode")
                    # PartialData on a page that has a NextToken only means "more pages follow", which
                    # pagination retrieves; it is a real gap only on the last page.
                    if status in ("InternalError", "Forbidden") or (status == "PartialData" and not token):
                        partial = True
                if not token:
                    break
            else:
                out["warnings"].append(
                    f"GetMetricData pagination stopped after {self._max_pages} pages; results may be incomplete"
                )
        if partial:
            out["warnings"].append(
                "CloudWatch returned PartialData, InternalError or Forbidden for some metrics; "
                "affected resources may be under-reported"
            )
        return True

    @staticmethod
    def _summary(results: Dict[str, Any]) -> Dict[str, Any]:
        patterns: Dict[str, int] = defaultdict(int)
        sufficiency: Dict[str, int] = defaultdict(int)
        for r in results.values():
            up = r.get("usage_pattern", {})
            patterns[up.get("pattern", "insufficient_data")] += 1
            sufficiency[up.get("data_sufficiency", {}).get("status", "unavailable")] += 1
        return {"resources": len(results), "patterns": dict(patterns), "data_sufficiency": dict(sufficiency)}


def _floor_hour(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _client_error(e: ClientError) -> Dict[str, Any]:
    err = e.response.get("Error", {})
    code = err.get("Code", "Unknown")
    result = {"api": "get_metric_data", "code": code, "message": err.get("Message", str(e))}
    if code in _DENIED_CODES:
        result["hint"] = "The role needs cloudwatch:GetMetricData."
        result["missing_permission"] = "cloudwatch:GetMetricData"
    return result
