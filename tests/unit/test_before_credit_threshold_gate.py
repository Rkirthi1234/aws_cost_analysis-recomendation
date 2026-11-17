"""Threshold gating with real saved billing evidence, without AWS calls."""
from decimal import Decimal
from unittest.mock import Mock

import pytest

from agent.services.cost_thresholds import BeforeCreditCostSource
from agent.services.report_views import recommendation_view
from test_cost_analysis_workflow import workflow


@pytest.mark.parametrize("limit,expected", [("0.01", "threshold_reached"), ("1", "below_threshold"),
                                             ("0.072418884", "threshold_reached")])
def test_saved_before_credit_cost_controls_evaluation(tmp_path, limit, expected):
    source = Mock()
    name = "Amazon Elastic Compute Cloud - Compute"
    source.service_costs.return_value = {
        "metric": "AmortizedCost", "warnings": [], "units": {name: ["USD"]},
        "cost_breakdown_available": True, "costs": {name: Decimal(0)},
        "costs_before_credits": {name: Decimal("0.072418884")},
        "credits": {name: Decimal("-0.072418884")}}
    wf = workflow(tmp_path, source=source)
    wf.engine.config.config["cost_thresholds"]["services"]["ec2"] = float(limit)
    analysis = wf.analyze("2026-11-01", "2026-11-13")
    result = wf.recommend(analysis["analysis_id"], service="ec2")
    assert result["threshold"]["status"] == expected
    assert result["selected_period_net_cost_usd"] == 0
    assert wf.engine.evaluate.call_count == (len(analysis["highest_cost_services"][0]["alternative_ids"]) if expected == "threshold_reached" else 0)
    assert source.service_costs.call_count == 1  # recommendation uses the saved evidence
    saved = wf.store.load_recommendation(result["recommendation_id"], wf._fingerprint(), wf.now_fn().timestamp())
    assert saved["threshold"] == result["threshold"]
    assert recommendation_view(saved)["threshold"] == result["threshold"]


@pytest.mark.parametrize("section,status", [({"enabled": False}, "thresholds_disabled"),
    ({"enabled": True, "services": {"s3": 0.01}}, "threshold_not_configured"),
    ({"enabled": True, "services": {"ec2": -1}}, "invalid_configuration")])
def test_unconfigured_threshold_never_collects_resources(tmp_path, section, status):
    wf = workflow(tmp_path)
    wf.engine.config.config["cost_thresholds"] = section
    analysis = wf.analyze("2026-11-01", "2026-11-13")
    report = wf.recommend(analysis["analysis_id"], "ec2")
    assert report["status"] == status
    wf.engine.evaluate.assert_not_called()
    assert recommendation_view(report)["recommendation"] is None


def test_legacy_analysis_does_not_substitute_net_cost(tmp_path):
    wf = workflow(tmp_path)
    analysis = wf.analyze("2026-11-01", "2026-11-13")
    snapshot = wf.store.load(analysis["analysis_id"], wf._fingerprint(), wf.now_fn().timestamp())
    snapshot.pop("cost_breakdown_available")
    snapshot["analysis_id"] = "analysis-legacy"
    wf.store.save(snapshot, wf._fingerprint(), wf.now_fn().timestamp())
    report = wf.recommend("analysis-legacy", "ec2")
    assert report["status"] == "needs_information"
    wf.engine.evaluate.assert_not_called()


@pytest.mark.parametrize("bad", [False, True])
def test_cli_adapter_requires_reconciled_credit_breakdown(bad):
    source = Mock()
    source.service_costs.return_value = {"cost_breakdown_available": True,
        "costs": {"EC2": Decimal(0)}, "credits": {"EC2": Decimal("-0.02")},
        "costs_before_credits": {"EC2": Decimal("0.01" if bad else "0.02")}}
    result = BeforeCreditCostSource(source).service_costs("2026-11-01", "2026-11-13")
    assert bool(result.get("error")) == bad
    if not bad:
        assert result["costs"]["EC2"] == Decimal("0.02")
    source.service_costs.assert_called_once_with("2026-11-01", "2026-11-13", include_credit_breakdown=True)
