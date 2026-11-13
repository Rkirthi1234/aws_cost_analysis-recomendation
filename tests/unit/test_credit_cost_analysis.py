"""Actual SERVICE/RECORD_TYPE adapter through saved reports; no live AWS calls."""
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent.api import app, get_cost_workflow, get_saved_reports
from agent.services.cost_analysis_workflow import CostAnalysisWorkflow, AnalysisStore, SavedReportReader, WorkflowError
from agent.services.high_cost_identifier import HighCostServiceIdentifier
from agent.services.report_views import cost_view
from test_cost_analysis_workflow import engine, NOW

COMPUTE = "Amazon Elastic Compute Cloud - Compute"
OTHER = "EC2 - Other"
S3 = "Amazon Simple Storage Service"


def row(service, kind, amount, unit="USD"):
    return {"Keys": [service, kind], "Metrics": {"AmortizedCost": {"Amount": str(amount), "Unit": unit}}}


class CE:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [row(COMPUTE, "Usage", ".0137"), row(COMPUTE, "Credit", "-.0137"),
            row(OTHER, "Usage", ".0107"), row(OTHER, "Credit", "-.0107"), row(S3, "Usage", ".001")]
        self.calls = []
    def get_cost_and_usage(self, **params):
        self.calls.append(params)
        return {"ResultsByTime": [{"Groups": self.rows}]}


def make(tmp_path, ce=None):
    obj = engine()
    client = ce or CE()
    source = HighCostServiceIdentifier(catalog=obj.catalog, ce_client=client)
    return CostAnalysisWorkflow(obj, AnalysisStore(tmp_path/"costs.sqlite3"), cost_source=source, now_fn=lambda: NOW), client


def test_ec2_ranks_first_even_when_credits_cancel_all_its_cost(tmp_path):
    flow, ce = make(tmp_path)
    result = flow.analyze()
    assert ce.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}]
    assert ce.calls[0]["Metrics"] == ["AmortizedCost"]
    assert len(ce.calls) == 1
    first = result["highest_cost_services"][0]
    assert first["service_key"] == "ec2"
    assert first["cost_before_credits_usd"] == .0244
    assert first["credits_applied_usd"] == -.0244
    assert first["net_cost_usd"] == 0
    assert result["total_cost_before_credits_usd"] == .0254
    assert result["total_credits_applied_usd"] == -.0244
    assert result["total_net_cost_usd"] == .001
    assert flow.recommend(result["analysis_id"])["service_key"] == "ec2"


def test_net_before_and_credit_amounts_reconcile_using_decimal(tmp_path):
    flow, _ = make(tmp_path)
    result = flow.analyze()
    assert Decimal(result["total_cost_before_credits_usd_exact"]) + Decimal(result["total_credits_applied_usd_exact"]) == Decimal(result["total_cost_usd_exact"])
    for item in result["highest_cost_services"]:
        assert Decimal(item["cost_before_credits_usd_exact"]) + Decimal(item["credits_applied_usd_exact"]) == Decimal(item["cost_usd_exact"])


@pytest.mark.parametrize("rows,before,credit,net", [
    ([row(COMPUTE, "Usage", "1"), row(COMPUTE, "Credit", "-.4")], "1", "-.4", ".6"),
    ([row(COMPUTE, "Credit", "-.4")], "0", "-.4", "-.4"),
    ([row(COMPUTE, "Usage", "1"), row(COMPUTE, "Refund", "-.1"), row(COMPUTE, "Discount", "-.2"), row(COMPUTE, "Credit", "-.4")], ".7", "-.4", ".3"),
    ([row(COMPUTE, "Usage", "1"), row(COMPUTE, "Credit", ".1")], "1", ".1", "1.1"),
])
def test_refunds_discounts_credit_only_and_reversals_keep_their_actual_signs(tmp_path, rows, before, credit, net):
    flow, _ = make(tmp_path, CE(rows))
    item = flow.analyze()["highest_cost_services"][0]
    assert Decimal(item["cost_before_credits_usd_exact"]) == Decimal(before)
    assert Decimal(item["credits_applied_usd_exact"]) == Decimal(credit)
    assert Decimal(item["cost_usd_exact"]) == Decimal(net)


def test_legacy_service_cost_query_remains_net_cost_and_single_dimension():
    ce = CE([])
    source = HighCostServiceIdentifier(catalog=engine().catalog, ce_client=ce)
    source.service_costs("2026-11-01", "2026-11-13")
    assert ce.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]


@pytest.mark.parametrize("bad", [row(COMPUTE, "Usage", "NaN"), row(COMPUTE, "Usage", "invalid"),
    {"Keys": [COMPUTE, "Usage"], "Metrics": {"AmortizedCost": {"Unit": "USD"}}},
    {"Keys": [COMPUTE, "Usage"], "Metrics": {"AmortizedCost": {"Amount": "1"}}},
    row(COMPUTE, "", "1"), {"Keys": [COMPUTE], "Metrics": {"AmortizedCost": {"Amount": "1", "Unit": "USD"}}}])
def test_incomplete_credit_queries_are_not_saved_as_complete_reports(tmp_path, bad):
    flow, _ = make(tmp_path, CE([bad]))
    with pytest.raises(WorkflowError): flow.analyze()
    assert not (tmp_path/"costs.sqlite3").exists()


def test_post_get_and_recommendation_handoff_keep_same_credit_breakdown(tmp_path):
    flow, ce = make(tmp_path)
    app.dependency_overrides[get_cost_workflow] = lambda: flow
    app.dependency_overrides[get_saved_reports] = lambda: SavedReportReader(flow.store, flow.registry, now_fn=lambda: NOW)
    try:
        with TestClient(app) as client:
            response = client.post("/cost-analysis", json={"top_n": 1})
            assert response.status_code == 200, response.text
            body = response.json()
            assert len(body["top_services"]) == 1
            assert body["top_services"][0] == {"service": "Amazon EC2", "cost_before_credits_usd": .0244, "credits_applied_usd": -.0244, "net_cost_usd": 0}
            assert client.get("/cost-analysis/"+body["analysis_id"]).json() == body
            rec = client.post("/recommendations", json={"analysis_id": body["analysis_id"]}).json()
            assert rec["service"] == "Amazon EC2"
            assert rec["selected_period_cost_before_credits_usd"] == .0244
            assert rec["selected_period_credits_applied_usd"] == -.0244
            assert rec["selected_period_net_cost_usd"] == 0
            assert client.get("/recommendations/"+rec["recommendation_id"]).json() == rec
            assert len(ce.calls) == 1  # GETs and recommendation do not rerun billing.
    finally:
        app.dependency_overrides.clear()


def test_old_snapshot_does_not_invent_zero_credits_or_change_its_ranking(tmp_path):
    flow, _ = make(tmp_path)
    old = flow.analyze()
    for key in [k for k in old if "credits" in k or k in ("ranking_basis", "cost_breakdown_available", "total_net_cost_usd")]:
        del old[key]
    for item in old["highest_cost_services"]:
        for key in [k for k in item if "credits" in k or k == "net_cost_usd"]: del item[key]
    view = cost_view(old)
    assert view["ranking_basis"] == "net_cost_legacy"
    assert view["total_credits_applied_usd"] is None
    assert view["top_services"][0]["cost_before_credits_usd"] is None
    assert "older snapshot" in view["limitations"][-1]


def test_missing_or_inconsistent_provider_credit_breakdown_is_rejected(tmp_path):
    flow, _ = make(tmp_path)
    source = Mock()
    source.service_costs.return_value = {"costs": {COMPUTE: Decimal(0)}, "units": {COMPUTE: ["USD"]}}
    flow.cost_source = source
    with pytest.raises(WorkflowError, match="verified"): flow.analyze()
    source.service_costs.return_value.update(cost_breakdown_available=True, costs_before_credits={COMPUTE: Decimal(1)}, credits={COMPUTE: Decimal(0)})
    with pytest.raises(WorkflowError, match="does not match"): flow.analyze()
