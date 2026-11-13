"""Step 38.2 tests: settings loader (approved values, fail-closed validation, unset thresholds stay unset)."""
import json

import pytest

from agent.services.alternative_decision_settings import (
    DECISION_SETTING_PATHS,
    WINDOW_MINIMUM_PATH,
    WINDOW_PREFERRED_PATH,
    blocked_decision,
    load_alternative_decision_settings,
    setting_path,
)
from agent.services.alternative_decision_models import ReasonCode, Verdict

ALT = "lambda-steady-high-volume-to-fargate"

APPROVED = {
    "min_coverage_pct": 80.0,
    "ec2_to_fargate.headroom_pct": 20.0,
    "ec2_to_fargate.sizing_statistic": "max",
    "lambda_to_fargate.min_monthly_invocations": None,
    "lambda_to_fargate.min_average_concurrency": None,
    "lambda_to_fargate.min_active_duty_cycle_pct": None,
    "attestation.max_age_days": None,
    "pricing.min_monthly_savings_usd": None,
    "pricing.min_savings_pct": None,
    "pricing.hours_per_month": 730,
    "pricing.currency": "USD",
    "cache.ttl_seconds": 86400,
}


class FakeConfigService:
    """Same shape as ConfigService: a ``config`` mapping and a dotted ``get``."""

    def __init__(self, config):
        self.config = config


def load(section=None, **extra):
    config = dict(extra)
    if section is not None:
        config["alternative_decisions"] = section
    return load_alternative_decision_settings(config)


# ------------------------------------------------------------------ approved defaults
def test_no_section_gives_exactly_the_approved_values():
    s = load()
    assert s.valid and s.source == "defaults" and s.configuration_issues == ()
    for path, expected in APPROVED.items():
        assert s.value_of(path) == expected, path


def test_every_documented_setting_has_an_approved_default():
    assert set(APPROVED) == set(DECISION_SETTING_PATHS)


def test_unset_thresholds_are_none_not_invented_defaults():
    s = load()
    for path in ("lambda_to_fargate.min_monthly_invocations", "lambda_to_fargate.min_average_concurrency",
                 "lambda_to_fargate.min_active_duty_cycle_pct", "attestation.max_age_days",
                 "pricing.min_monthly_savings_usd", "pricing.min_savings_pct"):
        assert s.value_of(path) is None


def test_window_is_reused_from_usage_patterns_defaults_and_overrides():
    s = load()
    assert (s.window_minimum_days, s.window_preferred_days) == (14, 30)
    s = load(usage_patterns={"window": {"preferred_days": 45, "minimum_days": 21}})
    assert (s.value_of(WINDOW_MINIMUM_PATH), s.value_of(WINDOW_PREFERRED_PATH)) == (21, 45) and s.valid


def test_accepts_config_service_like_objects_and_plain_mappings():
    cfg = {"alternative_decisions": {"min_coverage_pct": 90}}
    assert load_alternative_decision_settings(FakeConfigService(cfg)).min_coverage_pct == 90.0
    assert load_alternative_decision_settings(cfg).min_coverage_pct == 90.0
    for junk in (None, 42, "x", []):
        assert load_alternative_decision_settings(junk).valid


# ------------------------------------------------------------------ explicit values
def test_explicit_values_are_used_and_null_thresholds_stay_valid():
    s = load({
        "min_coverage_pct": 95,
        "ec2_to_fargate": {"headroom_pct": 0, "sizing_statistic": "max"},
        "lambda_to_fargate": {"min_monthly_invocations": 1000000, "min_average_concurrency": 2.5,
                              "min_active_duty_cycle_pct": None},
        "attestation": {"max_age_days": 90},
        "pricing": {"min_monthly_savings_usd": 50, "min_savings_pct": 10, "hours_per_month": 720, "currency": "usd"},
        "cache": {"ttl_seconds": 3600},
    })
    assert s.valid and s.source == "config"
    assert s.value_of("min_coverage_pct") == 95.0
    assert s.value_of("ec2_to_fargate.headroom_pct") == 0.0
    assert s.value_of("lambda_to_fargate.min_monthly_invocations") == 1000000.0
    assert s.value_of("lambda_to_fargate.min_active_duty_cycle_pct") is None
    assert s.value_of("attestation.max_age_days") == 90
    assert s.value_of("pricing.currency") == "USD" and s.value_of("cache.ttl_seconds") == 3600


def test_absent_key_keeps_the_default_while_explicit_null_unsets_a_nullable_threshold():
    s = load({"attestation": {}})
    assert s.valid and s.value_of("attestation.max_age_days") is None
    assert s.value_of("pricing.hours_per_month") == 730


# ------------------------------------------------------------------ fail closed
@pytest.mark.parametrize("path,section,bad", [
    ("min_coverage_pct", None, 0), ("min_coverage_pct", None, 101), ("min_coverage_pct", None, -1),
    ("min_coverage_pct", None, True), ("min_coverage_pct", None, "80"), ("min_coverage_pct", None, float("nan")),
    ("min_coverage_pct", None, float("inf")), ("min_coverage_pct", None, None),
    ("ec2_to_fargate.headroom_pct", "ec2_to_fargate", -5), ("ec2_to_fargate.headroom_pct", "ec2_to_fargate", "20"),
    ("ec2_to_fargate.headroom_pct", "ec2_to_fargate", None), ("ec2_to_fargate.headroom_pct", "ec2_to_fargate", True),
    ("ec2_to_fargate.sizing_statistic", "ec2_to_fargate", "p95"), ("ec2_to_fargate.sizing_statistic", "ec2_to_fargate", None),
    ("lambda_to_fargate.min_monthly_invocations", "lambda_to_fargate", 0),
    ("lambda_to_fargate.min_monthly_invocations", "lambda_to_fargate", -1),
    ("lambda_to_fargate.min_average_concurrency", "lambda_to_fargate", "x"),
    ("lambda_to_fargate.min_active_duty_cycle_pct", "lambda_to_fargate", 150),
    ("attestation.max_age_days", "attestation", 0), ("attestation.max_age_days", "attestation", 1.5),
    ("attestation.max_age_days", "attestation", True),
    ("pricing.hours_per_month", "pricing", 0), ("pricing.hours_per_month", "pricing", 745),
    ("pricing.hours_per_month", "pricing", 730.5), ("pricing.hours_per_month", "pricing", None),
    ("pricing.currency", "pricing", "EUR"), ("pricing.currency", "pricing", 5), ("pricing.currency", "pricing", None),
    ("pricing.min_savings_pct", "pricing", 0), ("pricing.min_monthly_savings_usd", "pricing", -1),
    ("cache.ttl_seconds", "cache", 0), ("cache.ttl_seconds", "cache", "86400"), ("cache.ttl_seconds", "cache", None),
])
def test_invalid_value_becomes_unusable_never_the_default(path, section, bad):
    key = path.split(".")[-1]
    body = {key: bad} if section is None else {section: {key: bad}}
    s = load(body)
    assert not s.valid
    assert s.value_of(path) is None, "an invalid value must never be replaced by its default"
    assert any(f"alternative_decisions.{path}" in text for text in s.configuration_issues)


def test_invalid_value_does_not_disturb_other_settings():
    s = load({"min_coverage_pct": -1, "cache": {"ttl_seconds": 60}})
    assert s.value_of("min_coverage_pct") is None and s.value_of("cache.ttl_seconds") == 60
    assert s.value_of("ec2_to_fargate.headroom_pct") == 20.0


@pytest.mark.parametrize("section", ["oops", 5, [1], True])
def test_non_mapping_section_blocks_everything_and_clears_all_values(section):
    s = load(section)
    assert not s.valid
    assert all(v is None for p, v in s.values.items() if p in DECISION_SETTING_PATHS)
    assert s.issues_affecting(["cache.ttl_seconds"])        # blocks_all


@pytest.mark.parametrize("group", ["ec2_to_fargate", "lambda_to_fargate", "attestation", "pricing", "cache"])
@pytest.mark.parametrize("body", ["oops", 5, None, [1]])
def test_non_mapping_group_is_an_issue_for_that_group_only(group, body):
    s = load({group: body})
    assert not s.valid
    affected = [p for p in DECISION_SETTING_PATHS if p.startswith(group + ".")]
    assert all(s.value_of(p) is None for p in affected)
    other = next(p for p in DECISION_SETTING_PATHS if "." in p and not p.startswith(group + "."))
    assert s.issues_affecting([other]) == ()


def test_unknown_keys_are_issues_that_block_every_decision():
    s = load({"min_coverage_pcnt": 50})
    assert not s.valid and s.value_of("min_coverage_pct") == 80.0
    assert s.issues_affecting(["cache.ttl_seconds"]) and "Unknown key" in s.configuration_issues[0]
    s = load({"pricing": {"hours_per_mnth": 700}})
    assert not s.valid and "Unknown key" in s.configuration_issues[0]


@pytest.mark.parametrize("window", [
    {"minimum_days": 0}, {"minimum_days": True}, {"preferred_days": "30"}, {"minimum_days": 40, "preferred_days": 30},
    {"minimum_days": 14.5}, "oops",
])
def test_invalid_usage_pattern_window_blocks_instead_of_silently_defaulting(window):
    s = load(usage_patterns={"window": window})
    assert not s.valid
    assert s.window_minimum_days is None and s.window_preferred_days is None
    assert s.issues_affecting([WINDOW_MINIMUM_PATH])
    assert any("usage_patterns" in text for text in s.configuration_issues)


def test_usage_patterns_not_a_mapping_blocks_the_window():
    s = load(usage_patterns="oops")
    assert not s.valid and s.window_minimum_days is None


def test_unrelated_usage_pattern_problems_are_not_this_loaders_concern():
    # thresholds are not reused by the decision engine; the existing loader reports them as warnings.
    s = load(usage_patterns={"thresholds": {"nope": 1}})
    assert s.valid


# ------------------------------------------------------------------ blocking
def test_blocked_decision_exposes_the_problems_and_uses_the_models_contract():
    s = load({"min_coverage_pct": -1, "attestation": {"max_age_days": 0}})
    d = blocked_decision(s, ALT, ["min_coverage_pct"], account_id="111122223333", region="us-east-1")
    assert d is not None and d.verdict is Verdict.INSUFFICIENT_DATA and d.blocked_by_configuration
    assert d.reasons[0].code is ReasonCode.CONFIGURATION_INVALID
    assert len(d.configuration_issues) == 1 and "min_coverage_pct" in d.configuration_issues[0]
    assert d.account_id == "111122223333"
    json.dumps(d.as_dict())


def test_decision_not_depending_on_the_invalid_setting_is_not_blocked():
    s = load({"cache": {"ttl_seconds": 0}})
    assert blocked_decision(s, ALT, ["min_coverage_pct", "ec2_to_fargate.headroom_pct"]) is None


def test_ancestor_issue_blocks_dependent_paths():
    s = load({"lambda_to_fargate": "oops"})
    d = blocked_decision(s, ALT, ["lambda_to_fargate.min_monthly_invocations"])
    assert d is not None and d.blocked_by_configuration


def test_valid_settings_never_block():
    assert blocked_decision(load(), ALT, list(DECISION_SETTING_PATHS)) is None


def test_value_of_unknown_path_raises_key_error():
    with pytest.raises(KeyError):
        load().value_of("no.such.setting")


def test_setting_path_mapping():
    assert setting_path("decision_setting", "min_coverage_pct") == "min_coverage_pct"
    assert setting_path("usage_pattern_setting", "window.minimum_days") == WINDOW_MINIMUM_PATH


def test_as_dict_is_json_serializable():
    json.dumps(load({"min_coverage_pct": -1}).as_dict())
    json.dumps(load().as_dict())


def test_loader_is_pure_no_file_access_and_no_aws_calls():
    # Inspect imports and calls (not raw text: the docstring legitimately names config.yaml).
    import ast

    import agent.services.alternative_decision_settings as module
    with open(module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"boto3", "botocore", "yaml", "pathlib", "os", "io"}
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "open" not in called
