"""Simulation is explicit, saved, deterministic, and cannot touch AWS."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent import api
from agent.services.recommendation_simulation import simulate, simulation_profile
from agent.services.report_views import recommendation_view
from test_cost_analysis_workflow import workflow

EXAMPLE = json.loads((Path(__file__).parents[2]/"examples/simulated-recommendation-request.json").read_text())


def profile():
    return copy.deepcopy(EXAMPLE["workload_inputs"][0])


def test_example_calculates_hypothetical_costs_and_labels_every_report():
    result = simulate(profile())
    assert result["status"] == "simulated_recommendation"
    assert result["simulated"] and not result["aws_access_checked"]
    assert "not a live AWS recommendation" in result["label"]
    r = result["recommendation"]
    assert r["current_monthly_cost_usd"] == 10
    assert r["estimated_alternative_monthly_cost_usd"] == 1.01
    assert r["estimated_monthly_savings_usd"] == 8.99
    assert r["estimated_savings_pct"] == 89.9
    assert any("not live AWS prices" in a for a in result["assumptions"])


@pytest.mark.parametrize("mutation", ["live_answers", "mixed", "string_flag", "duplicate", "unknown_question", "bad_cpu", "bad_units", "evidence_override"])
def test_unsupported_or_ambiguous_inputs_fail_explicitly(mutation):
    p = profile()
    if mutation == "live_answers": p["simulated"] = False
    if mutation == "string_flag": p["simulated"] = "true"
    if mutation == "duplicate": p["answers"][1] = p["answers"][0]
    if mutation == "unknown_question": p["answers"][0]["question"] = "Anything?"
    if mutation == "bad_cpu": p["answers"][7]["answer"] = "Average: 99%; peak: 25%"
    if mutation == "bad_units": p["answers"][1]["answer"] = "20 minutes"
    if mutation == "evidence_override": p["evidence"] = {"verified": True}
    with pytest.raises(ValueError):
        chosen = simulation_profile([p, profile()] if mutation == "mixed" else [p])
        simulate(chosen)


@pytest.mark.parametrize("index,answer", [(1, "3600 seconds"), (8, "10000000"), (0, "Runs continuously")])
def test_simulation_does_not_force_compatibility_or_savings(index, answer):
    p = profile()
    p["answers"][index]["answer"] = answer
    result = simulate(p)
    assert result["status"] == "simulated_no_recommendation"
    assert result["recommendation"] is None


def test_missing_answer_stays_missing():
    p = profile()
    p["answers"][4]["answer"] = "Unknown"
    result = simulate(p)
    assert result["status"] == "simulated_needs_information"
    assert result["required_information"] == ["What is the peak memory usage?"]


def test_saved_simulation_preserves_real_context_but_never_evaluates_live(tmp_path):
    wf = workflow(tmp_path)
    aid = wf.analyze()["analysis_id"]
    p = profile()
    p["account_id"] = "111111111111"
    wf.engine.evaluate.side_effect = AssertionError("Live evaluation")
    wf.engine._scope.side_effect = AssertionError("Live scope")
    result = wf.recommend(aid, "ec2", regions=["us-east-1"], workload_inputs=[p])
    assert result["status"] == "simulated_recommendation"
    assert result["selected_period_cost_before_credits_usd"] == 120
    saved = wf.store.load_recommendation(result["recommendation_id"], wf._fingerprint(), wf.now_fn().timestamp())
    assert recommendation_view(saved) == recommendation_view(result)
    assert "not a live AWS recommendation" in recommendation_view(saved)["label"]
    wf.engine.evaluate.assert_not_called()
    wf.engine._scope.assert_not_called()


def test_below_threshold_does_not_return_simulated_recommendation(tmp_path):
    wf = workflow(tmp_path)
    aid = wf.analyze()["analysis_id"]
    wf.engine.config.config["cost_thresholds"]["services"]["ec2"] = 1000
    p = profile()
    p["account_id"] = "111111111111"
    result = wf.recommend(aid, "ec2", workload_inputs=[p])
    assert result["status"] == "below_threshold"
    assert result["simulated"] and result["recommendation"] is None
    wf.engine.evaluate.assert_not_called()


@pytest.mark.parametrize("field,value", [("account_id", "222222222222"), ("region", "us-west-2"), ("resource_id", "i-other")])
def test_scope_conflicts_rejected(tmp_path, field, value):
    wf = workflow(tmp_path)
    aid = wf.analyze()["analysis_id"]
    p = profile()
    p["account_id"] = "111111111111"
    p[field] = value
    with pytest.raises(ValueError):
        wf.recommend(aid, "ec2", regions=["us-east-1"], resource_id="i-08228576b5fffc52a", workload_inputs=[p])
    wf.engine.evaluate.assert_not_called()


def test_real_api_dependency_simulation_and_saved_get_never_construct_aws_clients(tmp_path, monkeypatch):
    wf = workflow(tmp_path, now=datetime.now(timezone.utc))
    aid = wf.analyze("2026-11-01", "2026-11-13")["analysis_id"]
    wf.engine.config.account_registry = wf.registry
    monkeypatch.setattr(api, "ConfigService", Mock(return_value=wf.engine.config))
    monkeypatch.setenv("COST_ANALYZER_ANALYSIS_DB", str(tmp_path/"analyses.sqlite3"))
    monkeypatch.setattr(api, "_build_service", Mock(side_effect=AssertionError("Live service constructed")))
    monkeypatch.setattr(api, "SessionManager", Mock(side_effect=AssertionError("AWS sessions constructed")))
    import boto3
    monkeypatch.setattr(boto3, "client", Mock(side_effect=AssertionError("AWS client")))
    monkeypatch.setattr(boto3, "Session", Mock(side_effect=AssertionError("AWS session")))
    body = copy.deepcopy(EXAMPLE)
    body["analysis_id"] = aid
    body["workload_inputs"][0]["account_id"] = "111111111111"
    with TestClient(api.app) as client:
        response = client.post("/recommendations", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "simulated_recommendation"
        assert client.get("/recommendations/" + result["recommendation_id"]).json() == result
        body["workload_inputs"][0]["simulated"] = False
        bad = client.post("/recommendations", json=body)
        assert bad.status_code == 422
        assert "Live mode requires verified evidence" in bad.json()["detail"]
    api._build_service.assert_not_called()
    api.SessionManager.assert_not_called()
