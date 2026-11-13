"""Date-ranked cost -> saved analysis -> evaluated alternatives, entirely offline."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent.api import app, get_cost_workflow
from agent.services.account_registry import AccountRegistry
from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.cost_analysis_workflow import AnalysisStore, CostAnalysisWorkflow, WorkflowError

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)
EC2L = "ec2-short-event-driven-to-lambda"
EC2F = "ec2-containerized-to-fargate"
NAT = "nat-gateway-to-gateway-endpoints"
CATALOG = AlternativesCatalog.load()


class Source:
    def __init__(self): self.calls = []
    def service_costs(self, start, end, *, include_credit_breakdown=False):
        self.calls.append((start, end))
        amounts = {"Amazon Elastic Compute Cloud - Compute": Decimal(100), "EC2 - Other": Decimal(20),
                   "Amazon Relational Database Service": Decimal(90), "Amazon Simple Storage Service": Decimal(15)}
        return {"metric": "AmortizedCost", "costs": amounts, "units": {k: ["USD"] for k in amounts}, "warnings": [],
                "cost_breakdown_available": True, "costs_before_credits": amounts, "credits": {k: Decimal(0) for k in amounts}}


def engine():
    obj = Mock()
    obj.catalog = CATALOG
    obj.registry = AccountRegistry([{"account_id": "111111111111", "account_type": "payer", "use_current_credentials": True}], "us-east-1")
    obj.config.get.return_value = True
    obj.evaluate.return_value = {"reports": [], "errors": [], "warnings": [], "status": "evaluated"}
    return obj


def workflow(tmp_path, source=None, obj=None, now=NOW):
    return CostAnalysisWorkflow(obj or engine(), AnalysisStore(tmp_path/"analyses.sqlite3"),
                                cost_source=source or Source(), now_fn=lambda: now)


def test_rank_totals_group_ec2_costs_and_provide_analysis_handoff(tmp_path):
    source = Source()
    result = workflow(tmp_path, source).analyze("2026-11-01", "2026-11-13", 2)
    assert result["total_cost_usd"] == 225
    assert result["service_count"] == 3
    assert [s["service_key"] for s in result["highest_cost_services"]] == ["ec2", "rds"]
    assert result["highest_cost_services"][0]["cost_usd"] == 120
    assert len(result["highest_cost_services"][0]["cost_explorer_services"]) == 2
    assert result["analysis_id"].startswith("analysis-")
    assert source.calls == [("2026-11-01", "2026-11-13")]
    assert "services" not in result
    assert result["services_shown"] == 2


def test_omitted_dates_default_to_month_to_date(tmp_path):
    assert workflow(tmp_path).analyze()["period"] == {"start_date": "2026-11-01", "end_date_exclusive": "2026-11-13"}


@pytest.mark.parametrize("args", [("2026-11-01", None, 5), ("2026-11-13", "2026-11-01", 5),
    ("2026-11-01", "2026-12-01", 5), ("2026-11-01", "2026-11-13", 0), ("2026-11-01", "2026-11-13", True)])
def test_bad_dates_future_actual_costs_and_top_n_are_rejected(tmp_path, args):
    source = Source()
    with pytest.raises(ValueError): workflow(tmp_path, source).analyze(*args)
    assert source.calls == []


def test_saved_analysis_survives_new_workflow_instance_without_requerying_billing(tmp_path):
    first = workflow(tmp_path)
    analysis = first.analyze()
    second_source = Source()
    second = workflow(tmp_path, second_source)
    result = second.recommend(analysis["analysis_id"], "Amazon EC2", regions=["us-east-1"])
    assert result["selected_period_cost_usd"] == 120
    assert {c["alternative_id"] for c in result["candidates"]} == {EC2L, EC2F, NAT}
    assert second_source.calls == []
    assert second.engine.evaluate.call_count == 3


def test_missing_or_expired_analysis_is_clear_and_never_requeries_aws(tmp_path):
    first = workflow(tmp_path)
    with pytest.raises(WorkflowError) as exc: first.recommend("not-an-analysis")
    assert exc.value.status_code == 404
    saved = first.analyze()
    later = workflow(tmp_path, now=NOW+timedelta(days=1))
    with pytest.raises(WorkflowError) as exc: later.recommend(saved["analysis_id"])
    assert exc.value.status_code == 410
    later.engine.evaluate.assert_not_called()


def test_changed_account_scope_invalidates_saved_analysis(tmp_path):
    first = workflow(tmp_path)
    saved = first.analyze()
    obj = engine()
    obj.registry = AccountRegistry([{"account_id": "222222222222", "account_type": "payer", "use_current_credentials": True}], "us-east-1")
    with pytest.raises(WorkflowError) as exc: workflow(tmp_path, obj=obj).recommend(saved["analysis_id"])
    assert exc.value.status_code == 409


def test_highest_service_is_default_and_service_must_belong_to_analysis(tmp_path):
    obj = workflow(tmp_path)
    saved = obj.analyze()
    assert obj.recommend(saved["analysis_id"])["service_key"] == "ec2"
    with pytest.raises(ValueError, match="not found"): obj.recommend(saved["analysis_id"], "Made up service")


def test_unmapped_service_remains_visible_without_inventing_candidate(tmp_path):
    obj = workflow(tmp_path)
    saved = obj.analyze(top_n=1)  # S3 is outside the returned top one, but retained internally.
    result = obj.recommend(saved["analysis_id"], "s3")
    assert result["status"] == "no_supported_alternative"
    assert result["candidates"] == []
    obj.engine.evaluate.assert_not_called()


@pytest.mark.parametrize("bad", [{"error": True, "code": "AccessDenied"},
    {"costs": {"EC2 - Other": Decimal(1)}, "units": {"EC2 - Other": ["EUR"]}},
    {"warnings": ["Cost Explorer pagination stopped after 50 pages"]},
    {"costs": {"EC2 - Other": Decimal("NaN")}, "units": {"EC2 - Other": ["USD"]}}])
def test_failed_partial_mixed_currency_and_invalid_costs_are_not_saved(tmp_path, bad):
    source = Mock()
    source.service_costs.return_value = bad
    with pytest.raises(WorkflowError): workflow(tmp_path, source).analyze()
    assert not (tmp_path/"analyses.sqlite3").exists()


def test_credits_are_preserved_and_ties_are_deterministic(tmp_path):
    source = Mock()
    source.service_costs.return_value = {"costs": {"Service B": Decimal(10), "Service A": Decimal(10), "Credit": Decimal(-2)},
                                      "units": {k: ["USD"] for k in ("Service A", "Service B", "Credit")},
                                      "cost_breakdown_available": True,
                                      "costs_before_credits": {"Service A": Decimal(10), "Service B": Decimal(10), "Credit": Decimal(0)},
                                      "credits": {"Service A": Decimal(0), "Service B": Decimal(0), "Credit": Decimal(-2)}}
    out = workflow(tmp_path, source).analyze()
    assert out["total_cost_usd"] == 18
    assert [r["service"] for r in out["highest_cost_services"]] == ["Service A", "Service B", "Credit"]


def test_no_cost_data_does_not_generate_candidates(tmp_path):
    source = Mock()
    source.service_costs.return_value = {"costs": {}, "units": {}}
    obj = workflow(tmp_path, source)
    saved = obj.analyze()
    assert saved["status"] == "no_cost_data"
    assert obj.recommend(saved["analysis_id"])["status"] == "no_cost_data"
    obj.engine.evaluate.assert_not_called()


def report(eid, price="45", status="recommended", rid="i-test"):
    return {"status": status, "decision": {"alternative_id": eid, "account_id": "111111111111", "region": "us-east-1",
            "resource_id": rid, "subject": None, "reasons": [] if status == "recommended" else [{"message": "Peak memory needed"}]},
            "explanation": "Verified workload fit and lower estimate",
            "cost_comparison": {"status": "estimated", "current_monthly_cost_usd": "120", "alternative_monthly_cost_usd": price,
                                "monthly_savings_usd": str(120-Decimal(price)), "savings_pct": "62.50", "exact": {"alternative": price}}}


def test_verified_candidates_select_best_for_same_resource_and_never_use_service_total_as_baseline(tmp_path):
    obj = workflow(tmp_path)
    def evaluate(eid, *args):
        rows = [report(eid, "45" if eid == EC2L else "60")] if eid != NAT else []
        return {"reports": rows, "errors": []}
    obj.engine.evaluate.side_effect = evaluate
    result = obj.recommend(obj.analyze()["analysis_id"], "ec2")
    assert result["status"] == "recommended"
    assert len(result["preferred_recommendations"]) == 1
    assert result["recommendation"]["alternative_id"] == EC2L
    assert result["recommendation"]["monthly_savings_usd"] == "75"
    assert all(call.args[3] is None for call in obj.engine.evaluate.call_args_list)  # never inject service totals as workload costs


def test_missing_evidence_is_summarized_while_full_details_remain(tmp_path):
    obj = workflow(tmp_path)
    obj.engine.evaluate.return_value = {"reports": [report(EC2L, status="needs_information")], "errors": []}
    result = obj.recommend(obj.analyze()["analysis_id"], "ec2")
    assert result["status"] == "needs_information"
    assert result["recommendation"] is None
    assert result["candidates"][0]["missing_information"] == ["Peak memory needed"]
    assert result["candidates"][0]["details"]["reports"]


def test_store_is_bounded(tmp_path):
    store = AnalysisStore(tmp_path/"analyses.sqlite3", max_entries=1)
    store.save({"analysis_id": "one"}, "scope", NOW.timestamp())
    store.save({"analysis_id": "two"}, "scope", NOW.timestamp())
    assert store.load("two", "scope", NOW.timestamp())["analysis_id"] == "two"
    with pytest.raises(WorkflowError): store.load("one", "scope", NOW.timestamp())


def test_two_http_calls_use_real_workflow_with_fake_aws_and_persistent_snapshot(tmp_path):
    obj = workflow(tmp_path)
    app.dependency_overrides[get_cost_workflow] = lambda: obj
    try:
        with TestClient(app) as client:
            first = client.post("/cost-analysis", json={"start_date": "2026-11-01", "end_date": "2026-11-13", "top_n": 3})
            assert first.status_code == 200, first.text
            second = client.post("/recommendations", json={"analysis_id": first.json()["analysis_id"], "service": "Amazon EC2", "regions": ["us-east-1"]})
            assert second.status_code == 200, second.text
            assert second.json()["recommendation_id"].startswith("recommendation-")
            assert "candidates" not in second.json()
            assert client.post("/recommendations", json={"analysis_id": "unknown"}).status_code == 404
            assert client.post("/recommendations", json={}).status_code == 422
            assert client.post("/cost-analysis", json={"top_n": 0}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_top_five_response_does_not_leak_all_twelve_services(tmp_path):
    source = Mock()
    costs = {f"Service {i:02}": Decimal(i) for i in range(12)}
    source.service_costs.return_value = {"costs": costs, "units": {k: ["USD"] for k in costs},
                                      "cost_breakdown_available": True, "costs_before_credits": costs, "credits": {k: Decimal(0) for k in costs}}
    obj = workflow(tmp_path, source)
    app.dependency_overrides[get_cost_workflow] = lambda: obj
    try:
        with TestClient(app) as client:
            response = client.post("/cost-analysis", json={"top_n": 5})
        assert response.status_code == 200
        body = response.json()
        assert body["service_count"] == 12
        assert body["services_shown"] == 5
        assert len(body["top_services"]) == 5
        assert "services" not in body
        assert body["total_net_cost_usd"] == 66
        assert [r["cost_before_credits_usd"] for r in body["top_services"]] == [11, 10, 9, 8, 7]
    finally:
        app.dependency_overrides.clear()
