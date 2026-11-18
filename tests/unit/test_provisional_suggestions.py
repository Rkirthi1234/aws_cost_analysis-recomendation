"""Potential candidates are useful without inventing compatibility or savings."""
from copy import deepcopy

import pytest

from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.provisional_suggestions import build_provisional_suggestions
from agent.services.report_views import recommendation_view
from test_cost_analysis_workflow import workflow, report, EC2L, EC2F, NOW

CATALOG = AlternativesCatalog.load()
SCOPE = {"account_id": "111111111111", "region": "us-east-1", "resource_id": "i-test"}


def candidate(eid=EC2L):
    rr = report(eid, status="needs_information")
    rr["decision"].update(**SCOPE, verdict="insufficient_data", configuration_issues=[], evidence={},
        reasons=[{"code": "evidence_missing", "evidence_key": "workload.lambda_invocation_type", "message": "Invocation missing"},
                 {"code": "evidence_missing", "evidence_key": "ec2.peak_memory_mib", "message": "Memory coverage missing"}])
    rr["cost_comparison"] = {"status": "not_evaluated", "reason": "Compatibility required before pricing."}
    return {"alternative_id": eid, "alternative": "AWS Lambda" if eid == EC2L else "Amazon ECS on AWS Fargate",
            "status": "needs_information", "details": {"status": "evaluated", "reports": [rr], "errors": [], "input_summary": [{
                **SCOPE, "application": {"trigger": "manual", "monthly_runs_min": 1, "monthly_runs_max": 2},
                "measurement_report": {"input_file_bytes": 1000000, "processing_duration_seconds": .01749}}]}}


def test_actual_job_context_supports_suggestion_not_a_validated_recommendation():
    result = build_provisional_suggestions([candidate()], CATALOG)
    assert len(result) == 1
    item = result[0]
    assert item["service"] == "AWS Lambda"
    assert item["compatibility"] == "not_fully_verified"
    assert item["cost_advantage"] == "not_established"
    assert item["estimated_monthly_savings_usd"] is None
    assert "manual" in item["reason"]
    assert item["questions"][0]["field"] == "workload.lambda_invocation_type"
    assert "synchronous" in item["questions"][0]["allowed_values"]
    assert all(q["field"] != "ec2.peak_memory_mib" for q in item["questions"])
    assert any(g["field"] == "ec2.peak_memory_mib" for g in item["evidence_gaps"])


@pytest.mark.parametrize("mode", ["no_facts", "incompatible", "already_used", "no_savings", "errors", "partial", "config", "false_event", "different_resource"])
def test_no_provisional_choice_from_catalog_alone_or_contradictory_evidence(mode):
    c = candidate()
    d = c["details"]["reports"][0]["decision"]
    if mode == "no_facts": c["details"]["input_summary"] = []
    if mode == "incompatible": d["verdict"] = "incompatible"
    if mode == "already_used": d["verdict"] = "already_in_use"
    if mode == "no_savings": c["details"]["reports"][0]["status"] = "no_cost_advantage"
    if mode == "errors": c["details"]["errors"] = [{"message": "AccessDenied"}]
    if mode == "partial": c["details"]["status"] = "partial"
    if mode == "config": d["configuration_issues"] = ["Invalid rule setting"]
    if mode == "false_event": d["evidence"]["workload.event_driven"] = {"status": "available", "value": False}
    if mode == "different_resource": c["details"]["input_summary"][0]["resource_id"] = "i-other"
    assert not build_provisional_suggestions([c], CATALOG)


def test_container_candidate_needs_positive_container_evidence():
    c = candidate(EC2F)
    assert not build_provisional_suggestions([c], CATALOG)
    c["details"]["reports"][0]["decision"]["evidence"]["workload.containerizable"] = {"status": "available", "value": True}
    assert build_provisional_suggestions([c], CATALOG)[0]["service"] == "Amazon ECS on AWS Fargate"


def test_compatible_candidate_with_incomplete_pricing_remains_unpriced():
    c = candidate()
    c["details"]["reports"][0]["decision"]["verdict"] = "compatible"
    c["details"]["input_summary"] = []
    result = build_provisional_suggestions([c], CATALOG)[0]
    assert result["compatibility"] == "passed_existing_rules"
    assert result["estimated_monthly_cost_usd"] is None


def test_workflow_saves_and_get_view_preserves_provisional_result(tmp_path):
    wf = workflow(tmp_path)
    aid = wf.analyze()["analysis_id"]
    def evaluate(eid, *args):
        return candidate()["details"] if eid == EC2L else {"status": "evaluated", "reports": [], "errors": []}
    wf.engine.evaluate.side_effect = evaluate
    output = wf.recommend(aid, "ec2")
    assert output["status"] == "provisional_suggestion"
    assert output["recommendation"] is None
    shown = recommendation_view(output)
    assert shown["suggestion"]["service"] == "AWS Lambda"
    assert "required_information" not in shown  # candidate-specific questions replace the merged list
    saved = wf.store.load_recommendation(output["recommendation_id"], wf._fingerprint(), NOW.timestamp())
    assert recommendation_view(saved) == shown
    assert not saved["preferred_recommendations"]


def test_multiple_candidates_are_not_ranked_by_unavailable_prices():
    candidates = [candidate(), candidate(EC2F)]
    candidates[1]["details"]["reports"][0]["decision"]["evidence"]["workload.containerizable"] = {"status": "available", "value": True}
    suggestions = build_provisional_suggestions(candidates, CATALOG)
    result = recommendation_view({"status": "provisional_suggestion", "candidates": candidates, "provisional_suggestions": suggestions})
    assert len(result["suggestions"]) == 2 and "suggestion" not in result
    assert "recommendation" not in result


@pytest.mark.parametrize("field,value", [("workload.max_event_duration_seconds", 3600), ("ec2.peak_memory_mib", 20000)])
def test_known_standard_lambda_limit_conflicts_are_not_ignored(field, value):
    c = candidate()
    c["details"]["reports"][0]["decision"]["evidence"][field] = {"status": "available", "value": value}
    assert not build_provisional_suggestions([c], CATALOG)


def test_default_view_hides_technical_data_but_details_retains_everything():
    candidates = [candidate()]
    suggestions = build_provisional_suggestions(candidates, CATALOG)
    raw = {"status": "provisional_suggestion", "candidates": candidates,
           "provisional_suggestions": suggestions,
           "threshold": {"status": "threshold_reached", "threshold_usd": .01, "threshold_usd_exact": "0.01"}}
    brief = recommendation_view(raw)
    assert brief["suggestion"]["service"] == "AWS Lambda"
    assert brief["suggestion"]["savings_status"] == "not_calculated"
    assert brief["suggestion"]["estimated_monthly_savings_usd"] is None
    assert brief["suggestion"]["pending_checks"]
    assert not {"questions", "evidence_gaps", "supporting_facts", "input_path"}.intersection(brief["suggestion"])
    assert not {"details", "workload_inputs_received", "cost_basis", "limitations", "required_information"}.intersection(brief)
    assert brief["threshold"] == {"status": "threshold_reached", "threshold_usd": .01}
    detailed = recommendation_view(raw, include_details=True)
    assert detailed["details"] is raw
    assert detailed["details"]["provisional_suggestions"][0]["questions"] == suggestions[0]["questions"]
    assert "evidence_gaps" in detailed["details"]["provisional_suggestions"][0]
    assert raw["provisional_suggestions"][0] == suggestions[0]
