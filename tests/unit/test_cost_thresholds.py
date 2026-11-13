"""Unit tests for agent/services/cost_thresholds.py and HighCostServiceIdentifier.service_costs.

No AWS calls: Cost Explorer is an in-memory fake. All amounts below are MOCKED test inputs, not
real AWS cost data.
"""
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError

from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.cost_thresholds import (
    EC2_OTHER_NOTE,
    CostThresholdDetector,
    CostThresholdSettings,
    load_cost_threshold_settings,
    month_to_date,
)
from agent.services.high_cost_identifier import HighCostServiceIdentifier

EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"
EC2_OTHER = "EC2 - Other"
S3 = "Amazon Simple Storage Service"

# Exactly the section the user added to agent/config.yaml.
USER_CONFIG = yaml.safe_load(
    """
cost_thresholds:
  enabled: true
  currency: USD
  services:
    ec2: 0.01
    s3: 0.01
"""
)


@pytest.fixture(scope="module")
def catalog():
    return AlternativesCatalog.load()


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


def usd(**amounts):
    """{ce service name: (amount, 'USD')} from keyword amounts keyed by short names."""
    names = {"compute": EC2_COMPUTE, "other": EC2_OTHER, "s3": S3}
    return {names[k]: (v, "USD") for k, v in amounts.items()}


def make(catalog, buckets=None, config=USER_CONFIG, error=None):
    settings = load_cost_threshold_settings(config, catalog)
    ce = FakeCE(buckets, error)
    identifier = HighCostServiceIdentifier(catalog=catalog, ce_client=ce)
    return CostThresholdDetector(settings, catalog, identifier), ce


def run(detector, start="2026-11-01", end="2026-11-12"):
    result = detector.detect(start, end)
    json.dumps(result)  # every result must be JSON-serializable
    return result


def entry(result, key):
    return next(s for s in result["services"] if s["service_key"] == key)


# ============================================================ configuration
def test_absent_section_is_disabled_and_valid(catalog):
    settings = load_cost_threshold_settings({"aws": {"region": "us-east-1"}}, catalog)
    assert settings.valid and settings.enabled is False and settings.source == "defaults"


def test_users_config_is_read_as_written(catalog):
    settings = load_cost_threshold_settings(USER_CONFIG, catalog)
    assert settings.valid and settings.enabled is True and settings.currency == "USD"
    assert {k: str(v) for k, v in settings.thresholds.items()} == {"ec2": "0.01", "s3": "0.01"}
    assert settings.state_file is None


def test_config_service_style_object_is_accepted(catalog):
    class FakeConfigService:
        config = USER_CONFIG

    assert load_cost_threshold_settings(FakeConfigService(), catalog).enabled is True


def test_config_example_ships_cost_thresholds_disabled_and_valid(catalog):
    root = Path(__file__).resolve().parents[2]
    example = yaml.safe_load((root / "agent" / "config.yaml.example").read_text(encoding="utf-8"))
    assert example["cost_thresholds"]["enabled"] is False
    settings = load_cost_threshold_settings(example, catalog)
    assert settings.valid and settings.enabled is False
    # the template must also be valid once someone switches it on
    example["cost_thresholds"]["enabled"] = True
    assert load_cost_threshold_settings(example, catalog).valid


def test_disabled_section_is_not_deeply_validated(catalog):
    settings = load_cost_threshold_settings(
        {"cost_thresholds": {"enabled": False, "currency": "EUR", "services": {"nope": -1}}}, catalog
    )
    assert settings.valid and settings.enabled is False


def test_missing_enabled_is_treated_as_disabled_with_warning(catalog):
    settings = load_cost_threshold_settings({"cost_thresholds": {"services": {"ec2": 0.01}}}, catalog)
    assert settings.valid and settings.enabled is False and any("not set" in w for w in settings.warnings)


def test_unknown_key_is_a_warning_not_an_error(catalog):
    cfg = {"cost_thresholds": {**USER_CONFIG["cost_thresholds"], "frequency": "daily"}}
    settings = load_cost_threshold_settings(cfg, catalog)
    assert settings.valid and any("frequency" in w for w in settings.warnings)


def test_currency_is_case_insensitive_usd(catalog):
    cfg = {"cost_thresholds": {**USER_CONFIG["cost_thresholds"], "currency": "usd"}}
    assert load_cost_threshold_settings(cfg, catalog).valid


def test_service_aliases_resolve_to_the_canonical_key(catalog):
    cfg = {"cost_thresholds": {"enabled": True, "currency": "USD", "services": {"Amazon Simple Storage Service": 1}}}
    settings = load_cost_threshold_settings(cfg, catalog)
    assert settings.valid and list(settings.thresholds) == ["s3"]


def _cfg(**overrides):
    section = {"enabled": True, "currency": "USD", "services": {"ec2": 0.01}}
    section.update(overrides)
    return {"cost_thresholds": section}


INVALID_CONFIGS = [
    ({"cost_thresholds": "on"}, "must be a mapping"),
    (_cfg(enabled="yes"), "must be true or false"),
    (_cfg(enabled=1), "must be true or false"),
    (_cfg(currency="EUR"), "only USD is supported"),
    (_cfg(currency=5), "only USD is supported"),
    (_cfg(currency=None), "only USD is supported"),
    (_cfg(services=None), "non-empty mapping"),
    (_cfg(services={}), "non-empty mapping"),
    (_cfg(services=["ec2"]), "non-empty mapping"),
    (_cfg(services={"ec2": -0.01}), "greater than 0"),
    (_cfg(services={"ec2": 0}), "greater than 0"),
    (_cfg(services={"ec2": 0.0}), "greater than 0"),
    (_cfg(services={"ec2": float("nan")}), "finite"),
    (_cfg(services={"ec2": float("inf")}), "finite"),
    (_cfg(services={"ec2": True}), "must be a number"),
    (_cfg(services={"ec2": "0.01"}), "must be a number"),
    (_cfg(services={"ec2": None}), "must be a number"),
    (_cfg(services={"ec2": [1]}), "must be a number"),
    (_cfg(services={"not-a-service": 0.01}), "not a known service"),
    (_cfg(services={5: 0.01}), "not a known service"),
    (_cfg(services={"ec2": 0.01, "EC2 - Other": 0.02}), "duplicates"),
    (_cfg(dedupe="file.json"), "must be a mapping"),
    (_cfg(dedupe={"state_file": ""}), "non-empty local file path"),
    (_cfg(dedupe={"state_file": 7}), "non-empty local file path"),
    (_cfg(dedupe={"bucket": "s3://x"}), "Unknown key"),
]


@pytest.mark.parametrize("config,expected", INVALID_CONFIGS)
def test_invalid_configuration_is_reported_and_never_raises(catalog, config, expected):
    settings = load_cost_threshold_settings(config, catalog)
    assert not settings.valid and settings.enabled is False
    assert any(expected in e for e in settings.errors), settings.errors


@pytest.mark.parametrize("config,_", INVALID_CONFIGS)
def test_invalid_configuration_makes_no_cost_explorer_call(catalog, config, _):
    detector, ce = make(catalog, [usd(compute="5")], config=config)
    result = run(detector)
    assert result["status"] == "invalid_configuration" and result["errors"]
    assert ce.calls == []


def test_one_bad_threshold_blocks_the_whole_evaluation(catalog):
    detector, ce = make(catalog, [usd(compute="5")], config=_cfg(services={"ec2": 0.01, "s3": -1}))
    assert run(detector)["status"] == "invalid_configuration" and ce.calls == []


# ============================================================ detection
def test_cost_below_threshold(catalog):
    detector, _ = make(catalog, [usd(compute="0.004", other="0.003", s3="0.0099")])
    result = run(detector)
    assert result["status"] == "evaluated" and result["crossed_service_keys"] == []
    assert entry(result, "ec2")["status"] == "below_threshold" and entry(result, "ec2")["crossed"] is False
    assert entry(result, "s3")["status"] == "below_threshold"
    assert "below" in entry(result, "s3")["reason"]


def test_cost_exactly_equal_to_threshold_crosses(catalog):
    detector, _ = make(catalog, [usd(s3="0.0100000000", compute="0.01")])
    result = run(detector)
    for key in ("s3", "ec2"):
        assert entry(result, key)["status"] == "threshold_crossed" and entry(result, key)["crossed"] is True
        assert "equals" in entry(result, key)["reason"]
    assert result["crossed_service_keys"] == ["ec2", "s3"]


def test_cost_above_threshold_crosses(catalog):
    detector, _ = make(catalog, [usd(s3="0.02")])
    s3 = entry(run(detector), "s3")
    assert s3["crossed"] is True and "greater than" in s3["reason"]
    assert s3["cost_usd"] == 0.02 and s3["threshold_usd"] == 0.01


def test_comparison_uses_the_exact_amount_not_a_rounded_one(catalog):
    detector, _ = make(catalog, [usd(s3="0.00999999")])
    s3 = entry(run(detector), "s3")
    assert s3["crossed"] is False and s3["cost_usd"] == 0.01      # displays as 0.01, still below
    assert s3["cost_usd_exact"] == "0.00999999"


def test_service_with_zero_cost_is_below_threshold(catalog):
    detector, _ = make(catalog, [usd(s3="0.0000000000", compute="0", other="0")])
    result = run(detector)
    s3 = entry(result, "s3")
    assert s3["status"] == "below_threshold" and s3["cost_data"] == "zero" and s3["crossed"] is False
    assert result["summary"]["crossed"] == 0


def test_service_absent_from_cost_explorer_is_not_a_failure(catalog):
    detector, _ = make(catalog, [usd(compute="0.5")])         # S3 not reported at all
    result = run(detector)
    s3 = entry(result, "s3")
    assert s3["status"] == "no_cost_data" and s3["cost_data"] == "absent" and s3["crossed"] is False
    assert s3["cost_usd"] == 0.0 and "no cost" in s3["reason"]
    assert result["summary"]["no_cost_data"] == 1 and result["crossed_service_keys"] == ["ec2"]


def test_empty_cost_explorer_result_marks_every_service_as_no_data(catalog):
    detector, _ = make(catalog, [])
    result = run(detector)
    assert {s["status"] for s in result["services"]} == {"no_cost_data"}


def test_ec2_sums_both_cost_explorer_names_and_shows_them_separately(catalog):
    detector, _ = make(catalog, [usd(compute="0.006", other="0.005")])
    ec2 = entry(run(detector), "ec2")
    assert ec2["crossed"] is True and ec2["cost_usd"] == 0.011
    names = {b["ce_service"]: b for b in ec2["cost_explorer_services"]}
    assert set(names) == {EC2_COMPUTE, EC2_OTHER}
    assert names[EC2_COMPUTE]["cost_usd"] == 0.006 and names[EC2_OTHER]["cost_usd"] == 0.005
    assert all(b["present_in_cost_explorer"] for b in names.values())
    # neither component alone reaches the 0.01 threshold: the crossing is due to the sum
    assert names[EC2_COMPUTE]["cost_usd"] < 0.01 and names[EC2_OTHER]["cost_usd"] < 0.01


def test_ec2_other_is_explained_and_not_attributed_to_instances(catalog):
    detector, _ = make(catalog, [usd(other="0.02")])
    ec2 = entry(run(detector), "ec2")
    assert ec2["crossed"] is True
    assert ec2["notes"] == [EC2_OTHER_NOTE]
    for word in ("EBS", "NAT", "data-transfer", "not be attributed entirely to EC2 instances"):
        assert word in EC2_OTHER_NOTE
    assert entry(run(make(catalog, [usd(s3="0.02")])[0]), "s3")["notes"] == []   # only where EC2 - Other applies


def test_both_ec2_names_are_listed_even_when_one_is_absent(catalog):
    detector, _ = make(catalog, [usd(compute="0.5")])
    names = {b["ce_service"]: b for b in entry(run(detector), "ec2")["cost_explorer_services"]}
    assert names[EC2_OTHER]["present_in_cost_explorer"] is False and names[EC2_OTHER]["cost_usd"] == 0.0
    assert names[EC2_COMPUTE]["present_in_cost_explorer"] is True


def test_non_usd_unit_is_not_compared(catalog):
    detector, _ = make(catalog, [{S3: ("500", "EUR"), EC2_COMPUTE: ("0.02", "USD")}])
    result = run(detector)
    s3 = entry(result, "s3")
    assert s3["status"] == "unsupported_currency" and s3["crossed"] is False and s3["cost_usd"] is None
    assert "EUR" in s3["reason"] and "no conversion" in s3["reason"]
    assert entry(result, "ec2")["crossed"] is True and result["summary"]["unsupported_currency"] == 1


def test_monthly_buckets_are_summed_and_a_multi_month_warning_is_given(catalog):
    detector, _ = make(catalog, [usd(s3="0.006"), usd(s3="0.005")])
    result = run(detector, "2026-10-01", "2026-12-01")
    assert entry(result, "s3")["cost_usd"] == 0.011 and entry(result, "s3")["crossed"] is True
    assert any("several calendar months" in w for w in result["warnings"])
    assert not any("several calendar months" in w for w in run(make(catalog, [usd(s3="1")])[0])["warnings"])


def test_result_describes_period_metric_and_rule(catalog):
    detector, _ = make(catalog, [usd(s3="0.02")])
    result = run(detector, "2026-11-01", "2026-12-01")
    assert result["period"] == {
        "start": "2026-11-01", "end_exclusive": "2026-12-01", "kind": "custom",
        "granularity": "MONTHLY", "semantics": result["period"]["semantics"],
    }
    assert "start inclusive, end exclusive" in result["period"]["semantics"]
    assert result["metric"] == "AmortizedCost" and result["currency"] == "USD"
    assert ">=" in result["comparison_rule"]
    assert not {"recommendation", "estimated_savings", "savings_usd"} & set(result)


# ============================================================ disabled / period / errors
def test_disabled_detection_makes_no_call(catalog):
    cfg = _cfg(enabled=False)
    detector, ce = make(catalog, [usd(compute="5")], config=cfg)
    result = run(detector)
    assert result["status"] == "disabled" and result["enabled"] is False and ce.calls == []


def test_absent_section_detection_is_disabled(catalog):
    detector, ce = make(catalog, [usd(compute="5")], config={})
    assert run(detector)["status"] == "disabled" and ce.calls == []


def test_month_to_date_helper():
    assert month_to_date(date(2026, 11, 12)) == ("2026-11-01", "2026-11-12")
    assert month_to_date(date(2026, 11, 2)) == ("2026-11-01", "2026-11-02")
    assert month_to_date(date(2026, 11, 1)) is None


def test_default_period_is_month_to_date(catalog):
    detector, ce = make(catalog, [usd(s3="0.02")])
    result = detector.detect(today=date(2026, 11, 12))
    assert result["period"]["kind"] == "month_to_date"
    assert result["period"]["start"] == "2026-11-01" and result["period"]["end_exclusive"] == "2026-11-12"
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-11-01", "End": "2026-11-12"}


def test_month_to_date_on_the_first_requires_explicit_dates(catalog):
    detector, ce = make(catalog, [usd(s3="0.02")])
    result = detector.detect(today=date(2026, 11, 1))
    assert result["error"] is True and result["code"] == "PeriodEmpty" and ce.calls == []


def test_only_one_date_is_rejected(catalog):
    detector, ce = make(catalog, [usd(s3="0.02")])
    for kwargs in ({"start_date": "2026-11-01"}, {"end_date": "2026-11-12"}):
        out = detector.detect(**kwargs)
        assert out["error"] is True and out["code"] == "InvalidDate"
    assert ce.calls == []


@pytest.mark.parametrize("start,end,code", [
    ("2026-11", "2026-11-12", "InvalidDate"),
    ("2026-02-30", "2026-03-01", "InvalidDate"),
    ("2026-11-12", "2026-11-01", "InvalidDateRange"),
    ("2026-11-01", "2026-11-01", "InvalidDateRange"),
])
def test_invalid_dates_are_reported_before_any_call(catalog, start, end, code):
    detector, ce = make(catalog, [usd(s3="0.02")])
    out = detector.detect(start, end)
    assert out["error"] is True and out["code"] == code and ce.calls == []


def test_cost_explorer_access_denied_is_returned_not_raised(catalog):
    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetCostAndUsage")
    detector, _ = make(catalog, error=err)
    out = run(detector)
    assert out["error"] is True and out["code"] == "AccessDeniedException" and "ce:GetCostAndUsage" in out["hint"]


def test_unexpected_cost_explorer_failure_is_returned_not_raised(catalog):
    detector, _ = make(catalog, error=RuntimeError("boom"))
    out = run(detector)
    assert out["error"] is True and out["exception_type"] == "RuntimeError"


def test_only_the_read_only_cost_explorer_query_is_used(catalog):
    detector, ce = make(catalog, [usd(s3="0.02")])
    run(detector)
    assert len(ce.calls) == 1
    call = ce.calls[0]
    assert call["Granularity"] == "MONTHLY" and call["Metrics"] == ["AmortizedCost"]
    assert call["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]
    assert "Filter" not in call


def test_detector_without_a_cost_source_reports_an_error(catalog):
    settings = load_cost_threshold_settings(USER_CONFIG, catalog)
    out = CostThresholdDetector(settings, catalog, None).detect("2026-11-01", "2026-11-12")
    assert out["error"] is True and out["code"] == "NoCostSource"


def test_settings_as_dict_is_json_serializable(catalog):
    json.dumps(load_cost_threshold_settings(USER_CONFIG, catalog).as_dict())
    assert isinstance(CostThresholdSettings().as_dict()["thresholds"], dict)


# ============================================================ identifier.service_costs
def test_service_costs_sums_buckets_and_reports_units(catalog):
    ce = FakeCE([{S3: ("1.5", "USD")}, {S3: ("2.5", "USD"), EC2_OTHER: ("1", "EUR")}])
    out = HighCostServiceIdentifier(catalog=catalog, ce_client=ce).service_costs("2026-10-01", "2026-12-01")
    assert str(out["costs"][S3]) == "4.0" and str(out["costs"][EC2_OTHER]) == "1"
    assert out["units"] == {S3: ["USD"], EC2_OTHER: ["EUR"]} and out["metric"] == "AmortizedCost"


def test_service_costs_validates_dates_and_leaves_identify_unchanged(catalog):
    identifier = HighCostServiceIdentifier(catalog=catalog, ce_client=FakeCE([]))
    assert identifier.service_costs("bad", "2026-11-01")["code"] == "InvalidDate"
    assert identifier.identify("2026-10-01", "2026-11-01")["period"]["start"] == "2026-10-01"
