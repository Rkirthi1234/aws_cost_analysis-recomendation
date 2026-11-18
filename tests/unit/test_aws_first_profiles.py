"""AWS-first assessment, scoped profile reuse and typed small answers."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agent import api
from agent.services.workload_profiles import WorkloadProfiles, merge_profiles, simple_answers
from agent.services.report_views import recommendation_view
from test_cost_analysis_workflow import workflow, NOW, CATALOG
from test_recommendation_workflow import service, SCOPE, EC2L
from test_live_workload_inputs import live_profile


def connected(tmp_path):
    wf = workflow(tmp_path)
    live = service()
    wf.engine.evaluate.side_effect = live.evaluate
    return wf, live


def test_first_request_inspects_aws_and_returns_candidates_without_user_data(tmp_path):
    wf, live = connected(tmp_path)
    result = wf.recommend(wf.analyze()["analysis_id"], "ec2")
    assert live.collector.calls
    assert result["status"] == "candidates_for_assessment"
    shown = recommendation_view(result)
    assert {c["service"] for c in shown["candidates"]} == {"AWS Lambda", "Amazon ECS on AWS Fargate"}
    lambda_card = next(c for c in shown["candidates"] if c["service"] == "AWS Lambda")
    assert lambda_card["automatic_facts"]["ec2.memory_mib"] == 2048
    assert all(q["field"] not in ("ec2.peak_memory_mib", "ec2.cpu_max_pct") for q in lambda_card["questions"])
    assert all(c["validation"] == "not_validated" for c in shown["candidates"])
    assert result["recommendation"] is None
    assert not result["provisional_suggestions"]


def test_real_report_and_answers_are_reused_across_new_workflow_instances(tmp_path):
    wf, live = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    first = wf.recommend(aid, "ec2", workload_inputs=[live_profile()])
    assert first["status"] == "provisional_suggestion"
    second_wf, second_live = connected(tmp_path)
    second = second_wf.recommend(aid, "ec2", workload_inputs=[])
    assert second["status"] == "provisional_suggestion"
    assert second["workload_profile"]["reused"] == 1
    assert second_live.collector.calls  # fresh AWS inspection, not stale report reuse
    summary = second["candidates"][1]["details"]["input_summary"][0]
    assert summary["measurement_report"]["started_at"] == live_profile()["measurement_report"]["started_at"]


def test_simple_answers_refine_and_are_saved_without_reentering_scope_metadata(tmp_path):
    wf, _ = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    result = wf.recommend(aid, "ec2", resource_id="i-test", answers={
        "trigger": "manual", "runs_per_month": {"min": 1, "max": 2},
        "purpose": "Count words in S3 text files", "event_driven": True})
    assert result["status"] == "provisional_suggestion"
    again = wf.recommend(aid, "ec2")
    assert again["status"] == "provisional_suggestion"
    evidence = again["candidates"][1]["details"]["reports"][0]["decision"]["evidence"]
    assert evidence["workload.event_driven"]["value"] is True
    assert again["workload_profile"]["reused"] == 1


def test_explicit_new_answer_supersedes_old_fact(tmp_path):
    wf, _ = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    wf.recommend(aid, "ec2", resource_id="i-test", answers={"event_driven": True})
    changed = wf.recommend(aid, "ec2", resource_id="i-test", answers={"event_driven": False})
    assert not any(s["service"] == "AWS Lambda" for s in changed["provisional_suggestions"])
    again = wf.recommend(aid, "ec2")
    assert not any(s["service"] == "AWS Lambda" for s in again["provisional_suggestions"])


def test_profile_storage_is_scoped_expires_and_reads_do_not_refresh(tmp_path):
    store = WorkloadProfiles(str(tmp_path/"profiles.sqlite3"), ttl=10, maximum=2)
    p = live_profile()
    scope = {SCOPE["account_id"]: [SCOPE["region"]]}
    store.save("fingerprint", "ec2", p, 100)
    assert store.load("fingerprint", "ec2", scope, None, 105) == [p]
    assert not store.load("other", "ec2", scope, None, 105)
    assert not store.load("fingerprint", "rds", scope, None, 105)
    assert not store.load("fingerprint", "ec2", {SCOPE["account_id"]: ["us-west-2"]}, None, 105)
    assert not store.load("fingerprint", "ec2", scope, "i-other", 105)
    assert not store.load("fingerprint", "ec2", scope, None, 110)


def test_read_below_threshold_does_not_inspect_aws_or_save_answers(tmp_path):
    wf, live = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    wf.engine.config.config["cost_thresholds"]["services"]["ec2"] = 1000
    result = wf.recommend(aid, "ec2", resource_id="i-test", answers={"event_driven": True})
    assert result["status"] == "below_threshold"
    assert not live.collector.calls


def test_ambiguous_answers_and_measurement_overrides_are_rejected(tmp_path):
    wf, _ = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    with pytest.raises(ValueError, match="select resource_id"):
        wf.recommend(aid, "ec2", answers={"trigger": "manual"})
    with pytest.raises(ValueError, match="Unsupported answer"):
        wf.recommend(aid, "ec2", resource_id="i-test", answers={"ec2.memory_mib": 10})


def test_partial_update_preserves_older_fact_observation_date():
    p = live_profile()
    p["application"]["event_driven"] = True
    newer = simple_answers({"purpose": "New purpose"}, SCOPE, CATALOG, "ec2", NOW+timedelta(hours=1))
    merged = merge_profiles([p], [newer])[0]
    old = merged["evidence"]["workload.event_driven"]
    assert old["observed_at"] == p["application"]["observed_at"]
    assert merged["measurement_report"]["started_at"] == p["measurement_report"]["started_at"]
    assert "event_driven" not in merged["application"]
    assert p["application"]["event_driven"] is True


def test_saved_get_does_not_reinspect_and_simple_answers_api_round_trip(tmp_path):
    wf, live = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    api.app.dependency_overrides[api.get_cost_workflow] = lambda: wf
    from agent.services.cost_analysis_workflow import SavedReportReader
    api.app.dependency_overrides[api.get_saved_reports] = lambda: SavedReportReader(wf.store, wf.registry, lambda: NOW)
    try:
        with TestClient(api.app) as client:
            first = client.post("/recommendations", json={"analysis_id": aid, "service": "ec2"})
            assert first.status_code == 200
            assert first.json()["status"] == "candidates_for_assessment"
            answered = client.post("/recommendations", json={"analysis_id": aid, "service": "ec2", "resource_id": "i-test", "answers": {"event_driven": True}})
            assert answered.status_code == 200, answered.text
            assert answered.json()["status"] == "provisional_suggestion"
            again = client.post("/recommendations", json={"analysis_id": aid, "service": "ec2"})
            assert again.json()["workload_profile"]["reused"] == 1
            count = len(live.collector.calls)
            assert client.get("/recommendations/"+again.json()["recommendation_id"]).json() == again.json()
            assert len(live.collector.calls) == count
    finally:
        api.app.dependency_overrides.clear()


def test_new_simple_owner_plan_can_support_provisional_candidate_without_fake_metrics(tmp_path):
    wf, _ = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    result = wf.recommend(aid, "ec2", resource_id="i-test", answers={
        "trigger": "manual", "runs_per_month": {"min": 1, "max": 2}, "purpose": "Count text words"})
    assert result["status"] == "provisional_suggestion"
    suggestion = result["provisional_suggestions"][0]
    assert suggestion["service"] == "AWS Lambda"
    assert "test exists" not in suggestion["reason"]
    assert suggestion["estimated_monthly_savings_usd"] is None


def test_upgrade_can_recover_recent_real_context_from_saved_reports(tmp_path):
    wf, _ = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    wf.recommend(aid, "ec2", workload_inputs=[live_profile()])
    import sqlite3
    with sqlite3.connect(wf.store.path) as connection:
        connection.execute("DELETE FROM workload_profiles")
    result = wf.recommend(aid, "ec2", workload_inputs=[])
    assert result["status"] == "provisional_suggestion"
    assert result["workload_profile"]["reused"] == 1


def test_canonical_null_answer_clears_known_fact(tmp_path):
    wf, _ = connected(tmp_path)
    aid = wf.analyze()["analysis_id"]
    wf.recommend(aid, "ec2", resource_id="i-test", answers={"workload.event_driven": True})
    changed = wf.recommend(aid, "ec2", resource_id="i-test", answers={"workload.event_driven": None})
    assert not changed["provisional_suggestions"]
