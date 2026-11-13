"""Step 38.3 tests: typed evidence, rule and reference definitions of the five approved catalog entries.

These tests check the DATA the catalog carries (and the parser that validates it). Nothing here evaluates a
rule: the compatibility evaluator belongs to a later step.
"""
import copy
import re
from datetime import date

import pytest
import yaml

from agent.services import aws_limits
from agent.services.alternative_decision_models import ReasonCode, Verdict
from agent.services.alternative_decision_settings import (
    DECISION_SETTING_PATHS,
    WINDOW_MINIMUM_PATH,
    WINDOW_PREFERRED_PATH,
    blocked_decision,
    load_alternative_decision_settings,
)
from agent.services.alternatives_catalog import (
    AWS_LIMIT_REFS,
    DEFAULT_CATALOG_PATH,
    OPERATORS,
    ROLE_INFORMATIONAL,
    ROLE_REQUIRED,
    RULE_VERDICTS,
    VERDICT_PRECEDENCE,
    AlternativesCatalog,
    CatalogValidationError,
    ParameterRef,
)

NAT = "nat-gateway-to-gateway-endpoints"
RDS = "rds-mysql-postgres-to-aurora-serverless-v2"
LAMBDA_FARGATE = "lambda-steady-high-volume-to-fargate"
EC2_LAMBDA = "ec2-short-event-driven-to-lambda"
EC2_FARGATE = "ec2-containerized-to-fargate"
# The order of the shipped catalog: the three Phase 1 entries, then the two Phase 2 EC2 entries.
CATALOG_ORDER = [NAT, RDS, LAMBDA_FARGATE, EC2_LAMBDA, EC2_FARGATE]


@pytest.fixture(scope="module")
def catalog():
    return AlternativesCatalog.load()


@pytest.fixture
def entries(catalog):
    return {e.id: e for e in catalog.entries}


def rules_of(entry):
    return {r.id: r for r in entry.decision_rules}


def verdict_ids(entry, verdict):
    return {r.id for r in entry.decision_rules if r.verdict == verdict}


def evidence_text(entry):
    parts = [e.key + " " + e.description for e in entry.evidence_requirements]
    parts += [r.id + " " + r.description for r in entry.decision_rules]
    return " ".join(parts)


def all_conditions(entry):
    return [c for r in entry.decision_rules for c in r.conditions]


# ------------------------------------------------------------------ catalog identity
def test_catalog_ids_and_order_are_preserved(catalog):
    assert [e.id for e in catalog.entries] == CATALOG_ORDER
    assert all(e.approved for e in catalog.entries)


def test_descriptive_required_evidence_strings_are_still_present_for_the_evidence_output(catalog):
    # usage_evidence echoes these strings unchanged; the typed definitions are additional.
    for entry in catalog.entries:
        assert entry.required_evidence and all(isinstance(s, str) and s for s in entry.required_evidence)


def test_verdict_precedence_and_rule_verdicts():
    assert VERDICT_PRECEDENCE == ("already_in_use", "incompatible", "compatible", "insufficient_data")
    assert RULE_VERDICTS == ("already_in_use", "incompatible", "compatible")      # insufficient_data is never a rule
    assert {v.value for v in Verdict} == set(VERDICT_PRECEDENCE)


@pytest.mark.parametrize("entry_id", CATALOG_ORDER)
def test_every_entry_has_typed_evidence_rules_and_references(entries, entry_id):
    entry = entries[entry_id]
    assert entry.evidence_requirements and entry.decision_rules and entry.references
    assert verdict_ids(entry, "compatible") and verdict_ids(entry, "incompatible")
    assert all(r.verdict != "insufficient_data" for r in entry.decision_rules)
    assert len({r.id for r in entry.decision_rules}) == len(entry.decision_rules)
    assert len({e.key for e in entry.evidence_requirements}) == len(entry.evidence_requirements)


@pytest.mark.parametrize("entry_id", CATALOG_ORDER)
def test_every_reference_has_an_aws_docs_url_and_a_verification_date(entries, entry_id):
    for ref in entries[entry_id].references:
        assert ref.url.startswith("https://docs.aws.amazon.com/"), ref
        date.fromisoformat(ref.verified_on)
        assert ref.title


@pytest.mark.parametrize("entry_id", CATALOG_ORDER)
def test_rule_and_evidence_references_are_declared_and_documentation_rules_cite_a_source(entries, entry_id):
    entry = entries[entry_id]
    declared = {r.id for r in entry.references}
    for rule in entry.decision_rules:
        assert set(rule.references) <= declared, rule.id
        if rule.basis in ("aws_documentation", "both"):
            assert rule.references, rule.id
    for ev in entry.evidence_requirements:
        assert set(ev.references) <= declared, ev.key


def test_catalog_references_match_the_aws_limits_module_sources(entries):
    lam = {r.id: r for r in entries[EC2_LAMBDA].references}["lambda-quotas"]
    assert lam.url == aws_limits.LAMBDA_LIMITS_URL and lam.verified_on == aws_limits.VERIFIED_ON
    far = {r.id: r for r in entries[EC2_FARGATE].references}["fargate-task-sizes"]
    assert far.url == aws_limits.FARGATE_TASK_SIZE_URL and far.verified_on == aws_limits.VERIFIED_ON


# ------------------------------------------------------------------ evidence roles
@pytest.mark.parametrize("entry_id", CATALOG_ORDER)
def test_informational_evidence_is_never_used_by_a_rule(entries, entry_id):
    entry = entries[entry_id]
    informational = {e.key for e in entry.evidence_requirements if e.role == ROLE_INFORMATIONAL}
    used = {k for c in all_conditions(entry) for k in c.evidence_keys()}
    used |= {k for r in entry.decision_rules for k in r.identifies}
    assert not informational & used
    assert all(e.role in (ROLE_REQUIRED, ROLE_INFORMATIONAL) for e in entry.evidence_requirements)


@pytest.mark.parametrize("entry_id", CATALOG_ORDER)
def test_typed_evidence_declares_where_each_value_comes_from(entries, entry_id):
    for ev in entries[entry_id].evidence_requirements:
        if ev.origin == "collected":
            assert ev.source_path and ev.source_path.startswith("regions[]."), ev.key
        if ev.origin == "derived":
            assert ev.derived_from and ev.derivation, ev.key
        if ev.origin == "attestation":
            assert ev.attestation_expiry, ev.key
        if ev.requires_metric_coverage:
            assert ev.coverage_series, ev.key


# ------------------------------------------------------------------ 1. EC2 -> Lambda
def test_ec2_to_lambda_rules_follow_the_approved_design(entries):
    entry = entries[EC2_LAMBDA]
    rules = rules_of(entry)
    assert verdict_ids(entry, "incompatible") == {
        "ec2-lambda-not-event-driven", "ec2-lambda-exceeds-timeout", "ec2-lambda-exceeds-request-payload",
        "ec2-lambda-exceeds-response-payload", "ec2-lambda-exceeds-memory",
    }
    assert verdict_ids(entry, "compatible") == {"ec2-lambda-within-limits"}
    assert not verdict_ids(entry, "already_in_use")

    compatible = rules["ec2-lambda-within-limits"]
    assert [c.op for c in compatible.conditions] == [
        "is_true", "within_applicable_lambda_limit", "within_applicable_lambda_limit",
        "within_applicable_lambda_limit", "lte",
    ]
    assert [c.parameter.ref for c in compatible.conditions[1:]] == [
        "lambda.applicable.timeout_seconds", "lambda.applicable.request_payload_mb",
        "lambda.applicable.response_payload_mb", "lambda.memory_max_mb",
    ]
    assert compatible.conditions[0].evidence == "workload.event_driven"
    assert compatible.conditions[4].evidence == "ec2.peak_memory_mib"


def test_ec2_to_lambda_limits_depend_on_the_invocation_type_never_a_single_universal_limit(entries):
    entry = entries[EC2_LAMBDA]
    limit_conditions = [c for c in all_conditions(entry)
                        if c.op in ("within_applicable_lambda_limit", "exceeds_applicable_lambda_limit")]
    assert len(limit_conditions) == 6          # 3 limits x (incompatible rule + compatible rule)
    for cond in limit_conditions:
        assert cond.parameter.source == "aws_limit" and cond.parameter.ref.startswith("lambda.applicable.")
        assert cond.related_evidence == {"invocation_type": "workload.lambda_invocation_type"}
    invocation = entry.evidence_requirement("workload.lambda_invocation_type")
    assert invocation.type == "enum" and set(invocation.values) == set(aws_limits.LAMBDA_INVOCATION_TYPES)
    for cond in all_conditions(entry):          # no timeout / payload comparison uses a fixed literal
        if cond.evidence.startswith("workload.max_"):
            assert cond.value is None and cond.parameter is not None


def test_ec2_to_lambda_needs_attestations_that_can_expire_and_planned_memory_evidence(entries):
    entry = entries[EC2_LAMBDA]
    for key in ("workload.event_driven", "workload.lambda_invocation_type", "workload.max_event_duration_seconds",
                "workload.max_request_payload_mb", "workload.max_response_payload_mb"):
        ev = entry.evidence_requirement(key)
        assert ev.origin == "attestation" and ev.attestation_expiry and ev.role == ROLE_REQUIRED, key
    memory = entry.evidence_requirement("ec2.memory_utilization_max_pct")
    assert memory.origin == "planned" and memory.requires_metric_coverage and memory.coverage_series == "memory_utilization"
    peak = entry.evidence_requirement("ec2.peak_memory_mib")
    assert peak.origin == "derived" and set(peak.derived_from) == {"ec2.memory_mib", "ec2.memory_utilization_max_pct"}


# ------------------------------------------------------------------ 2. EC2 -> Fargate
def test_ec2_to_fargate_rules_follow_the_approved_design(entries):
    entry = entries[EC2_FARGATE]
    rules = rules_of(entry)
    assert verdict_ids(entry, "incompatible") == {
        "ec2-fargate-not-containerizable", "ec2-fargate-unsupported-architecture", "ec2-fargate-unsupported-os",
        "ec2-fargate-exceeds-valid-sizing",
    }
    assert verdict_ids(entry, "compatible") == {"ec2-fargate-fits-valid-sizing"}
    fits = rules["ec2-fargate-fits-valid-sizing"]
    assert [(c.evidence, c.op) for c in fits.conditions[:3]] == [
        ("workload.containerizable", "is_true"), ("ec2.architecture", "in"), ("ec2.target_os_family", "in"),
    ]
    sizing = fits.conditions[3]
    assert sizing.op == "fargate_size_fits" and sizing.evidence == "ec2.peak_vcpus_used"
    assert sizing.parameter == ParameterRef("aws_limit", "fargate.task_sizes")
    assert sizing.headroom == ParameterRef("decision_setting", "ec2_to_fargate.headroom_pct")
    assert sizing.related_evidence == {"memory": "ec2.peak_memory_mib", "os_family": "ec2.target_os_family"}
    exceeds = rules["ec2-fargate-exceeds-valid-sizing"].conditions[0]
    assert exceeds.op == "fargate_size_exceeds" and exceeds.headroom == sizing.headroom


def test_ec2_to_fargate_headroom_and_statistic_are_settings_references_not_literals(entries):
    entry = entries[EC2_FARGATE]
    cpu = entry.evidence_requirement("ec2.cpu_max_pct")
    assert cpu.parameters == (ParameterRef("decision_setting", "ec2_to_fargate.sizing_statistic"),)
    assert cpu.requires_metric_coverage and cpu.coverage_series == "cpu_max"
    assert not re.search(r"\b20\b", " ".join(r.description for r in entry.decision_rules))
    os_family = entry.evidence_requirement("ec2.target_os_family")
    assert set(os_family.values) == {"LINUX", "WINDOWS", "UNSUPPORTED"} and os_family.origin == "derived"


def test_ec2_to_fargate_container_instance_registration_is_only_informational(entries):
    entry = entries[EC2_FARGATE]
    assert entry.evidence_requirement("ec2.is_ecs_container_instance").role == ROLE_INFORMATIONAL
    containerizable = entry.evidence_requirement("workload.containerizable")
    assert containerizable.origin == "attestation" and containerizable.role == ROLE_REQUIRED


# ------------------------------------------------------------------ 3. Lambda -> Fargate
def test_lambda_to_fargate_rules_follow_the_approved_design(entries):
    entry = entries[LAMBDA_FARGATE]
    rules = rules_of(entry)
    assert verdict_ids(entry, "incompatible") == {"lambda-fargate-irreplaceable-behavior"}
    assert verdict_ids(entry, "compatible") == {"lambda-fargate-thresholds-met"}
    behavior = rules["lambda-fargate-irreplaceable-behavior"].conditions
    assert [(c.evidence, c.op, c.value) for c in behavior] == [
        ("workload.irreplaceable_lambda_behavior", "equals", "present")]
    compatible = rules["lambda-fargate-thresholds-met"].conditions
    assert [(c.evidence, c.op) for c in compatible[:3]] == [
        ("workload.trigger_replacement_assessed", "is_true"),
        ("workload.irreplaceable_lambda_behavior", "equals"),
        ("workload.containerizable", "is_true"),
    ]
    assert compatible[1].value == "absent"
    thresholds = compatible[3:]
    assert [c.op for c in thresholds] == ["gte", "gte", "gte"]
    assert [c.parameter for c in thresholds] == [
        ParameterRef("decision_setting", "lambda_to_fargate.min_monthly_invocations"),
        ParameterRef("decision_setting", "lambda_to_fargate.min_average_concurrency"),
        ParameterRef("decision_setting", "lambda_to_fargate.min_active_duty_cycle_pct"),
    ]
    assert all(c.value is None for c in thresholds)       # the thresholds are never written into the catalog


def test_lambda_to_fargate_thresholds_are_unset_in_the_approved_settings(entries):
    settings = load_alternative_decision_settings()
    for cond in all_conditions(entries[LAMBDA_FARGATE]):
        if cond.parameter is not None:
            assert settings.value_of(cond.parameter.setting_path) is None, cond.parameter


def test_lambda_to_fargate_event_source_mappings_are_informational_only(entries):
    entry = entries[LAMBDA_FARGATE]
    mappings = entry.evidence_requirement("lambda.event_source_mappings")
    assert mappings.role == ROLE_INFORMATIONAL
    assert "lambda.event_source_mappings" not in {c.evidence for c in all_conditions(entry)}
    behavior = entry.evidence_requirement("workload.irreplaceable_lambda_behavior")
    assert behavior.origin == "attestation" and set(behavior.values) == {"absent", "present"}
    for key in ("workload.trigger_replacement_assessed", "workload.containerizable"):
        assert entry.evidence_requirement(key).origin == "attestation"


def test_lambda_to_fargate_workload_metrics_declare_collectors_and_coverage(entries):
    entry = entries[LAMBDA_FARGATE]
    for key in ("lambda.invocations_hourly_mean", "lambda.duration_avg_ms", "lambda.active_hour_fraction"):
        ev = entry.evidence_requirement(key)
        assert ev.origin == "collected" and ev.requires_metric_coverage, key
    monthly = entry.evidence_requirement("lambda.monthly_invocations")
    assert monthly.parameters == (ParameterRef("decision_setting", "pricing.hours_per_month"),)


# ------------------------------------------------------------------ 4. RDS -> Aurora Serverless v2
def test_rds_to_aurora_serverless_v2_rules_follow_the_approved_design(entries):
    entry = entries[RDS]
    rules = rules_of(entry)
    assert verdict_ids(entry, "incompatible") == {"rds-aurora-sv2-no-supported-target"}
    assert verdict_ids(entry, "compatible") == {"rds-aurora-sv2-supported-and-estimated"}
    no_target = rules["rds-aurora-sv2-no-supported-target"].conditions
    assert [(c.evidence, c.op, c.value) for c in no_target] == [
        ("rds.serverless_v2_region_version_support", "equals", "unsupported")]
    compatible = rules["rds-aurora-sv2-supported-and-estimated"].conditions
    assert [(c.evidence, c.op) for c in compatible] == [
        ("rds.engine", "in"), ("rds.aurora_version_mapping", "is_present"),
        ("rds.serverless_v2_region_version_support", "equals"),
        ("rds.serverless_v2_capacity_limits", "is_present"), ("rds.workload_capacity_estimate", "is_present"),
    ]


def test_rds_never_uses_universal_acu_limits_or_converts_cpu_and_memory_to_acus(entries):
    entry = entries[RDS]
    assert not re.search(r"\d\s*ACU", evidence_text(entry), re.IGNORECASE)
    assert not any(c.parameter is not None and c.parameter.source == "aws_limit" for c in all_conditions(entry))
    assert not any(c.value not in (None, "unsupported", "supported") and isinstance(c.value, (int, float))
                   for c in all_conditions(entry))
    utilization = entry.evidence_requirement("rds.cloudwatch_utilization")
    assert utilization.role == ROLE_INFORMATIONAL
    assert "rds.cloudwatch_utilization" not in {k for c in all_conditions(entry) for k in c.evidence_keys()}
    estimate = entry.evidence_requirement("rds.workload_capacity_estimate")
    assert estimate.origin == "planned" and "NOT be a direct conversion" in estimate.description


def test_rds_unverified_version_mapping_and_missing_capacity_evidence_are_planned_not_assumed(entries):
    entry = entries[RDS]
    for key in ("rds.aurora_version_mapping", "rds.serverless_v2_capacity_limits", "rds.workload_capacity_estimate"):
        assert entry.evidence_requirement(key).origin == "planned", key
    support = entry.evidence_requirement("rds.serverless_v2_region_version_support")
    assert support.origin == "derived"
    assert set(support.derived_from) == {
        "rds.engine", "rds.engine_version", "rds.region", "rds.aurora_version_mapping"}
    assert "unverified is missing, never unsupported" in support.description
    assert set(entry.evidence_requirement("rds.engine").values) == {"mysql", "postgres"}


# ------------------------------------------------------------------ 5. NAT Gateway -> Gateway endpoints
SERVICES = ("s3", "dynamodb")


def test_nat_rules_are_evaluated_separately_for_s3_and_dynamodb(entries):
    entry = entries[NAT]
    expected = set()
    for service in SERVICES:
        expected |= {f"nat-{service}-endpoint-already-in-use", f"nat-{service}-only-cross-region-traffic",
                     f"nat-{service}-no-eligible-traffic", f"nat-{service}-eligible-traffic-remaining"}
    assert {r.id for r in entry.decision_rules} == expected
    for rule in entry.decision_rules:
        service = rule.subject
        assert service in SERVICES and f"-{service}-" in rule.id
        other = next(s for s in SERVICES if s != service)
        assert not any(other in key for c in rule.conditions for key in c.evidence_keys()), rule.id
        assert not any(other in key for key in rule.identifies), rule.id


@pytest.mark.parametrize("service", SERVICES)
def test_nat_verdict_rules_for_each_service(entries, service):
    rules = rules_of(entries[NAT])
    full = rules[f"nat-{service}-endpoint-already-in-use"]
    assert full.verdict == "already_in_use"
    assert [(c.evidence, c.op, c.value) for c in full.conditions] == [
        (f"nat.{service}.endpoint_coverage", "equals", "full")]

    cross = rules[f"nat-{service}-only-cross-region-traffic"]
    assert cross.verdict == "incompatible"
    assert [(c.evidence, c.op, c.value) for c in cross.conditions] == [
        (f"nat.{service}.same_region_bytes", "equals", 0), (f"nat.{service}.cross_region_bytes", "gt", 0)]

    none_eligible = rules[f"nat-{service}-no-eligible-traffic"]
    assert none_eligible.verdict == "incompatible"
    assert [(c.evidence, c.op, c.value) for c in none_eligible.conditions] == [
        (f"nat.{service}.same_region_bytes", "equals", 0)]

    remaining = rules[f"nat-{service}-eligible-traffic-remaining"]
    assert remaining.verdict == "compatible"
    assert remaining.conditions[0].evidence == f"nat.{service}.endpoint_coverage"
    assert remaining.conditions[0].op == "in" and set(remaining.conditions[0].values) == {"partial", "none"}
    assert (remaining.conditions[1].evidence, remaining.conditions[1].op, remaining.conditions[1].value) == (
        f"nat.{service}.same_region_bytes", "gt", 0)
    assert remaining.identifies == (f"nat.{service}.uncovered_route_table_ids",)      # uncovered route tables reported


@pytest.mark.parametrize("service", SERVICES)
def test_nat_destination_and_route_table_evidence_is_planned_because_no_collector_provides_it(entries, service):
    entry = entries[NAT]
    for key in (f"nat.{service}.same_region_bytes", f"nat.{service}.cross_region_bytes",
                f"nat.{service}.covered_route_table_ids"):
        ev = entry.evidence_requirement(key)
        assert ev.origin == "planned" and ev.role == ROLE_REQUIRED, key
    assert entry.evidence_requirement("nat.route_tables_using_nat").origin == "planned"
    coverage = entry.evidence_requirement(f"nat.{service}.endpoint_coverage")
    assert coverage.origin == "derived" and set(coverage.values) == {"full", "partial", "none"}
    uncovered = entry.evidence_requirement(f"nat.{service}.uncovered_route_table_ids")
    assert uncovered.origin == "derived" and uncovered.type == "list"


@pytest.mark.parametrize("service", SERVICES)
def test_nat_cloudwatch_totals_and_vpc_level_flags_alone_can_never_decide(entries, service):
    entry = entries[NAT]
    totals = entry.evidence_requirement("nat.total_bytes_out_to_destination")
    assert totals.role == ROLE_INFORMATIONAL and totals.origin == "collected"
    flag = entry.evidence_requirement(f"nat.{service}.vpc_has_gateway_endpoint")
    assert flag.role == ROLE_INFORMATIONAL and flag.source_path.endswith(f"vpc_has_{service}_gateway_endpoint")
    used = {k for c in all_conditions(entry) for k in c.evidence_keys()}
    assert totals.key not in used and flag.key not in used


# ------------------------------------------------------------------ parameters and settings
def test_aws_limit_references_resolve_to_the_aws_limits_module(entries):
    seen = set()
    for entry in entries.values():
        for cond in all_conditions(entry):
            if cond.parameter is not None and cond.parameter.source == "aws_limit":
                seen.add(cond.parameter.ref)
    assert seen == {"lambda.applicable.timeout_seconds", "lambda.applicable.request_payload_mb",
                    "lambda.applicable.response_payload_mb", "lambda.memory_max_mb", "fargate.task_sizes"}
    assert seen <= AWS_LIMIT_REFS
    for ref in seen:
        if ref.startswith("lambda.applicable."):
            assert ref.rsplit(".", 1)[1] in aws_limits.LAMBDA_LIMIT_KINDS
        elif ref == "lambda.memory_max_mb":
            assert "memory_max_mb" in aws_limits.LAMBDA_LIMITS
        else:
            assert aws_limits.FARGATE_TASK_SIZES


def test_every_settings_reference_exists_in_the_settings_loader(entries):
    known = set(load_alternative_decision_settings().values)
    for entry in entries.values():
        refs = [p for c in all_conditions(entry) for p in c.parameters()]
        refs += [p for e in entry.evidence_requirements for p in e.parameters]
        for ref in refs:
            if ref.source != "aws_limit":
                assert ref.setting_path in known, ref
        assert entry.decision_setting_paths() <= known, entry.id
    assert DECISION_SETTING_PATHS <= known


def test_parameter_reference_setting_paths():
    assert ParameterRef("decision_setting", "min_coverage_pct").setting_path == "min_coverage_pct"
    assert ParameterRef("usage_pattern_setting", "window.minimum_days").setting_path == WINDOW_MINIMUM_PATH
    assert ParameterRef("aws_limit", "fargate.task_sizes").setting_path is None


EXPECTED_SETTING_PATHS = {
    NAT: frozenset(),
    RDS: frozenset(),
    LAMBDA_FARGATE: frozenset({
        "lambda_to_fargate.min_monthly_invocations", "lambda_to_fargate.min_average_concurrency",
        "lambda_to_fargate.min_active_duty_cycle_pct", "pricing.hours_per_month", "min_coverage_pct",
        WINDOW_MINIMUM_PATH, WINDOW_PREFERRED_PATH, "attestation.max_age_days"}),
    EC2_LAMBDA: frozenset({"min_coverage_pct", WINDOW_MINIMUM_PATH, WINDOW_PREFERRED_PATH,
                           "attestation.max_age_days"}),
    EC2_FARGATE: frozenset({
        "ec2_to_fargate.headroom_pct", "ec2_to_fargate.sizing_statistic", "min_coverage_pct",
        WINDOW_MINIMUM_PATH, WINDOW_PREFERRED_PATH, "attestation.max_age_days"}),
}


@pytest.mark.parametrize("entry_id", CATALOG_ORDER)
def test_settings_each_entry_depends_on(entries, entry_id):
    assert entries[entry_id].decision_setting_paths() == EXPECTED_SETTING_PATHS[entry_id]


def decision_settings(config):
    return load_alternative_decision_settings(config)


def blocked_ids(catalog, settings):
    return {e.id for e in catalog.entries if blocked_decision(settings, e.id, e.decision_setting_paths())}


def test_valid_default_settings_block_nothing_and_unset_thresholds_do_not_block(catalog):
    assert blocked_ids(catalog, decision_settings(None)) == set()
    unset = decision_settings({"alternative_decisions": {"lambda_to_fargate": {"min_monthly_invocations": None},
                                                         "attestation": {"max_age_days": None}}})
    assert unset.valid and blocked_ids(catalog, unset) == set()


@pytest.mark.parametrize("config,blocked", [
    ({"ec2_to_fargate": {"headroom_pct": -1}}, {EC2_FARGATE}),
    ({"ec2_to_fargate": {"sizing_statistic": "p95"}}, {EC2_FARGATE}),
    ({"lambda_to_fargate": {"min_monthly_invocations": 0}}, {LAMBDA_FARGATE}),
    ({"lambda_to_fargate": "oops"}, {LAMBDA_FARGATE}),
    ({"pricing": {"hours_per_month": 0}}, {LAMBDA_FARGATE}),
    ({"min_coverage_pct": 0}, {LAMBDA_FARGATE, EC2_LAMBDA, EC2_FARGATE}),
    ({"attestation": {"max_age_days": 0}}, {LAMBDA_FARGATE, EC2_LAMBDA, EC2_FARGATE}),
    ({"pricing": {"currency": "EUR"}}, set()),          # referenced by no compatibility decision
    ({"cache": {"ttl_seconds": 0}}, set()),
])
def test_an_invalid_setting_blocks_exactly_the_decisions_that_depend_on_it(catalog, config, blocked):
    settings = decision_settings({"alternative_decisions": config})
    assert not settings.valid
    assert blocked_ids(catalog, settings) == blocked


def test_blocked_decision_is_insufficient_data_with_the_configuration_issue(catalog, entries):
    settings = decision_settings({"alternative_decisions": {"ec2_to_fargate": {"headroom_pct": -1}}})
    entry = entries[EC2_FARGATE]
    decision = blocked_decision(settings, entry.id, entry.decision_setting_paths(), account_id="111122223333")
    assert decision.verdict is Verdict.INSUFFICIENT_DATA and decision.blocked_by_configuration
    assert decision.reasons[0].code is ReasonCode.CONFIGURATION_INVALID
    assert "ec2_to_fargate.headroom_pct" in decision.configuration_issues[0]
    assert decision.account_id == "111122223333"


def test_unknown_setting_key_and_invalid_window_block_as_documented(catalog):
    unknown = decision_settings({"alternative_decisions": {"min_coverage_pcnt": 50}})
    assert blocked_ids(catalog, unknown) == set(CATALOG_ORDER)           # unknown keys block every decision
    window = decision_settings({"usage_patterns": {"window": {"minimum_days": 0}}})
    assert blocked_ids(catalog, window) == {LAMBDA_FARGATE, EC2_LAMBDA, EC2_FARGATE}


# ------------------------------------------------------------------ catalog data hygiene
def shipped():
    return yaml.safe_load(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))


def test_the_shipped_catalog_has_no_threshold_literals_for_settings():
    text = DEFAULT_CATALOG_PATH.read_text(encoding="utf-8")
    for name in ("min_monthly_invocations", "min_average_concurrency", "min_active_duty_cycle_pct",
                 "headroom_pct", "min_coverage_pct", "max_age_days"):
        for match in re.finditer(rf"{name}\s*:\s*(\S+)", text):
            raise AssertionError(f"{name} must not be assigned a value in the catalog: {match.group(0)}")


def test_operators_include_every_operator_the_catalog_uses(catalog):
    used = {c.op for e in catalog.entries for c in all_conditions(e)}
    assert used <= set(OPERATORS)


# ------------------------------------------------------------------ the parser rejects bad definitions
def entry_data(data, entry_id):
    return next(e for e in data["alternatives"] if e["id"] == entry_id)


def rule_data(entry, rule_id):
    return next(r for r in entry["compatibility_rules"] if r.get("id") == rule_id)


def evidence_data(entry, key):
    return next(e for e in entry["evidence_requirements"] if e.get("key") == key)


def _drop(mapping, key):
    mapping.pop(key)


def _rds_without_compatible_rule(entry):
    entry["compatibility_rules"] = [r for r in entry["compatibility_rules"] if r["verdict"] != "compatible"]


def _duplicate_rule(entry):
    entry["compatibility_rules"].append(copy.deepcopy(entry["compatibility_rules"][0]))


def _unused_required_evidence(entry):
    entry["evidence_requirements"].append(
        {"key": "ec2.extra_flag", "type": "boolean", "origin": "attestation", "description": "unused"})


def _informational_in_rule(entry):
    rule_data(entry, "lambda-fargate-irreplaceable-behavior")["when"][0] = {
        "evidence": "lambda.event_source_mappings", "op": "is_present"}


BAD_DEFINITIONS = {
    "insufficient_data rule": (
        LAMBDA_FARGATE, lambda e: rule_data(e, "lambda-fargate-irreplaceable-behavior").update(verdict="insufficient_data"),
        "insufficient_data is never a rule"),
    "undeclared rule reference": (
        EC2_FARGATE, lambda e: rule_data(e, "ec2-fargate-unsupported-os").update(references=["nope"]),
        "references must list declared reference ids"),
    "documentation rule without reference": (
        EC2_FARGATE, lambda e: rule_data(e, "ec2-fargate-unsupported-os").update(references=[]),
        "needs at least one reference"),
    "undeclared evidence key": (
        EC2_FARGATE, lambda e: rule_data(e, "ec2-fargate-not-containerizable")["when"][0].update(evidence="workload.nope"),
        "is not a declared evidence key"),
    "informational evidence in a rule": (
        LAMBDA_FARGATE, _informational_in_rule, "informational evidence 'lambda.event_source_mappings' cannot be used"),
    "unknown settings reference": (
        LAMBDA_FARGATE,
        lambda e: rule_data(e, "lambda-fargate-thresholds-met")["when"][3]["parameter"].update(ref="lambda_to_fargate.min_nothing"),
        "unknown decision_setting reference"),
    "comparison without a value or parameter": (
        LAMBDA_FARGATE, lambda e: _drop(rule_data(e, "lambda-fargate-thresholds-met")["when"][3], "parameter"),
        "needs exactly one of value or parameter"),
    "fargate operator without headroom": (
        EC2_FARGATE, lambda e: _drop(rule_data(e, "ec2-fargate-fits-valid-sizing")["when"][3], "headroom"),
        "headroom must be a decision_setting reference"),
    "lambda limit without the invocation type": (
        EC2_LAMBDA, lambda e: _drop(rule_data(e, "ec2-lambda-exceeds-timeout")["when"][0], "invocation_type_evidence"),
        "invocation_type_evidence is required"),
    "enum literal outside the allowed values": (
        EC2_FARGATE, lambda e: rule_data(e, "ec2-fargate-unsupported-os")["when"][0].update(value="BSD"),
        "not in the allowed values"),
    "unknown condition key": (
        EC2_FARGATE, lambda e: rule_data(e, "ec2-fargate-not-containerizable")["when"][0].update(bogus=1),
        "unknown condition keys"),
    "collected evidence without a source path": (
        EC2_FARGATE, lambda e: _drop(evidence_data(e, "ec2.vcpus"), "source_path"), "a collected key needs a source_path"),
    "derived evidence without inputs": (
        EC2_FARGATE, lambda e: _drop(evidence_data(e, "ec2.peak_vcpus_used"), "derived_from"),
        "a derived key needs derived_from"),
    "derivation on a collected key": (
        EC2_FARGATE, lambda e: evidence_data(e, "ec2.vcpus").update(derivation="x"),
        "only a derived key may define derived_from or derivation"),
    "attestation expiry on a collected key": (
        EC2_FARGATE, lambda e: evidence_data(e, "ec2.vcpus").update(attestation_expiry=True),
        "only allowed for an attestation"),
    "metric coverage without a series": (
        EC2_FARGATE, lambda e: _drop(evidence_data(e, "ec2.cpu_max_pct"), "coverage_series"),
        "requires_metric_coverage needs a coverage_series"),
    "evidence depending on an AWS limit": (
        EC2_FARGATE,
        lambda e: evidence_data(e, "ec2.cpu_max_pct").update(parameters=[{"source": "aws_limit", "ref": "fargate.task_sizes"}]),
        "an evidence key may depend on settings only"),
    "invalid evidence type": (
        EC2_FARGATE, lambda e: evidence_data(e, "ec2.vcpus").update(type="float"), "type must be one of"),
    "enum without values": (
        EC2_FARGATE, lambda e: evidence_data(e, "ec2.target_os_family").update(values=[]),
        "an enum needs a non-empty list of values"),
    "values on a non-enum": (
        EC2_FARGATE, lambda e: evidence_data(e, "ec2.vcpus").update(values=["a"]), "only an enum may define values"),
    "undeclared evidence reference": (
        EC2_FARGATE, lambda e: evidence_data(e, "ec2.vcpus").update(references=["nope"]),
        "references must list declared reference ids"),
    "duplicate evidence key": (
        EC2_FARGATE, lambda e: e["evidence_requirements"].append(copy.deepcopy(evidence_data(e, "ec2.vcpus"))),
        "duplicate evidence key"),
    "duplicate rule id": (EC2_FARGATE, _duplicate_rule, "duplicate rule id"),
    "required evidence no rule uses": (EC2_FARGATE, _unused_required_evidence, "required evidence is not used by any rule"),
    "no compatible rule": (RDS, _rds_without_compatible_rule, "at least one compatible rule is required"),
    "malformed repeat_for": (
        NAT, lambda e: e["compatibility_rules"][0].update(repeat_for={"name": "service"}), "repeat_for needs a name"),
    "reference without https": (
        EC2_FARGATE, lambda e: e["references"][0].update(url="http://example.com"), "url must be an https URL"),
    "reference without a date": (
        EC2_FARGATE, lambda e: e["references"][0].update(verified_on="yesterday"), "verified_on must be an ISO date"),
}


@pytest.mark.parametrize("case", sorted(BAD_DEFINITIONS))
def test_invalid_typed_definition_is_rejected(case):
    entry_id, mutate, expected = BAD_DEFINITIONS[case]
    data = copy.deepcopy(shipped())
    mutate(entry_data(data, entry_id))
    with pytest.raises(CatalogValidationError) as exc:
        AlternativesCatalog.from_dict(data)
    assert expected in str(exc.value)


def test_shipped_data_is_valid_before_any_mutation():
    AlternativesCatalog.from_dict(copy.deepcopy(shipped()))
