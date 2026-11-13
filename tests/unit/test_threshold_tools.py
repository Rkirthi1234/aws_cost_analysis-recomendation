"""Unit tests for agent/tools/threshold_tools.py (the cost threshold alert tool).

Everything runs against in-memory fakes: no live AWS calls are made. All amounts below are MOCKED
test inputs, not real AWS cost data. The existing detector, settings loader and de-duplicator are
used for real; only Cost Explorer is faked.
"""
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError

from agent.prompts.system_prompt import get_system_prompt
from agent.services.account_registry import AccountRegistry
from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.cost_thresholds import EC2_OTHER_NOTE, load_cost_threshold_settings
from agent.services.high_cost_identifier import HighCostServiceIdentifier
from agent.services.readonly_guard import ReadOnlySessionManager, ReadOnlyViolation
from agent.services.threshold_alerts import (
    InMemoryAlertStore,
    JsonFileAlertStore,
    ThresholdAlertDeduplicator,
)
from agent.tools.alternatives_tools import get_alternatives_tools, is_alternatives_enabled
from agent.tools.analysis_tools import AnalysisTools
from agent.tools.billing_tools import BillingTools
from agent.tools.date_tools import DateTools
from agent.tools.threshold_tools import (
    CONFIG_FLAG,
    TOOL_NAME,
    ThresholdTools,
    get_threshold_tools,
    is_threshold_detection_enabled,
)

PAYER = "111111111111"
M1 = "222222222222"
EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"
EC2_OTHER = "EC2 - Other"
S3 = "Amazon Simple Storage Service"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
START, END = "2026-11-01", "2026-11-12"

CROSSING = {EC2_COMPUTE: ("0.5", "USD"), EC2_OTHER: ("0.25", "USD"), S3: ("0.02", "USD")}
DEFAULT_CT = {"enabled": True, "currency": "USD", "services": {"ec2": 0.01, "s3": 0.01}}


# ------------------------------------------------------------------- fakes
class FakeConfig:
    """Mimics ConfigService: a ``config`` mapping and a dotted ``get``."""

    def __init__(self, data):
        self.config = data

    def get(self, path, default=None):
        value = self.config
        for key in path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value


class FakeCE:
    """Records every call; returns one Cost Explorer monthly bucket per entry of ``buckets``."""

    def __init__(self, buckets=None, error=None):
        self.buckets = buckets or []
        self.error = error
        self.calls = []

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        metric = kw["Metrics"][0]
        return {"ResultsByTime": [
            {"Groups": [
                {"Keys": [service], "Metrics": {metric: {"Amount": amount, "Unit": unit}}}
                for service, (amount, unit) in bucket.items()
            ]}
            for bucket in self.buckets
        ]}


class FakeSM:
    """SessionManager stand-in: hands out clients that record every operation."""

    def __init__(self, buckets=None):
        self.buckets = buckets if buckets is not None else [CROSSING]
        self.calls = []   # (account_id, service, region)
        self.ops = set()  # boto3 method names invoked
        self.writes = []  # must stay empty

    def get_client(self, account_id, service_name, region=None):
        self.calls.append((account_id, service_name, region))
        return FakeClient(self)


class FakeClient:
    def __init__(self, sm):
        self.sm = sm
        self.ce = FakeCE(sm.buckets)

    def get_cost_and_usage(self, **kw):
        self.sm.ops.add("get_cost_and_usage")
        return self.ce.get_cost_and_usage(**kw)

    # a write operation that must never be reachable through the tool
    def terminate_instances(self, **kw):
        self.sm.writes.append(("terminate_instances", kw))
        return {}


class NoPayerRegistry:
    def get_payer_account(self):
        return None


def make_registry():
    return AccountRegistry(
        [
            {"account_id": PAYER, "account_type": "payer", "region": "us-east-1",
             "role_arn": f"arn:aws:iam::{PAYER}:role/PayerRole"},
            {"account_id": M1, "account_type": "member", "region": "us-east-1",
             "role_arn": f"arn:aws:iam::{M1}:role/MemberRole"},
        ],
        "us-east-1",
    )


@pytest.fixture(scope="module")
def catalog():
    return AlternativesCatalog.load()


def make_tools(catalog, cost_thresholds=DEFAULT_CT, buckets=None, error=None, clock=None, deduplicator=None):
    """ThresholdTools wired to a FakeCE. Returns (tools, ce, built) where ``built`` records cost-source creation."""
    data = {"threshold_detection": {"enabled": True}}
    if cost_thresholds is not None:
        data["cost_thresholds"] = cost_thresholds
    settings = load_cost_threshold_settings(FakeConfig(data), catalog)
    ce = FakeCE([CROSSING] if buckets is None else buckets, error)
    built = []

    def factory():
        built.append(1)
        return HighCostServiceIdentifier(catalog=catalog, ce_client=ce)

    tools = ThresholdTools(None, make_registry(), catalog, settings, identifier_factory=factory,
                           deduplicator=deduplicator, clock=clock)
    return tools, ce, built


def tool_name(t):
    return getattr(t, "tool_name", None) or getattr(t, "__name__", None)


def run(fn, *args, **kwargs):
    text = fn(*args, **kwargs)
    assert isinstance(text, str)
    assert "NaN" not in text and "Infinity" not in text
    return json.loads(text)


def alert(out, key):
    return next(a for a in out["alerts"] if a["service_key"] == key)


def error_of(out):
    assert out["error"] is True and out["api"] == TOOL_NAME and "message" in out
    return out


def assert_no_delivery_or_recommendation(out):
    """Every response must say that nothing was sent and must carry no recommendation/savings data."""
    assert out["notification_sent"] is False
    if "notifications" in out:
        assert out["notifications"]["delivery"] == "not_performed"
        assert out["notifications"].get("marked_as_notified", False) is False
    keys = set()

    def walk(o):
        if isinstance(o, dict):
            keys.update(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(out)
    assert not keys & {"recommendation", "recommendations", "estimated_savings", "savings_usd", "pricing",
                       "compatibility", "alternative_id", "alternative"}
    assert "NOT an alternative-service recommendation" in out["note"] or "not an alternative" in out["note"].lower()


# ============================================================ tool execution: EC2 and S3
def test_both_services_crossed_return_the_required_fields(catalog):
    tools, ce, _ = make_tools(catalog)
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert out["alert_type"] == "cost_threshold_alert" and out["status"] == "evaluated" and out["enabled"] is True
    assert [a["service_key"] for a in out["alerts"]] == ["ec2", "s3"]
    assert out["crossed_service_keys"] == ["ec2", "s3"]
    assert out["currency"] == "USD" and out["metric"] == "AmortizedCost"
    assert out["period"]["start"] == START and out["period"]["end_exclusive"] == END

    ec2 = alert(out, "ec2")
    assert ec2["service_name"] and ec2["threshold_status"] == "threshold_crossed" and ec2["crossed"] is True
    assert ec2["actual_cost"] == 0.75 and ec2["actual_cost_exact"] == "0.75"       # compute 0.5 + EC2 - Other 0.25
    assert ec2["configured_threshold"] == 0.01 and ec2["configured_threshold_exact"] == "0.01"
    assert ec2["currency"] == "USD"
    assert ec2["period"] == {"start": START, "end_exclusive": END, "kind": "custom"}
    assert ec2["notification_status"] == "new_alert" and ec2["notification"]["notify"] is True
    assert {c["ce_service"] for c in ec2["cost_explorer_services"]} == {EC2_COMPUTE, EC2_OTHER}
    assert EC2_OTHER_NOTE in ec2["notes"]      # a crossing is not attributed entirely to EC2 instances

    s3 = alert(out, "s3")
    assert s3["actual_cost"] == 0.02 and s3["threshold_status"] == "threshold_crossed"
    assert s3["notification_status"] == "new_alert"

    assert out["summary"]["configured"] == 2 and out["summary"]["crossed"] == 2 and out["summary"]["new_alerts"] == 2
    assert ce.calls and ce.calls[0]["TimePeriod"] == {"Start": START, "End": END}
    assert_no_delivery_or_recommendation(out)


def test_threshold_crossed_and_not_crossed(catalog):
    tools, _, _ = make_tools(catalog, buckets=[{EC2_COMPUTE: ("0.5", "USD"), S3: ("0.005", "USD")}])
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert alert(out, "ec2")["threshold_status"] == "threshold_crossed" and alert(out, "ec2")["crossed"] is True
    s3 = alert(out, "s3")
    assert s3["threshold_status"] == "below_threshold" and s3["crossed"] is False
    assert s3["actual_cost"] == 0.005 and s3["configured_threshold"] == 0.01
    assert s3["notification_status"] == "not_applicable" and s3["notification"] is None
    assert out["crossed_service_keys"] == ["ec2"]
    assert out["summary"]["crossed"] == 1 and out["summary"]["below_threshold"] == 1 and out["summary"]["new_alerts"] == 1
    assert "next_step" in out
    assert_no_delivery_or_recommendation(out)


def test_cost_equal_to_the_threshold_counts_as_crossed(catalog):
    tools, _, _ = make_tools(catalog, buckets=[{EC2_COMPUTE: ("0.01", "USD"), S3: ("0.01", "USD")}])
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert [a["threshold_status"] for a in out["alerts"]] == ["threshold_crossed", "threshold_crossed"]


def test_nothing_crossed_has_no_next_step_and_no_new_alerts(catalog):
    tools, _, _ = make_tools(catalog, buckets=[{EC2_COMPUTE: ("0.001", "USD"), S3: ("0.001", "USD")}])
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert out["crossed_service_keys"] == [] and out["summary"]["new_alerts"] == 0 and "next_step" not in out
    assert all(a["notification_status"] == "not_applicable" for a in out["alerts"])


def test_default_period_is_month_to_date_using_the_clock(catalog):
    tools, ce, _ = make_tools(catalog, clock=lambda: date(2026, 11, 12))
    out = run(tools.detect_cost_threshold_alerts)
    assert out["period"]["start"] == "2026-11-01" and out["period"]["end_exclusive"] == "2026-11-12"
    assert out["period"]["kind"] == "month_to_date"
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-11-01", "End": "2026-11-12"}


def test_month_to_date_on_the_first_of_a_month_is_a_structured_error(catalog):
    tools, ce, _ = make_tools(catalog, clock=lambda: date(2026, 11, 1))
    out = error_of(run(tools.detect_cost_threshold_alerts))
    assert out["code"] == "PeriodEmpty" and ce.calls == []


def test_period_warnings_are_passed_through(catalog):
    tools, _, _ = make_tools(catalog)
    out = run(tools.detect_cost_threshold_alerts, "2026-10-01", "2026-12-01")
    assert any("several calendar months" in w for w in out["warnings"])
    out = run(tools.detect_cost_threshold_alerts, "2026-11-01", "2026-11-30")
    assert any("last day of a month" in w for w in out["warnings"])
    out = run(tools.detect_cost_threshold_alerts, "2026-11-01", "2026-12-01")
    assert not any("last day of a month" in w for w in out["warnings"])


# ============================================================ deduplication
def test_tool_never_marks_an_alert_as_notified(catalog):
    store = InMemoryAlertStore()
    tools, _, _ = make_tools(catalog, deduplicator=ThresholdAlertDeduplicator(store))
    first = run(tools.detect_cost_threshold_alerts, START, END)
    second = run(tools.detect_cost_threshold_alerts, START, END)
    for out in (first, second):
        assert [a["notification_status"] for a in out["alerts"]] == ["new_alert", "new_alert"]
        assert out["notifications"]["marked_as_notified"] is False and out["notifications"]["new"] == ["ec2", "s3"]
        assert out["notifications"]["delivery"] == "not_performed"
    for a in first["alerts"]:
        assert store.contains(a["notification"]["fingerprint"]) is False   # nothing was recorded


def test_already_notified_alerts_are_reported_and_suppressed(catalog):
    store = InMemoryAlertStore()
    tools, _, _ = make_tools(catalog, deduplicator=ThresholdAlertDeduplicator(store))
    first = run(tools.detect_cost_threshold_alerts, START, END)
    store.add(alert(first, "ec2")["notification"]["fingerprint"])   # what a notification workflow does after delivery

    out = run(tools.detect_cost_threshold_alerts, START, END)
    ec2, s3 = alert(out, "ec2"), alert(out, "s3")
    assert ec2["notification_status"] == "already_notified" and ec2["notification"]["notify"] is False
    assert ec2["crossed"] is True and ec2["actual_cost"] == 0.75          # the full result is still returned
    assert s3["notification_status"] == "new_alert"
    assert out["notifications"]["already_notified"] == ["ec2"] and out["notifications"]["new"] == ["s3"]
    assert out["summary"]["new_alerts"] == 1 and out["summary"]["already_notified"] == 1
    assert out["notification_sent"] is False                                # "already notified" is never "sent now"


def test_a_new_month_produces_a_new_alert(catalog):
    store = InMemoryAlertStore()
    tools, _, _ = make_tools(catalog, deduplicator=ThresholdAlertDeduplicator(store))
    november = run(tools.detect_cost_threshold_alerts, "2026-11-01", "2026-11-12")
    for a in november["alerts"]:
        store.add(a["notification"]["fingerprint"])
    again = run(tools.detect_cost_threshold_alerts, "2026-11-01", "2026-11-12")
    assert {a["notification_status"] for a in again["alerts"]} == {"already_notified"}
    october = run(tools.detect_cost_threshold_alerts, "2026-10-01", "2026-11-01")
    assert {a["notification_status"] for a in october["alerts"]} == {"new_alert"}


def test_optional_local_state_file_is_read_but_never_written_by_the_tool(catalog, tmp_path):
    state = tmp_path / "alerts.json"
    ct = {**DEFAULT_CT, "dedupe": {"state_file": str(state)}}
    tools, _, _ = make_tools(catalog, cost_thresholds=ct)
    first = run(tools.detect_cost_threshold_alerts, START, END)
    assert first["notifications"]["store"] == "local_file" and not state.exists()   # nothing marked, nothing written

    JsonFileAlertStore(str(state)).add(alert(first, "s3")["notification"]["fingerprint"])
    tools2, _, _ = make_tools(catalog, cost_thresholds=ct)                          # new process reads the file
    out = run(tools2.detect_cost_threshold_alerts, START, END)
    assert alert(out, "s3")["notification_status"] == "already_notified"
    assert alert(out, "ec2")["notification_status"] == "new_alert"


def test_deduplication_failure_does_not_hide_the_threshold_result(catalog):
    class Boom:
        def annotate(self, result, *, mark=False):
            raise RuntimeError("store exploded")

    tools, _, _ = make_tools(catalog, deduplicator=Boom())
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert out["status"] == "evaluated" and out["crossed_service_keys"] == ["ec2", "s3"]
    assert [a["notification_status"] for a in out["alerts"]] == ["unknown", "unknown"]
    assert out["notifications"] == {"available": False, "delivery": "not_performed"}
    assert any("de-duplication was unavailable" in w for w in out["warnings"])
    assert out["notification_sent"] is False


# ============================================================ configuration problems
@pytest.mark.parametrize("bad", [
    {"enabled": True, "services": {"nope": 1}},                                    # unknown service
    {"enabled": True, "services": {"ec2": 0}},                                     # threshold must be > 0
    {"enabled": True, "services": {"ec2": "ten"}},                                 # not a number
    {"enabled": True, "currency": "EUR", "services": {"ec2": 0.01}},               # unsupported configured currency
    {"enabled": True},                                                             # no services
    {"enabled": "yes", "services": {"ec2": 0.01}},                                 # not a boolean
])
def test_invalid_configuration_is_reported_without_calling_cost_explorer(catalog, bad):
    tools, ce, built = make_tools(catalog, cost_thresholds=bad)
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert out["status"] == "invalid_configuration" and out["enabled"] is False
    assert out["alerts"] == [] and out["errors"]
    assert ce.calls == [] and built == []            # no cost source was even created
    assert_no_delivery_or_recommendation(out)


def test_cost_thresholds_disabled_or_absent_returns_disabled_without_aws_access(catalog):
    for ct in ({**DEFAULT_CT, "enabled": False}, None):
        tools, ce, built = make_tools(catalog, cost_thresholds=ct)
        out = run(tools.detect_cost_threshold_alerts, START, END)
        assert out["status"] == "disabled" and out["enabled"] is False and out["alerts"] == []
        assert ce.calls == [] and built == []
        assert_no_delivery_or_recommendation(out)


# ============================================================ currency and missing data
def test_non_usd_units_are_not_compared_or_converted(catalog):
    tools, _, _ = make_tools(catalog, buckets=[{EC2_COMPUTE: ("0.5", "USD"), S3: ("999", "EUR")}])
    out = run(tools.detect_cost_threshold_alerts, START, END)
    s3 = alert(out, "s3")
    assert s3["threshold_status"] == "unsupported_currency" and s3["crossed"] is False
    assert s3["actual_cost"] is None and s3["notification_status"] == "not_applicable"
    assert "EUR" in s3["reason"] and "no conversion" in s3["reason"]
    assert out["summary"]["unsupported_currency"] == 1 and out["crossed_service_keys"] == ["ec2"]
    assert alert(out, "ec2")["threshold_status"] == "threshold_crossed"      # other services are unaffected


def test_missing_cost_data_is_reported_not_invented(catalog):
    tools, _, _ = make_tools(catalog, buckets=[{EC2_COMPUTE: ("0.5", "USD")}])      # no S3 line at all
    out = run(tools.detect_cost_threshold_alerts, START, END)
    s3 = alert(out, "s3")
    assert s3["threshold_status"] == "no_cost_data" and s3["crossed"] is False and s3["cost_data"] == "absent"
    assert s3["actual_cost"] == 0.0 and "no cost" in s3["reason"].lower()
    assert s3["notification_status"] == "not_applicable"
    assert out["summary"]["no_cost_data"] == 1

    tools, _, _ = make_tools(catalog, buckets=[])                                   # Cost Explorer returned nothing
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert [a["threshold_status"] for a in out["alerts"]] == ["no_cost_data", "no_cost_data"]
    assert out["crossed_service_keys"] == [] and "next_step" not in out


# ============================================================ input validation and error handling
def test_date_validation_happens_before_any_aws_access(catalog):
    tools, ce, built = make_tools(catalog)
    for start, end, code in (
        ("2026-11", "2026-12-01", "InvalidDate"),
        ("2026-11-01", "12/01/2026", "InvalidDate"),
        ("2026-02-30", "2026-03-01", "InvalidDate"),
        ("2026-11-12", "2026-11-01", "InvalidDateRange"),
        ("2026-11-01", "2026-11-01", "InvalidDateRange"),
        (START, None, "InvalidDate"),                 # both dates or neither
        (None, END, "InvalidDate"),
    ):
        assert error_of(run(tools.detect_cost_threshold_alerts, start, end))["code"] == code
    assert ce.calls == [] and built == []


def test_aws_api_errors_become_structured_results(catalog):
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetCostAndUsage")
    tools, _, _ = make_tools(catalog, error=denied)
    out = error_of(run(tools.detect_cost_threshold_alerts, START, END))
    assert out["code"] == "AccessDeniedException" and out["source_api"] == "get_cost_and_usage"
    assert "ce:GetCostAndUsage" in out["hint"]

    tools, _, _ = make_tools(catalog, error=RuntimeError("ce is down"))
    out = error_of(run(tools.detect_cost_threshold_alerts, START, END))
    assert out["exception_type"] == "RuntimeError" and "ce is down" in out["message"]


def test_unexpected_cost_source_failures_never_raise(catalog):
    class Raises:
        def __init__(self, exc):
            self.exc = exc

        def service_costs(self, *a):
            raise self.exc

    settings = load_cost_threshold_settings(FakeConfig({"cost_thresholds": DEFAULT_CT}), catalog)

    def build(source):
        return ThresholdTools(None, make_registry(), catalog, settings, identifier_factory=lambda: source)

    out = error_of(run(build(Raises(ReadOnlyViolation("'ce.put' is not an allowed read-only operation"))).detect_cost_threshold_alerts, START, END))
    assert out["code"] == "ReadOnlyViolation"
    out = error_of(run(build(Raises(ValueError("bad"))).detect_cost_threshold_alerts, START, END))
    assert out["exception_type"] == "ValueError"

    class ReturnsNone:
        def service_costs(self, *a):
            return None

    out = error_of(run(build(ReturnsNone()).detect_cost_threshold_alerts, START, END))
    assert out["code"] == "UnexpectedResult"

    def broken_factory():
        raise RuntimeError("factory failed")

    tools = ThresholdTools(None, make_registry(), catalog, settings, identifier_factory=broken_factory)
    assert error_of(run(tools.detect_cost_threshold_alerts, START, END))["exception_type"] == "RuntimeError"


def test_missing_payer_account_is_a_structured_error(catalog):
    settings = load_cost_threshold_settings(FakeConfig({"cost_thresholds": DEFAULT_CT}), catalog)
    tools = ThresholdTools(FakeSM(), NoPayerRegistry(), catalog, settings)
    out = error_of(run(tools.detect_cost_threshold_alerts, START, END))
    assert out["exception_type"] == "ValueError" and "payer" in out["message"].lower()


def test_aws_error_text_is_redacted_before_reaching_the_model(catalog):
    msg = ("User: arn:aws:sts::123456789012:assumed-role/AgentExecRole/s is not authorized to perform: "
           "ce:GetCostAndUsage on resource: arn:aws:iam::123456789012:role/PayerRole (ExternalId=hunter2)")
    for exc in (ClientError({"Error": {"Code": "AccessDeniedException", "Message": msg}}, "GetCostAndUsage"),
                RuntimeError(msg)):
        tools, _, _ = make_tools(catalog, error=exc)
        text = tools.detect_cost_threshold_alerts(START, END)
        for fragment in ("123456789012", "AgentExecRole", "PayerRole", "hunter2"):
            assert fragment not in text, fragment
        assert json.loads(text)["error"] is True


def test_outputs_do_not_contain_strings_that_could_break_the_stream(catalog):
    tools, _, _ = make_tools(catalog)
    text = tools.detect_cost_threshold_alerts(START, END)
    assert "[BLOCK_END]" not in text and "arn:aws:iam::" not in text


# ============================================================ read-only behaviour
def test_default_cost_source_goes_through_the_read_only_guard(catalog):
    settings = load_cost_threshold_settings(FakeConfig({"cost_thresholds": DEFAULT_CT}), catalog)
    sm = FakeSM()
    tools = ThresholdTools(sm, make_registry(), catalog, settings)
    identifier = tools._make_detector()._source
    assert isinstance(identifier, HighCostServiceIdentifier)
    assert isinstance(identifier._session_manager, ReadOnlySessionManager)
    ce = identifier._get_ce()
    assert callable(ce.get_cost_and_usage)
    for forbidden in ("terminate_instances", "create_cost_category_definition", "update_anomaly_monitor",
                      "delete_anomaly_monitor", "get_cost_forecast"):
        with pytest.raises(ReadOnlyViolation):
            getattr(ce, forbidden)


def test_a_full_run_only_calls_cost_explorer_read_operation_on_the_payer_account(catalog):
    settings = load_cost_threshold_settings(FakeConfig({"cost_thresholds": DEFAULT_CT}), catalog)
    sm = FakeSM()
    tools = ThresholdTools(sm, make_registry(), catalog, settings)
    out = run(tools.detect_cost_threshold_alerts, START, END)
    assert out["status"] == "evaluated" and out["crossed_service_keys"] == ["ec2", "s3"]
    assert sm.ops == {"get_cost_and_usage"} and sm.writes == []
    assert {(acct, svc) for acct, svc, _ in sm.calls} == {(PAYER, "ce")}
    assert "arn:aws:iam::" not in json.dumps(out)


def test_disabled_and_invalid_configurations_make_no_client_at_all(catalog):
    for ct in ({**DEFAULT_CT, "enabled": False}, {"enabled": True, "services": {"nope": 1}}):
        settings = load_cost_threshold_settings(FakeConfig({"cost_thresholds": ct}), catalog)
        sm = FakeSM()
        run(ThresholdTools(sm, make_registry(), catalog, settings).detect_cost_threshold_alerts, START, END)
        assert sm.calls == [] and sm.ops == set()


# ============================================================ feature flag
def test_flag_is_disabled_by_default_and_registers_nothing(catalog):
    for data in ({}, {"threshold_detection": {}}, {"threshold_detection": {"enabled": False}},
                 {"threshold_detection": {"enabled": None}},
                 {"cost_thresholds": DEFAULT_CT}):               # a configured/enabled detector does not register the tool
        cfg = FakeConfig(data)
        assert is_threshold_detection_enabled(cfg) is False
        assert get_threshold_tools(cfg, None, None) == []        # sentinels prove nothing is touched when disabled


def test_flag_must_be_a_real_boolean_true(catalog):
    for bad in ("true", "yes", 1, "enabled"):
        cfg = FakeConfig({"threshold_detection": {"enabled": bad}, "cost_thresholds": DEFAULT_CT})
        assert is_threshold_detection_enabled(cfg) is False
        assert get_threshold_tools(cfg, FakeSM(), make_registry()) == []
    assert is_threshold_detection_enabled(object()) is False


def test_flag_enabled_registers_exactly_one_tool(catalog):
    cfg = FakeConfig({"threshold_detection": {"enabled": True}, "cost_thresholds": DEFAULT_CT})
    assert CONFIG_FLAG == "threshold_detection.enabled" and is_threshold_detection_enabled(cfg) is True
    registered = get_threshold_tools(cfg, FakeSM(), make_registry())
    assert [tool_name(t) for t in registered] == [TOOL_NAME] == ["detect_cost_threshold_alerts"]


def test_flag_enabled_but_detector_not_enabled_registers_a_tool_that_reports_disabled(catalog):
    cfg = FakeConfig({"threshold_detection": {"enabled": True}})                    # no cost_thresholds section
    sm = FakeSM()
    registered = get_threshold_tools(cfg, sm, make_registry())
    assert len(registered) == 1
    out = run(registered[0], START, END)
    assert out["status"] == "disabled" and sm.calls == []


def test_initialization_failure_returns_empty_and_does_not_raise(catalog, monkeypatch):
    cfg = FakeConfig({"threshold_detection": {"enabled": True}, "cost_thresholds": DEFAULT_CT})

    def broken(cls, path=None):
        raise FileNotFoundError("catalog missing")

    monkeypatch.setattr(AlternativesCatalog, "load", classmethod(broken))
    assert get_threshold_tools(cfg, FakeSM(), make_registry()) == []
    monkeypatch.undo()
    assert len(get_threshold_tools(cfg, FakeSM(), make_registry())) == 1            # recovers once the catalog is fine


def test_threshold_flag_and_alternatives_flag_are_independent(catalog):
    only_threshold = FakeConfig({"threshold_detection": {"enabled": True}, "cost_thresholds": DEFAULT_CT})
    assert is_alternatives_enabled(only_threshold) is False
    assert get_alternatives_tools(only_threshold, FakeSM(), make_registry()) == []   # no alternatives tool enabled by accident
    assert len(get_threshold_tools(only_threshold, FakeSM(), make_registry())) == 1

    only_alternatives = FakeConfig({"alternatives": {"enabled": True}, "cost_thresholds": DEFAULT_CT})
    assert is_threshold_detection_enabled(only_alternatives) is False
    assert get_threshold_tools(only_alternatives, FakeSM(), make_registry()) == []
    assert len(get_alternatives_tools(only_alternatives, FakeSM(), make_registry())) == 3


def test_config_example_ships_the_flag_disabled_next_to_the_other_flags():
    example = yaml.safe_load((PROJECT_ROOT / "agent" / "config.yaml.example").read_text(encoding="utf-8"))
    assert example["threshold_detection"]["enabled"] is False
    assert example["cost_thresholds"]["enabled"] is False and example["alternatives"]["enabled"] is False


# ============================================================ agent registration
def test_existing_tools_are_unchanged_and_the_new_tool_name_does_not_collide(catalog):
    reg = make_registry()
    existing = (list(DateTools().get_tools()) + list(AnalysisTools().get_tools())
                + list(BillingTools(FakeSM(), reg).get_tools()))
    names = [tool_name(t) for t in existing]
    assert len(existing) == 1 + 2 + 43 and TOOL_NAME not in names

    cfg_off = FakeConfig({"cost_thresholds": DEFAULT_CT})
    assert [tool_name(t) for t in existing + get_threshold_tools(cfg_off, FakeSM(), reg)] == names

    cfg_on = FakeConfig({"threshold_detection": {"enabled": True}, "cost_thresholds": DEFAULT_CT})
    after = [tool_name(t) for t in existing + get_threshold_tools(cfg_on, FakeSM(), reg)]
    assert after[: len(names)] == names and after[len(names):] == [TOOL_NAME]
    assert len(set(after)) == len(after)


def test_agent_registers_the_tool_behind_its_flag_after_alternatives_and_before_mcp():
    src = (PROJECT_ROOT / "agent" / "agentcore_agent.py").read_text(encoding="utf-8")
    assert src.count("threshold_tools import") == 1                      # no module-level import
    assert src.count("alternatives_tools import") == 1                   # alternatives wiring untouched
    flag_check = src.index("if _threshold_flag_on(config):")
    assert src.index("get_alternatives_tools(") < flag_check < src.index("from agent.tools.threshold_tools import")
    assert src.index("from agent.tools.threshold_tools import") < src.index("tools.extend(mcp_clients)")
    assert "include_threshold_detection=threshold_registered" in src
    assert "include_alternatives=alternatives_registered" in src
    assert "if _alternatives_flag_on(config):" in src                    # the alternatives flag is still its own check


def test_agent_flag_helper_matches_the_documented_flag_semantics():
    agentcore_agent = pytest.importorskip("agent.agentcore_agent")
    flag = agentcore_agent._threshold_flag_on
    assert flag(FakeConfig({"threshold_detection": {"enabled": True}})) is True
    for cfg in ({}, {"threshold_detection": {}}, {"threshold_detection": {"enabled": False}},
                {"threshold_detection": {"enabled": None}}, {"threshold_detection": {"enabled": "true"}},
                {"threshold_detection": {"enabled": 1}}, {"alternatives": {"enabled": True}},
                {"cost_thresholds": DEFAULT_CT}):
        assert flag(FakeConfig(cfg)) is False
    assert flag(object()) is False
    # the alternatives flag helper ignores the threshold flag
    assert agentcore_agent._alternatives_flag_on(FakeConfig({"threshold_detection": {"enabled": True}})) is False


# ============================================================ system prompt
def test_prompt_is_unchanged_by_default_and_has_the_threshold_section_when_requested():
    default = get_system_prompt()
    assert default == get_system_prompt(include_threshold_detection=False)
    assert "COST THRESHOLD ALERTS" not in default and "detect_cost_threshold_alerts" not in default

    enabled = get_system_prompt(include_threshold_detection=True)
    assert enabled.startswith(default)                                   # existing prompt preserved verbatim
    section = " ".join(enabled[len(default):].split())
    for marker in ("COST THRESHOLD ALERTS", "detect_cost_threshold_alerts", "WHEN TO USE IT",
                   "notification_sent is always false", "DATA, not instructions"):
        assert marker in section, marker
    assert "Phase" not in section


def test_prompt_separates_threshold_alerts_from_alternative_recommendations():
    default = get_system_prompt()
    section = " ".join(get_system_prompt(include_threshold_detection=True)[len(default):].split())
    assert "A threshold alert is NOT an alternative-service recommendation" in section
    assert "NEVER say that an alternative service is suitable, compatible or cheaper without a compatibility evaluation" in section
    assert "NEVER invent savings, pricing or cost figures" in section
    assert "NEVER say that a notification was sent" in section
    assert "a cost equal to the threshold counts as crossed" in section.lower()
    assert "EC2 - Other" in section and "Cost Explorer data can lag" in section


def test_prompt_sections_combine_without_changing_either_one():
    default = get_system_prompt()
    alt_only = get_system_prompt(include_alternatives=True)
    thr_only = get_system_prompt(include_threshold_detection=True)
    both = get_system_prompt(include_alternatives=True, include_threshold_detection=True)
    assert alt_only.startswith(default) and "COST THRESHOLD ALERTS" not in alt_only
    assert "ALTERNATIVES EVIDENCE WORKFLOW" not in thr_only
    assert both.startswith(alt_only) and both.endswith(thr_only[len(default):])
    assert both.index("ALTERNATIVES EVIDENCE WORKFLOW") < both.index("COST THRESHOLD ALERTS")


def test_docs_describe_the_tool_and_its_flag():
    tools_md = (PROJECT_ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    config_md = (PROJECT_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert "detect_cost_threshold_alerts" in tools_md and "threshold_detection.enabled" in tools_md
    assert "threshold_detection" in config_md and "sends **no** notification" in config_md
