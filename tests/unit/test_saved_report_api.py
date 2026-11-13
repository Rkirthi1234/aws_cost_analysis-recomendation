"""Saved GET reports do not contact AWS; default views stay concise and truthful."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent import api
from agent.services.cost_analysis_workflow import AnalysisStore, SavedReportReader, WorkflowError, account_fingerprint
from agent.services.report_views import cost_view, recommendation_view
from test_cost_analysis_workflow import workflow, NOW, report, EC2L, EC2F, NAT


def ready(tmp_path, monkeypatch):
    obj = workflow(tmp_path)
    def evaluate(eid, *args):
        if eid == NAT:
            return {"reports": [], "errors": [], "status": "evaluated"}
        rr = report(eid, status="needs_information")
        rr["decision"]["reasons"] = [
            {"evidence_key": "ec2.peak_memory_mib", "message": "Peak memory missing"},
            {"evidence_key": "ec2.memory_utilization_max_pct", "message": "Memory utilization missing"}]
        return {"reports": [rr], "errors": [], "warnings": []}
    obj.engine.evaluate.side_effect = evaluate
    api.app.dependency_overrides[api.get_cost_workflow] = lambda: obj
    # Exercise the real GET dependency: config/registry only, with no SessionManager.
    monkeypatch.setenv("COST_ANALYZER_ANALYSIS_DB", str(tmp_path/"analyses.sqlite3"))
    monkeypatch.setattr(api, "ConfigService", Mock(return_value=SimpleNamespace(account_registry=obj.registry)))
    monkeypatch.setattr(api, "SessionManager", Mock(side_effect=AssertionError("GET attempted an AWS session")))
    monkeypatch.setattr(api, "SavedReportReader", lambda store, registry: SavedReportReader(store, registry, now_fn=lambda: NOW))
    return obj


def test_post_get_round_trip_survives_new_reader_and_never_reexecutes_aws(tmp_path, monkeypatch):
    obj = ready(tmp_path, monkeypatch)
    try:
        with TestClient(api.app) as client:
            created_cost = client.post("/cost-analysis", json={"top_n": 2}).json()
            assert len(created_cost["top_services"]) == 2
            assert "details" not in created_cost
            cost_id = created_cost["analysis_id"]
            created_rec = client.post("/recommendations", json={"analysis_id": cost_id, "service": "ec2"}).json()
            rec_id = created_rec["recommendation_id"]
            assert created_rec["status"] == "needs_information"
            assert created_rec["required_information"] == ["Measured peak memory usage over a representative period."]
            assert "VPC gateway endpoints (S3 and DynamoDB)" not in created_rec["alternatives_considered"]
            assert "candidates" not in created_rec and "details" not in created_rec
            calls = obj.engine.evaluate.call_count
            obj.engine.evaluate.side_effect = AssertionError("GET reran evaluation")
            api.app.dependency_overrides[api.get_cost_workflow] = lambda: (_ for _ in ()).throw(AssertionError("GET built AWS workflow"))
            assert client.get(f"/cost-analysis/{cost_id}").json() == created_cost
            assert client.get(f"/recommendations/{rec_id}").json() == created_rec
            detailed = client.get(f"/recommendations/{rec_id}?include_details=true").json()
            assert len(detailed["details"]["candidates"]) == 3
            assert client.get(f"/cost-analysis/{cost_id}?include_details=true").json()["details"]["metric"] == "AmortizedCost"
            assert obj.engine.evaluate.call_count == calls
            api.SessionManager.assert_not_called()
    finally:
        api.app.dependency_overrides.clear()


def test_missing_expired_and_scope_changed_recommendations_return_clear_errors(tmp_path):
    obj = workflow(tmp_path)
    saved = obj.recommend(obj.analyze()["analysis_id"], "ec2")
    with pytest.raises(WorkflowError) as missing:
        obj.store.load_recommendation("missing", obj._fingerprint(), NOW.timestamp())
    assert missing.value.status_code == 404
    with pytest.raises(WorkflowError) as expired:
        obj.store.load_recommendation(saved["recommendation_id"], obj._fingerprint(), (NOW+timedelta(days=1)).timestamp())
    assert expired.value.status_code == 410
    with pytest.raises(WorkflowError) as changed:
        obj.store.load_recommendation(saved["recommendation_id"], "another-scope", NOW.timestamp())
    assert changed.value.status_code == 409


def test_repeated_recommendation_runs_get_distinct_saved_ids(tmp_path):
    obj = workflow(tmp_path)
    aid = obj.analyze()["analysis_id"]
    first, second = obj.recommend(aid, "ec2"), obj.recommend(aid, "ec2")
    assert first["recommendation_id"] != second["recommendation_id"]
    assert obj.store.load_recommendation(first["recommendation_id"], obj._fingerprint(), NOW.timestamp()) == first


def test_recommendation_snapshot_redacts_sensitive_source_text(tmp_path):
    obj = workflow(tmp_path)
    obj.engine.evaluate.return_value = {"reports": [], "errors": [], "warnings": ["AKIA1234567890ABCDEF"], "status": "partial"}
    out = obj.recommend(obj.analyze()["analysis_id"], "ec2")
    stored = obj.store.load_recommendation(out["recommendation_id"], obj._fingerprint(), NOW.timestamp())
    assert "AKIA1234567890ABCDEF" not in str(stored)


def test_priced_view_distinguishes_monthly_estimates_and_preserves_material_assumptions(tmp_path):
    obj = workflow(tmp_path)
    def evaluate(eid, *args):
        if eid != EC2L: return {"reports": [], "errors": []}
        rr = report(eid)
        rr["cost_comparison"]["assumptions"] = ["Public on-demand prices; tax and private discounts excluded.", "730 hours per month."]
        return {"reports": [rr], "errors": []}
    obj.engine.evaluate.side_effect = evaluate
    result = recommendation_view(obj.recommend(obj.analyze()["analysis_id"], "ec2"))
    assert result["status"] == "recommended"
    assert result["recommendation"]["estimated_monthly_savings_usd"] == "75"
    assert result["recommendation"]["resource_id"] == "i-test"
    assert "Public on-demand" in result["assumptions"][0]
    assert "different scopes" in result["cost_basis"]


def test_collection_errors_remain_visible_in_concise_view(tmp_path):
    obj = workflow(tmp_path)
    obj.engine.evaluate.return_value = {"reports": [], "errors": [{"code": "AccessDenied", "message": "CloudWatch access denied"}], "warnings": ["Inventory was truncated"]}
    result = recommendation_view(obj.recommend(obj.analyze()["analysis_id"], "ec2"))
    assert result["status"] == "needs_information"
    assert "CloudWatch access denied" in result["limitations"]
    assert "Inventory was truncated" in result["limitations"]


def test_no_cost_advantage_preserves_actual_compared_numbers(tmp_path):
    obj = workflow(tmp_path)
    obj.engine.evaluate.return_value = {"reports": [report(EC2L, "150", status="no_cost_advantage")], "errors": []}
    result = recommendation_view(obj.recommend(obj.analyze()["analysis_id"], "ec2"))
    assert result["recommendation"] is None
    assert result["alternative_outcomes"][0]["estimated_alternative_monthly_cost_usd"] == "150"


def test_invalid_database_lookup_ids_are_safe_and_missing_http_ids_are_404(tmp_path, monkeypatch):
    ready(tmp_path, monkeypatch)
    try:
        with TestClient(api.app) as client:
            assert client.get("/cost-analysis/missing").status_code == 404
            assert client.get("/recommendations/missing").status_code == 404
    finally:
        api.app.dependency_overrides.clear()
