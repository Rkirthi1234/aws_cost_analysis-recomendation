"""Step 38.1 tests: decision models (verdicts, structured reason codes, configuration_issues)."""
import json

import pytest

import agent.services.alternative_decision_models as models
from agent.services.alternative_decision_models import (
    AlternativeDecision,
    DecisionReason,
    ReasonCode,
    Verdict,
    configuration_invalid,
    insufficient_data,
)

ALT = "lambda-steady-high-volume-to-fargate"


def reason(code=ReasonCode.EVIDENCE_MISSING, message="required evidence is missing", **kw):
    return DecisionReason(code=code, message=message, **kw)


# ----------------------------------------------------------------- vocabulary
def test_there_are_exactly_four_verdicts():
    assert {v.value for v in Verdict} == {"compatible", "incompatible", "already_in_use", "insufficient_data"}
    assert len(Verdict) == 4


def test_required_reason_codes_exist_with_stable_values():
    assert ReasonCode.CONFIGURATION_INVALID.value == "configuration_invalid"
    assert ReasonCode.COVERAGE_BELOW_MINIMUM.value == "coverage_below_minimum"
    assert ReasonCode.EVIDENCE_MISSING.value == "evidence_missing"
    assert ReasonCode.EVIDENCE_COLLECTION_FAILED.value == "evidence_collection_failed"
    assert ReasonCode.ATTESTATION_EXPIRED.value == "attestation_expired"
    assert ReasonCode.THRESHOLD_UNSET.value == "threshold_unset"
    assert len(ReasonCode) == 6


def test_attestation_expired_and_threshold_unset_are_insufficiency_reasons_only():
    for code in (ReasonCode.ATTESTATION_EXPIRED, ReasonCode.THRESHOLD_UNSET):
        d = insufficient_data(ALT, (reason(code, "explains why no verdict can be reached"),))
        assert d.verdict is Verdict.INSUFFICIENT_DATA and not d.blocked_by_configuration
        for verdict in (Verdict.COMPATIBLE, Verdict.INCOMPATIBLE, Verdict.ALREADY_IN_USE):
            with pytest.raises(ValueError):
                AlternativeDecision(alternative_id=ALT, verdict=verdict, reasons=(reason(code),))


def test_verdicts_and_codes_serialize_as_plain_strings():
    assert json.dumps({"v": Verdict.COMPATIBLE, "c": ReasonCode.EVIDENCE_MISSING}) == '{"v": "compatible", "c": "evidence_missing"}'


# ----------------------------------------------------------------- reasons
def test_reason_requires_code_enum_and_message():
    with pytest.raises(ValueError):
        DecisionReason(code="evidence_missing", message="x")        # plain string is not a ReasonCode
    with pytest.raises(ValueError):
        DecisionReason(code=ReasonCode.EVIDENCE_MISSING, message="  ")


def test_reason_as_dict_includes_optional_context_only_when_present():
    bare = reason().as_dict()
    assert bare == {"code": "evidence_missing", "message": "required evidence is missing"}
    rich = reason(ReasonCode.COVERAGE_BELOW_MINIMUM, "coverage too low", evidence_key="cpu_avg",
                  details={"coverage_pct": 69.44, "min_coverage_pct": 80.0}).as_dict()
    assert rich["evidence_key"] == "cpu_avg" and rich["details"]["coverage_pct"] == 69.44


# ----------------------------------------------------------------- decisions
def test_insufficient_data_decision_carries_structured_reasons():
    d = insufficient_data(ALT, (reason(), reason(ReasonCode.EVIDENCE_COLLECTION_FAILED, "CloudWatch call failed")),
                          account_id="111122223333", region="us-east-1", resource_id="fn-a")
    assert d.verdict is Verdict.INSUFFICIENT_DATA
    assert [r.code for r in d.reasons] == [ReasonCode.EVIDENCE_MISSING, ReasonCode.EVIDENCE_COLLECTION_FAILED]
    assert not d.blocked_by_configuration
    out = d.as_dict()
    assert out["verdict"] == "insufficient_data" and out["configuration_issues"] == []
    assert (out["account_id"], out["region"], out["resource_id"]) == ("111122223333", "us-east-1", "fn-a")
    json.dumps(out)


def test_insufficient_data_without_a_reason_is_rejected():
    with pytest.raises(ValueError):
        AlternativeDecision(alternative_id=ALT, verdict=Verdict.INSUFFICIENT_DATA)


def test_insufficiency_codes_cannot_accompany_other_verdicts():
    for verdict in (Verdict.COMPATIBLE, Verdict.INCOMPATIBLE, Verdict.ALREADY_IN_USE):
        for code in ReasonCode:
            issues = ("x",) if code is ReasonCode.CONFIGURATION_INVALID else ()
            with pytest.raises(ValueError):
                AlternativeDecision(alternative_id=ALT, verdict=verdict, reasons=(reason(code),),
                                    configuration_issues=issues)


def test_non_insufficient_verdicts_are_buildable_without_reasons():
    for verdict in (Verdict.COMPATIBLE, Verdict.INCOMPATIBLE, Verdict.ALREADY_IN_USE):
        d = AlternativeDecision(alternative_id=ALT, verdict=verdict)
        assert d.as_dict()["verdict"] == verdict.value and d.as_dict()["reasons"] == []


def test_alternative_id_and_verdict_are_validated():
    with pytest.raises(ValueError):
        AlternativeDecision(alternative_id=" ", verdict=Verdict.COMPATIBLE)
    with pytest.raises(ValueError):
        AlternativeDecision(alternative_id=ALT, verdict="compatible")


# ----------------------------------------------------------------- configuration_invalid
def test_configuration_invalid_is_insufficient_data_and_exposes_issues():
    d = configuration_invalid(ALT, ("alternative_decisions.min_coverage_pct must be > 0, got -5",))
    assert d.verdict is Verdict.INSUFFICIENT_DATA
    assert [r.code for r in d.reasons] == [ReasonCode.CONFIGURATION_INVALID]
    assert d.configuration_issues == ("alternative_decisions.min_coverage_pct must be > 0, got -5",)
    assert d.blocked_by_configuration is True
    out = d.as_dict()
    assert out["verdict"] == "insufficient_data"
    assert out["reasons"][0]["code"] == "configuration_invalid"
    assert out["configuration_issues"] == ["alternative_decisions.min_coverage_pct must be > 0, got -5"]


def test_configuration_invalid_needs_at_least_one_issue():
    with pytest.raises(ValueError):
        configuration_invalid(ALT, ())
    with pytest.raises(ValueError):
        configuration_invalid(ALT, ("  ",))


def test_configuration_invalid_reason_without_issues_is_rejected():
    with pytest.raises(ValueError):
        AlternativeDecision(alternative_id=ALT, verdict=Verdict.INSUFFICIENT_DATA,
                            reasons=(reason(ReasonCode.CONFIGURATION_INVALID, "bad config"),))


def test_configuration_issues_without_the_configuration_reason_are_rejected():
    with pytest.raises(ValueError):
        AlternativeDecision(alternative_id=ALT, verdict=Verdict.INSUFFICIENT_DATA, reasons=(reason(),),
                            configuration_issues=("something",))


def test_configuration_reason_may_be_combined_with_other_insufficiency_reasons():
    d = AlternativeDecision(alternative_id=ALT, verdict=Verdict.INSUFFICIENT_DATA,
                            reasons=(reason(ReasonCode.CONFIGURATION_INVALID, "bad config"), reason()),
                            configuration_issues=("bad value",))
    assert d.blocked_by_configuration is True


# ----------------------------------------------------------------- separation / immutability
def test_decisions_are_immutable():
    d = configuration_invalid(ALT, ("x",))
    with pytest.raises(Exception):
        d.verdict = Verdict.COMPATIBLE


def test_module_is_independent_of_the_usage_evidence_output():
    source = open(models.__file__, encoding="utf-8").read()
    for module in ("usage_evidence", "metrics_collector", "inventory_collector", "usage_patterns", "boto3"):
        assert f"import {module}" not in source and f"services.{module}" not in source
