"""Phase 1 tests: service-name mapping, catalog loading and catalog validation."""
import copy

import pytest
import yaml

from agent.services.alternatives_catalog import (
    DEFAULT_CATALOG_PATH,
    AlternativesCatalog,
    CatalogValidationError,
)

EXPECTED_IDS = {
    "nat-gateway-to-gateway-endpoints",
    "rds-mysql-postgres-to-aurora-serverless-v2",
    "lambda-steady-high-volume-to-fargate",
    "ec2-short-event-driven-to-lambda",
    "ec2-containerized-to-fargate",
}
EC2_LAMBDA_ID = "ec2-short-event-driven-to-lambda"
EC2_FARGATE_ID = "ec2-containerized-to-fargate"


def valid_data():
    return yaml.safe_load(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def catalog():
    return AlternativesCatalog.load()


# --------------------------------------------------------- shipped catalog
def test_shipped_catalog_loads_and_validates(catalog):
    assert {e.id for e in catalog.entries} == EXPECTED_IDS
    assert all(e.approved for e in catalog.entries)


def test_ec2_cross_service_alternatives_are_present(catalog):
    # Phase 2: EC2 -> Lambda and EC2 -> Fargate were added (cross-service only).
    assert {e.id for e in catalog.get_alternatives_for("ec2")} == {
        "nat-gateway-to-gateway-endpoints",
        EC2_LAMBDA_ID,
        EC2_FARGATE_ID,
    }
    by_id = {e.id: e for e in catalog.entries}
    assert by_id[EC2_LAMBDA_ID].alternative_services == ("lambda",)
    assert by_id[EC2_FARGATE_ID].alternative_services == ("ecs",)
    for entry_id in (EC2_LAMBDA_ID, EC2_FARGATE_ID):
        entry = by_id[entry_id]
        assert entry.alternative_kind == "service"
        assert entry.scope.kind == "usage_type_contains"
        assert set(entry.scope.values) == {"BoxUsage", "SpotUsage"}
        # Every note is backed by an AWS documentation page.
        assert all(n.reference.startswith("https://docs.aws.amazon.com/") for n in entry.notes)
        # Workload evidence must be required before any recommendation.
        assert any("event-driven" in e or "containerized" in e for e in entry.required_evidence)


def test_ec2_entries_do_not_introduce_graviton_or_spot_alternatives(catalog):
    for entry in catalog.entries:
        assert "graviton" not in entry.alternative_name.casefold()
        assert "spot" not in entry.alternative_name.casefold()


def test_rules_never_copy_a_threshold_value_into_the_catalog(catalog):
    # Step 38.3: rules point to settings / AWS limits by reference. The only literal number a comparison
    # may carry is 0 ("no traffic"); every other threshold must be a parameter reference.
    for entry in catalog.entries:
        for rule in entry.decision_rules:
            for cond in rule.conditions:
                if cond.op in ("gt", "gte", "lt", "lte"):
                    assert cond.parameter is not None or cond.value == 0, (entry.id, rule.id)


def test_every_note_has_https_reference_and_every_entry_lists_evidence(catalog):
    for entry in catalog.entries:
        assert entry.notes, entry.id
        assert entry.required_evidence, entry.id
        assert all(n.reference.startswith("https://") for n in entry.notes)


def test_shipped_catalog_has_rules_for_every_entry_and_no_warnings(catalog):
    assert catalog.warnings == []
    assert all(e.rules and e.decision_rules for e in catalog.entries)


def test_empty_rules_are_still_reported_not_silently_accepted():
    data = valid_data()
    entry = data["alternatives"][0]
    for key in ("compatibility_rules", "evidence_requirements", "references"):
        entry.pop(key, None)
    cat = AlternativesCatalog.from_dict(data)
    assert len(cat.warnings) == 1 and "compatibility_rules is empty" in cat.warnings[0]


# ------------------------------------------------------- service-name mapping
@pytest.mark.parametrize(
    "name,expected",
    [
        ("EC2 - Other", "ec2"),
        ("ec2 - other", "ec2"),
        ("Amazon Elastic Compute Cloud - Compute", "ec2"),
        ("  AWS   Lambda ", "lambda"),
        ("Amazon Relational Database Service", "rds"),
        ("Amazon Elastic Container Service", "ecs"),
        ("Amazon Virtual Private Cloud", "vpc"),
        ("Amazon Simple Storage Service", "s3"),
        ("Amazon DynamoDB", "dynamodb"),
        ("lambda", "lambda"),
    ],
)
def test_resolve_service_maps_known_names(catalog, name, expected):
    assert catalog.resolve_service(name) == expected


@pytest.mark.parametrize("name", ["Amazon Kinesis", "", None, 42])
def test_resolve_service_unknown_returns_none(catalog, name):
    assert catalog.resolve_service(name) is None


def test_pricing_codes_and_ce_names(catalog):
    assert catalog.pricing_code_for("rds") == "AmazonRDS"
    assert catalog.pricing_code_for("lambda") == "AWSLambda"
    assert catalog.pricing_code_for("nope") is None
    assert "EC2 - Other" in catalog.ce_names_for("ec2")
    assert catalog.ce_names_for("nope") == ()


def test_has_catalog_entry(catalog):
    assert catalog.has_catalog_entry("lambda") is True
    assert catalog.has_catalog_entry("s3") is False
    assert catalog.has_catalog_entry(None) is False


# -------------------------------------------------------- pending entries
def test_pending_entries_are_loaded_but_never_returned():
    data = valid_data()
    data["alternatives"][0]["status"] = "pending_approval"
    cat = AlternativesCatalog.from_dict(data)
    assert len(cat.entries) == 5
    assert "nat-gateway-to-gateway-endpoints" not in {e.id for e in cat.approved_entries()}
    assert cat.get_alternatives_for("vpc") == []


# ------------------------------------------------------ validation failures
def _mutate(fn):
    data = copy.deepcopy(valid_data())
    fn(data)
    return data


NEGATIVE_CASES = {
    "bad schema version": (lambda d: d.update(schema_version=2), "schema_version"),
    "no services": (lambda d: d.pop("services"), "'services' must be"),
    "no alternatives": (lambda d: d.update(alternatives=[]), "'alternatives' must be"),
    "unknown source service": (
        lambda d: d["alternatives"][0]["source"].update(services=["nope"]),
        "unknown service key 'nope'",
    ),
    "unknown alternative service": (
        lambda d: d["alternatives"][0]["alternative"].update(services=["nope"]),
        "unknown service key 'nope'",
    ),
    "duplicate entry id": (
        lambda d: d["alternatives"][1].update(id=d["alternatives"][0]["id"]),
        "duplicate id",
    ),
    "bad entry id": (lambda d: d["alternatives"][0].update(id="Bad ID"), "id must match"),
    "duplicate ce name across services": (
        lambda d: d["services"]["vpc"]["ce_names"].append("AWS Lambda"),
        "already mapped to service 'vpc'",
    ),
    "missing pricing code": (lambda d: d["services"]["rds"].pop("pricing_service_code"), "pricing_service_code"),
    "empty ce_names": (lambda d: d["services"]["rds"].update(ce_names=[]), "ce_names must be"),
    "bad status": (lambda d: d["alternatives"][0].update(status="maybe"), "status must be"),
    "bad scope kind": (
        lambda d: d["alternatives"][0]["source"]["scope"].update(kind="everything"),
        "kind must be one of",
    ),
    "usage scope without values": (
        lambda d: d["alternatives"][0]["source"]["scope"].update(values=[]),
        "requires a non-empty list of values",
    ),
    "whole_service with values": (
        lambda d: d["alternatives"][2]["source"]["scope"].update(values=["x"]),
        "must not define values",
    ),
    "note without reference": (
        lambda d: d["alternatives"][0]["compatibility_notes"][0].pop("reference"),
        "reference must be an https URL",
    ),
    "note with http reference": (
        lambda d: d["alternatives"][0]["compatibility_notes"][0].update(reference="http://example.com"),
        "reference must be an https URL",
    ),
    "alternative equals source": (
        lambda d: d["alternatives"][0]["alternative"].update(name="nat gateway"),
        "alternative must differ from the source",
    ),
    "missing alternative name": (
        lambda d: d["alternatives"][0]["alternative"].pop("name"),
        "name is required",
    ),
    "bad alternative kind": (
        lambda d: d["alternatives"][0]["alternative"].update(kind="magic"),
        "kind must be one of",
    ),
    "rules not a list": (
        lambda d: d["alternatives"][0].update(compatibility_rules="x"),
        "compatibility_rules must be a list",
    ),
    "evidence not a list": (
        lambda d: d["alternatives"][0].update(required_evidence="x"),
        "required_evidence must be a list",
    ),
}


@pytest.mark.parametrize("case", sorted(NEGATIVE_CASES))
def test_invalid_catalog_is_rejected(case):
    mutator, expected = NEGATIVE_CASES[case]
    with pytest.raises(CatalogValidationError) as exc:
        AlternativesCatalog.from_dict(_mutate(mutator))
    assert expected in str(exc.value)


def test_all_problems_are_reported_together():
    def two_problems(d):
        d["alternatives"][0]["status"] = "maybe"
        d["alternatives"][1]["source"]["services"] = ["nope"]

    with pytest.raises(CatalogValidationError) as exc:
        AlternativesCatalog.from_dict(_mutate(two_problems))
    assert len(exc.value.errors) == 2
    assert any("status must be" in e for e in exc.value.errors)
    assert any("unknown service key" in e for e in exc.value.errors)


def test_root_must_be_mapping():
    with pytest.raises(CatalogValidationError):
        AlternativesCatalog.from_dict(["not", "a", "mapping"])


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        AlternativesCatalog.load(tmp_path / "missing.yaml")


def test_malformed_yaml_raises_validation_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("services: [unclosed", encoding="utf-8")
    with pytest.raises(CatalogValidationError) as exc:
        AlternativesCatalog.load(bad)
    assert "YAML parse error" in str(exc.value)
