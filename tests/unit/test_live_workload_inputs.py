"""Live facts stay distinct from incomplete measurements and pricing assumptions."""
from copy import deepcopy
from datetime import timedelta

import pytest

from agent.services.live_workload_inputs import normalize_live_profiles
from agent.services.report_views import recommendation_view
from agent.services.alternative_pricing import AlternativeCostComparison
from test_recommendation_workflow import NOW, SCOPE, EC2L, entry, settings, evaluate, pricing_profile, FixedPrices
from test_cost_analysis_workflow import workflow


def live_profile():
    return {**SCOPE, "simulated": False,
            "application": {"source": "Workload owner interview", "observed_at": NOW.isoformat(),
                            "trigger": "manual", "input_file_bytes": 1000000,
                            "monthly_runs_min": 1, "monthly_runs_max": 2,
                            "event_driven": None, "lambda_invocation_type": None},
            "measurement_report": {"simulated": False, "resource_id": SCOPE["resource_id"],
                "started_at": NOW.isoformat(), "input_file_bytes": 1000000, "word_count": 186813,
                "processing_duration_seconds": .017491911, "processing_cpu_seconds": .017468745,
                "python_process_peak_memory_mib": 24.7265625,
                "measurement_scope": "Local processing only; excludes S3 transfers and CLI processes"}}


def test_actual_report_is_context_not_complete_benchmark():
    profiles, summary = normalize_live_profiles([live_profile()], NOW)
    assert profiles[0]["evidence"] == {}
    assert "pricing" not in profiles[0]
    assert summary[0]["measurement_report"]["python_process_peak_memory_mib"] == 24.7265625
    assert any("do not establish" in s for s in summary[0]["limitations"])
    assert "measurement_report" not in profiles[0]


def test_explicit_owner_facts_are_converted_without_granting_assessment():
    p = live_profile()
    p["application"].update(event_driven=True, lambda_invocation_type="synchronous", max_request_payload_mb=.001)
    normalized, _ = normalize_live_profiles([p], NOW)
    evidence = normalized[0]["evidence"]
    assert evidence["workload.event_driven"]["value"] is True
    assert evidence["workload.event_driven"]["source"] == "Workload owner interview"
    assert "ec2.peak_memory_mib" not in evidence
    assert "workload.max_event_duration_seconds" not in evidence
    assert "workload_assessment" not in normalized[0]


@pytest.mark.parametrize("mutation", ["simulation", "answers", "false_numeric", "future", "conflict", "scope", "range", "flat"])
def test_invalid_or_unsupported_inputs_are_never_silently_ignored(mutation):
    p = live_profile()
    if mutation == "simulation": p["simulated"] = True
    if mutation == "answers": p["answers"] = []
    if mutation == "false_numeric": p["application"]["max_request_payload_mb"] = False
    if mutation == "future": p["application"]["observed_at"] = (NOW+timedelta(days=1)).isoformat()
    if mutation == "conflict":
        p["application"]["event_driven"] = True
        p["evidence"] = {"workload.event_driven": {"value": False}}
    if mutation == "scope": p["measurement_report"]["resource_id"] = "i-other"
    if mutation == "range": p["application"]["monthly_runs_min"] = 3
    if mutation == "flat": p["peak_memory_mib"] = 256
    with pytest.raises(ValueError): normalize_live_profiles([p], NOW)


def test_profile_not_mutated_and_existing_evidence_remains_compatible():
    p = {**SCOPE, "evidence": {"some.fact": {"value": 3}}}
    original = deepcopy(p)
    normalized, summary = normalize_live_profiles([p], NOW)
    assert normalized == [original] and not summary
    assert p == original


def test_view_exposes_live_context_and_limitations_without_claiming_savings():
    _, summary = normalize_live_profiles([live_profile()], NOW)
    out = recommendation_view({"status": "needs_information", "candidates": [{
        "alternative": "AWS Lambda", "status": "needs_information",
        "details": {"input_summary": summary, "reports": []}}]})
    assert out["workload_inputs_received"] == summary
    assert out["recommendation"] is None
    assert any("do not establish" in s for s in out["limitations"])


def test_monthly_projection_is_not_scaled_as_a_continuous_instance():
    prices = FixedPrices()
    profile = pricing_profile()
    profile["current_cost"].update(cost_basis="monthly_projection", monthly_amount_usd="0.05", schedule_assessed=True)
    result = AlternativeCostComparison(prices, settings(), lambda: NOW).compare(
        entry(EC2L), evaluate(EC2L), {}, {"pricing": profile})
    assert result["status"] == "estimated"
    assert result["exact"]["current"] == "0.05"
    assert any("not an observed monthly bill" in s for s in result["assumptions"])


def test_projected_cost_without_schedule_assessment_blocks_pricing():
    profile = pricing_profile()
    profile["current_cost"].update(cost_basis="monthly_projection", monthly_amount_usd="0.05")
    prices = FixedPrices()
    result = AlternativeCostComparison(prices, settings(), lambda: NOW).compare(
        entry(EC2L), evaluate(EC2L), {}, {"pricing": profile})
    assert result["status"] == "insufficient_data"
    assert not prices.calls


def test_simulation_is_rejected_before_reading_saved_analysis(tmp_path):
    wf = workflow(tmp_path)
    with pytest.raises(ValueError, match="live-only"):
        wf.recommend("missing", workload_inputs=[{**SCOPE, "simulated": True}])


def test_service_connects_typed_facts_and_preserves_incomplete_real_report():
    from test_recommendation_workflow import service, AID, REGION
    p = live_profile()
    p["application"].update(event_driven=True, lambda_invocation_type="synchronous")
    result = service().evaluate(EC2L, AID, [REGION], [p])
    assert result["input_summary"][0]["measurement_report"]["word_count"] == 186813
    report = result["reports"][0]
    assert report["status"] == "needs_information"
    assert report["decision"]["evidence"]["workload.event_driven"]["value"] is True
    assert report["cost_comparison"]["status"] == "not_evaluated"
