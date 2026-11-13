"""Phase 2 tests: CloudWatch metrics collection (botocore Stubber validates request/response shapes)."""
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from botocore.stub import ANY, Stubber

from agent.services.metrics_collector import RESOURCE_METRICS, MetricsCollector
from agent.services.readonly_guard import ReadOnlySessionManager, ReadOnlyViolation
from agent.services.usage_pattern_config import load_usage_pattern_settings
from agent.services.usage_patterns import MetricsWindow, PatternThresholds

NOW = datetime(2026, 11, 11, 12, 30, tzinfo=timezone.utc)
END = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
ACCT = "111122223333"


class StubbedCW:
    def __init__(self):
        self.client = boto3.client("cloudwatch", region_name="us-east-1",
                                   aws_access_key_id="x", aws_secret_access_key="y")
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.calls = []

    def get_client(self, account_id, service_name, region=None):
        assert service_name == "cloudwatch"
        return self.client


def collector(sm, **kw):
    return MetricsCollector(ReadOnlySessionManager(sm), now_fn=lambda: NOW, **kw)


def hours(days, fn, end=END):
    n = int(days * 24)
    return [(end - timedelta(hours=n - i), fn(i)) for i in range(n)]


def result(qid, points, status="Complete"):
    return {"Id": qid, "Label": qid, "StatusCode": status,
            "Timestamps": [t for t, _ in points], "Values": [float(v) for _, v in points]}


def ec2_response(points_by_qid, status="Complete", next_token=None):
    resp = {"MetricDataResults": [result(q, p, status) for q, p in points_by_qid.items()]}
    if next_token:
        resp["NextToken"] = next_token
    return resp


def steady(i):
    return 40.0 + (i % 3)


# --------------------------------------------------------------------- happy path
def test_steady_30_day_ec2_series_is_classified_steady_with_preferred_sufficiency():
    sm = StubbedCW()
    pts = hours(30, steady)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts}),
                         {"MetricDataQueries": ANY, "StartTime": END - timedelta(days=30),
                          "EndTime": END, "ScanBy": "TimestampAscending"})
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    sm.stub.assert_no_pending_responses()

    assert out["errors"] == []
    assert out["api_calls"] == 1
    assert out["metrics_requested"] == 4
    assert out["window"]["preferred_days"] == 30 and out["window"]["minimum_days"] == 14
    assert out["window"]["period_seconds"] == 3600
    assert out["thresholds_used"]["steady_max_cv"] == 0.5
    up = out["resources"]["i-aaa"]["usage_pattern"]
    assert up["pattern"] == "steady"
    assert up["data_sufficiency"]["status"] == "preferred"
    assert out["summary"]["patterns"] == {"steady": 1}


def test_requests_only_the_expected_ec2_metrics():
    sm = StubbedCW()
    seen = {}

    def capture(params, **kw):
        seen["q"] = params["MetricDataQueries"]   # observe only; must not return a value

    sm.client.meta.events.register("provide-client-params.cloudwatch.GetMetricData", capture)
    pts = hours(30, steady)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts}))
    collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    names = [(q["MetricStat"]["Metric"]["MetricName"], q["MetricStat"]["Stat"]) for q in seen["q"]]
    assert names == [("CPUUtilization", "Average"), ("CPUUtilization", "Maximum"),
                     ("NetworkIn", "Sum"), ("NetworkOut", "Sum")]
    assert all(q["MetricStat"]["Metric"]["Namespace"] == "AWS/EC2" for q in seen["q"])
    assert all(q["MetricStat"]["Period"] == 3600 for q in seen["q"])


def test_20_days_is_minimum_sufficiency_not_preferred():
    sm = StubbedCW()
    pts = hours(20, steady)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts}))
    up = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])["resources"]["i-aaa"]["usage_pattern"]
    assert up["data_sufficiency"]["status"] == "minimum"
    assert up["pattern"] == "steady"


def test_under_14_days_is_insufficient_data_never_a_pattern():
    sm = StubbedCW()
    pts = hours(10, steady)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts}))
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    up = out["resources"]["i-aaa"]["usage_pattern"]
    assert up["pattern"] == "insufficient_data"
    assert up["data_sufficiency"]["status"] == "insufficient"
    assert out["summary"]["data_sufficiency"] == {"insufficient": 1}


def test_no_datapoints_is_insufficient_data():
    sm = StubbedCW()
    sm.stub.add_response("get_metric_data", ec2_response({"m0": [], "m1": [], "m2": [], "m3": []}))
    up = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-new"])["resources"]["i-new"]["usage_pattern"]
    assert up["pattern"] == "insufficient_data"


def test_resource_age_reported_when_created_at_given():
    sm = StubbedCW()
    pts = hours(30, steady)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts}))
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"],
                                created_at={"i-aaa": END - timedelta(days=5)})
    assert out["resources"]["i-aaa"]["resource_age_days"] == 5.0


# ---------------------------------------------------------------- lambda zero-fill
def test_lambda_count_metrics_are_zero_filled_so_sparse_invocations_are_bursty_not_steady():
    sm = StubbedCW()
    start = END - timedelta(days=30)
    spikes = [(start + timedelta(hours=h), 500) for h in (10, 200, 400, 600)]
    first = [(start, 0)]
    inv = first + spikes
    empty = [(start, 0)]
    sm.stub.add_response("get_metric_data", {"MetricDataResults": [
        result("m0", inv), result("m1", empty), result("m2", empty),
        result("m3", [(start, 100)]), result("m4", [(start, 200)]), result("m5", empty)]})
    out = collector(sm).collect(ACCT, "us-east-1", "lambda", ["fn-a"])
    entry = out["resources"]["fn-a"]
    assert out["metrics_requested"] == 6
    assert entry["metrics"]["invocations"]["count"] == 30 * 24      # zero-filled to full window
    assert entry["usage_pattern"]["pattern"] == "bursty"


def test_lambda_with_no_invocations_is_idle():
    sm = StubbedCW()
    start = END - timedelta(days=30)
    z = [(start, 0)]
    sm.stub.add_response("get_metric_data", {"MetricDataResults": [result(f"m{i}", z) for i in range(6)]})
    up = collector(sm).collect(ACCT, "us-east-1", "lambda", ["fn-a"])["resources"]["fn-a"]["usage_pattern"]
    assert up["pattern"] == "idle"


# ------------------------------------------------------------- chunking / paging
def test_queries_are_chunked_by_chunk_size():
    sm = StubbedCW()
    pts = hours(30, steady)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts}))
    sm.stub.add_response("get_metric_data", ec2_response({"m2": pts, "m3": pts}))
    out = collector(sm, chunk_size=2).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    sm.stub.assert_no_pending_responses()
    assert out["api_calls"] == 2
    assert out["resources"]["i-aaa"]["usage_pattern"]["pattern"] == "steady"


def test_next_token_pagination_merges_datapoints():
    sm = StubbedCW()
    pts = hours(30, steady)
    half = len(pts) // 2
    sm.stub.add_response("get_metric_data",
                         ec2_response({"m0": pts[:half], "m1": pts[:half], "m2": pts[:half], "m3": pts[:half]},
                                      next_token="tok"))
    sm.stub.add_response("get_metric_data",
                         ec2_response({"m0": pts[half:], "m1": pts[half:], "m2": pts[half:], "m3": pts[half:]}),
                         {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY,
                          "ScanBy": "TimestampAscending", "NextToken": "tok"})
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    sm.stub.assert_no_pending_responses()
    assert out["api_calls"] == 2
    assert out["resources"]["i-aaa"]["metrics"]["cpu_avg"]["count"] == 30 * 24


def test_duplicate_ids_are_deduplicated_and_empty_ids_make_no_calls():
    sm = StubbedCW()
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", [])
    assert out["api_calls"] == 0 and out["metrics_requested"] == 0 and out["resources"] == {}
    pts = hours(30, steady)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts}))
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa", "i-aaa"])
    assert out["metrics_requested"] == 4 and list(out["resources"]) == ["i-aaa"]


# ------------------------------------------------------------ failure behaviour
def test_access_denied_degrades_to_insufficient_data_and_names_the_permission():
    sm = StubbedCW()
    sm.stub.add_client_error("get_metric_data", service_error_code="AccessDenied",
                             service_message="not authorized", http_status_code=403)
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    assert out["errors"][0]["missing_permission"] == "cloudwatch:GetMetricData"
    entry = out["resources"]["i-aaa"]
    assert entry["metrics_available"] is False
    assert entry["usage_pattern"]["pattern"] == "insufficient_data"


def test_partial_data_status_produces_a_warning():
    sm = StubbedCW()
    pts = hours(30, steady)
    resp = ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts})
    resp["MetricDataResults"][0]["StatusCode"] = "PartialData"
    sm.stub.add_response("get_metric_data", resp)
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    assert any("PartialData" in w for w in out["warnings"])


def test_unsupported_resource_type_raises():
    with pytest.raises(ValueError):
        collector(StubbedCW()).collect(ACCT, "us-east-1", "s3", ["b"])


def test_invalid_window_and_chunk_size_rejected():
    with pytest.raises(ValueError):
        MetricsWindow(preferred_days=10, minimum_days=14)
    with pytest.raises(ValueError):
        MetricsWindow(minimum_days=0)
    with pytest.raises(ValueError):
        collector(StubbedCW(), chunk_size=0)


def test_only_get_metric_data_is_reachable_through_the_guard():
    from agent.services.readonly_guard import ReadOnlyViolation
    ro = ReadOnlySessionManager(StubbedCW()).get_client(ACCT, "cloudwatch", "us-east-1")
    for forbidden in ("put_metric_alarm", "delete_alarms", "put_dashboard", "put_metric_data"):
        with pytest.raises(ReadOnlyViolation):
            getattr(ro, forbidden)


# ------------------------------- configurable idle thresholds and high-CPU protection
NON_IDLE = {"steady", "diurnal", "bursty", "variable"}


def run_ec2(avg_fn, max_fn, **kw):
    sm = StubbedCW()
    avg, mx, net = hours(30, avg_fn), hours(30, max_fn), hours(30, lambda i: 1000.0)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": avg, "m1": mx, "m2": net, "m3": net}))
    out = collector(sm, **kw).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    sm.stub.assert_no_pending_responses()
    return out, out["resources"]["i-aaa"]["usage_pattern"]


def run_rds(avg_fn, max_fn, **kw):
    sm = StubbedCW()
    avg, mx, other = hours(30, avg_fn), hours(30, max_fn), hours(30, lambda i: 10.0)
    sm.stub.add_response("get_metric_data", ec2_response(
        {"m0": avg, "m1": mx, "m2": other, "m3": other, "m4": other, "m5": other, "m6": other}))
    out = collector(sm, **kw).collect(ACCT, "us-east-1", "rds", ["db-aaa"])
    sm.stub.assert_no_pending_responses()
    return out, out["resources"]["db-aaa"]["usage_pattern"]


def test_resource_rules_point_at_threshold_fields_and_the_maximum_series():
    expected = {
        "ec2": ("idle_ec2_cpu_pct", True, "cpu_max"),
        "rds": ("idle_rds_cpu_pct", True, "cpu_max"),
        "lambda": ("idle_lambda_invocations_per_hour", False, None),
        "nat_gateway": ("idle_nat_bytes_per_hour", False, None),
    }
    for resource_type, (key, guard, peak) in expected.items():
        spec = RESOURCE_METRICS[resource_type]
        assert (spec.rule.idle_level_key, spec.rule.peak_guard, spec.peak) == (key, guard, peak)
        if peak:
            metric = {m.key: m for m in spec.specs}[peak]
            assert (metric.metric_name, metric.stat) == ("CPUUtilization", "Maximum")


def test_ec2_low_average_but_high_maximum_is_not_idle():
    out, up = run_ec2(lambda i: 2.0, lambda i: 90.0 if i == 100 else 4.0)
    assert up["pattern"] in NON_IDLE
    assert "idle_blocked_by_cpu_peak" in up["flags"]
    # The classifier received the Maximum-statistic series, not the Average one.
    assert up["stats"]["peak_cpu_pct"] == 90.0
    assert out["resources"]["i-aaa"]["metrics"]["cpu_avg"]["max"] == 2.0
    assert out["resources"]["i-aaa"]["metrics"]["cpu_max"]["max"] == 90.0


def test_ec2_low_average_and_low_maximum_is_idle():
    _, up = run_ec2(lambda i: 2.0, lambda i: 20.0 if i == 100 else 4.0)
    assert up["pattern"] == "idle"
    assert up["flags"] == []
    assert up["stats"]["peak_cpu_pct"] == 20.0


def test_ec2_without_maximum_datapoints_is_not_called_idle():
    sm = StubbedCW()
    avg, net = hours(30, lambda i: 2.0), hours(30, lambda i: 1000.0)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": avg, "m1": [], "m2": net, "m3": net}))
    up = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])["resources"]["i-aaa"]["usage_pattern"]
    assert up["pattern"] in NON_IDLE
    assert "idle_peak_unverified" in up["flags"]


def test_rds_high_maximum_blocks_idle_and_low_maximum_allows_it():
    _, blocked = run_rds(lambda i: 2.0, lambda i: 85.0 if i == 50 else 3.0)
    _, idle = run_rds(lambda i: 2.0, lambda i: 12.0 if i == 50 else 3.0)
    assert blocked["pattern"] in NON_IDLE
    assert "idle_blocked_by_cpu_peak" in blocked["flags"]
    assert blocked["stats"]["peak_cpu_pct"] == 85.0
    assert idle["pattern"] == "idle"


def test_ec2_idle_threshold_is_configurable_and_echoed_in_thresholds_used():
    default_out, default_up = run_ec2(lambda i: 8.0, lambda i: 9.0)
    assert default_up["pattern"] == "steady"

    custom = PatternThresholds(idle_ec2_cpu_pct=10.0)
    out, up = run_ec2(lambda i: 8.0, lambda i: 9.0, thresholds=custom)
    assert up["pattern"] == "idle"
    assert out["thresholds_used"] == custom.as_dict()
    assert up["thresholds_used"] == custom.as_dict()
    for key in ("idle_ec2_cpu_pct", "idle_rds_cpu_pct", "idle_lambda_invocations_per_hour",
                "idle_nat_bytes_per_hour", "idle_cpu_peak_pct"):
        assert key in out["thresholds_used"]
    assert default_out["thresholds_used"]["idle_ec2_cpu_pct"] == 5.0


def test_rds_idle_threshold_is_independent_of_the_ec2_threshold():
    _, with_ec2_only = run_rds(lambda i: 8.0, lambda i: 9.0, thresholds=PatternThresholds(idle_ec2_cpu_pct=10.0))
    _, with_rds = run_rds(lambda i: 8.0, lambda i: 9.0, thresholds=PatternThresholds(idle_rds_cpu_pct=10.0))
    assert with_ec2_only["pattern"] == "steady"
    assert with_rds["pattern"] == "idle"


def test_peak_threshold_is_configurable_through_the_collector():
    spike = lambda i: 40.0 if i == 100 else 4.0
    _, default = run_ec2(lambda i: 2.0, spike)
    _, relaxed = run_ec2(lambda i: 2.0, spike, thresholds=PatternThresholds(idle_cpu_peak_pct=50.0))
    assert default["pattern"] in NON_IDLE
    assert relaxed["pattern"] == "idle"


def test_lambda_idle_threshold_is_configurable():
    def run(thresholds=None):
        sm = StubbedCW()
        inv = hours(30, lambda i: 1.0)
        results = [result("m0", inv)] + [result(f"m{i}", []) for i in range(1, 6)]
        sm.stub.add_response("get_metric_data", {"MetricDataResults": results})
        kw = {"thresholds": thresholds} if thresholds else {}
        out = collector(sm, **kw).collect(ACCT, "us-east-1", "lambda", ["fn-a"])
        return out, out["resources"]["fn-a"]["usage_pattern"]

    _, default = run()
    out, relaxed = run(PatternThresholds(idle_lambda_invocations_per_hour=1.0))
    assert default["pattern"] == "steady"      # 1 invocation every hour is activity at the default level 0
    assert relaxed["pattern"] == "idle"
    assert out["thresholds_used"]["idle_lambda_invocations_per_hour"] == 1.0


def test_nat_gateway_idle_threshold_is_configurable():
    def run(thresholds=None):
        sm = StubbedCW()
        sm.stub.add_response("get_metric_data", {"MetricDataResults": [
            result("m0", hours(30, lambda i: 500.0)), result("m1", [])]})
        kw = {"thresholds": thresholds} if thresholds else {}
        out = collector(sm, **kw).collect(ACCT, "us-east-1", "nat_gateway", ["nat-1"])
        return out, out["resources"]["nat-1"]["usage_pattern"]

    _, default = run()
    out, relaxed = run(PatternThresholds(idle_nat_bytes_per_hour=1000.0))
    assert default["pattern"] == "steady"
    assert relaxed["pattern"] == "idle"
    assert out["thresholds_used"]["idle_nat_bytes_per_hour"] == 1000.0


def test_window_and_thresholds_loaded_from_config_drive_the_collector():
    settings = load_usage_pattern_settings({"usage_patterns": {
        "window": {"preferred_days": 20, "minimum_days": 7},
        "thresholds": {"idle_ec2_cpu_pct": 10.0},
    }})
    sm = StubbedCW()
    pts = hours(20, lambda i: 8.0)
    sm.stub.add_response("get_metric_data", ec2_response({"m0": pts, "m1": pts, "m2": pts, "m3": pts}),
                         {"MetricDataQueries": ANY, "StartTime": END - timedelta(days=20),
                          "EndTime": END, "ScanBy": "TimestampAscending"})
    out = collector(sm, window=settings.window, thresholds=settings.thresholds).collect(
        ACCT, "us-east-1", "ec2", ["i-aaa"])
    sm.stub.assert_no_pending_responses()
    up = out["resources"]["i-aaa"]["usage_pattern"]
    assert out["window"]["preferred_days"] == 20 and out["window"]["minimum_days"] == 7
    assert up["data_sufficiency"]["status"] == "preferred"
    assert up["pattern"] == "idle"
    assert out["thresholds_used"]["idle_ec2_cpu_pct"] == 10.0


def test_absent_config_gives_the_same_collector_as_the_built_in_defaults():
    settings = load_usage_pattern_settings(None)
    c = collector(StubbedCW(), window=settings.window, thresholds=settings.thresholds)
    assert c.window == MetricsWindow()
    out = c.collect(ACCT, "us-east-1", "ec2", [])
    assert out["thresholds_used"] == PatternThresholds().as_dict()


# ----------------------------------------------- PartialData handling (R-6)
def test_partial_data_on_a_page_with_next_token_is_not_flagged_once_pagination_completes():
    sm = StubbedCW()
    pts = hours(30, steady)
    half = len(pts) // 2
    qids = ("m0", "m1", "m2", "m3")
    sm.stub.add_response("get_metric_data",
                         ec2_response({q: pts[:half] for q in qids}, status="PartialData", next_token="tok"))
    sm.stub.add_response("get_metric_data", ec2_response({q: pts[half:] for q in qids}),
                         {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY,
                          "ScanBy": "TimestampAscending", "NextToken": "tok"})
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    sm.stub.assert_no_pending_responses()
    assert out["warnings"] == [] and out["errors"] == []
    assert out["resources"]["i-aaa"]["metrics"]["cpu_avg"]["count"] == 30 * 24


def test_partial_data_on_the_final_page_is_still_flagged():
    sm = StubbedCW()
    pts = hours(30, steady)
    half = len(pts) // 2
    qids = ("m0", "m1", "m2", "m3")
    sm.stub.add_response("get_metric_data", ec2_response({q: pts[:half] for q in qids}, next_token="tok"))
    sm.stub.add_response("get_metric_data", ec2_response({q: pts[half:] for q in qids}, status="PartialData"),
                         {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY,
                          "ScanBy": "TimestampAscending", "NextToken": "tok"})
    out = collector(sm).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    assert any("PartialData" in w for w in out["warnings"])


# --------------------------------- ReadOnlyViolation / network failures (R-6)
class _FixedSM:
    def __init__(self, client=None, raises=None):
        self.client, self.raises = client, raises

    def get_client(self, account_id, service_name, region=None):
        if self.raises is not None:
            raise self.raises
        return self.client


class _FailingCW:
    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def get_metric_data(self, **kwargs):
        self.calls += 1
        raise self.exc


def test_read_only_violation_from_client_creation_is_re_raised_not_downgraded():
    sm = _FixedSM(raises=ReadOnlyViolation("blocked"))
    with pytest.raises(ReadOnlyViolation):
        MetricsCollector(sm, now_fn=lambda: NOW).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])


def test_read_only_violation_during_get_metric_data_is_re_raised():
    cw = _FailingCW(ReadOnlyViolation("blocked"))
    with pytest.raises(ReadOnlyViolation):
        collector(_FixedSM(cw)).collect(ACCT, "us-east-1", "ec2", ["i-aaa"])
    assert cw.calls == 1


_URL = "https://monitoring.us-east-1.amazonaws.com"


@pytest.mark.parametrize("exc", [
    ReadTimeoutError(endpoint_url=_URL),
    ConnectTimeoutError(endpoint_url=_URL),
    EndpointConnectionError(endpoint_url=_URL),
    RuntimeError("socket closed"),
])
def test_network_and_timeout_errors_are_reported_not_raised(exc):
    cw = _FailingCW(exc)
    out = collector(_FixedSM(cw)).collect(ACCT, "us-east-1", "ec2", ["i-aaa", "i-bbb"])
    assert cw.calls == 1                                   # no retry loop of our own
    assert out["errors"][0]["api"] == "get_metric_data" and out["errors"][0]["code"] == type(exc).__name__
    assert "missing_permission" not in out["errors"][0]
    for rid in ("i-aaa", "i-bbb"):
        assert out["resources"][rid]["metrics_available"] is False
        assert out["resources"][rid]["usage_pattern"]["pattern"] == "insufficient_data"
    assert out["summary"]["patterns"] == {"insufficient_data": 2}
