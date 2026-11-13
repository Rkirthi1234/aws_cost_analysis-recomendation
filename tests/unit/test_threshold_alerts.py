"""Unit tests for agent/services/threshold_alerts.py (notification de-duplication).

No AWS calls and no network. The file store tests only use pytest's temporary directory.
"""
import json
from decimal import Decimal

import pytest
import yaml

from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.cost_thresholds import CostThresholdDetector, load_cost_threshold_settings
from agent.services.high_cost_identifier import HighCostServiceIdentifier
from agent.services.threshold_alerts import (
    InMemoryAlertStore,
    JsonFileAlertStore,
    ThresholdAlertDeduplicator,
    alert_fingerprint,
    build_alert_store,
    period_key,
)

S3 = "Amazon Simple Storage Service"
EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"


@pytest.fixture(scope="module")
def catalog():
    return AlternativesCatalog.load()


class FakeCE:
    def __init__(self, amounts):
        self.amounts = amounts

    def get_cost_and_usage(self, **kw):
        metric = kw["Metrics"][0]
        return {"ResultsByTime": [{"Groups": [
            {"Keys": [name], "Metrics": {metric: {"Amount": amount, "Unit": "USD"}}}
            for name, amount in self.amounts.items()
        ]}]}


def evaluated(catalog, amounts, start="2026-11-01", end="2026-11-12", threshold=0.01):
    config = yaml.safe_load(
        f"cost_thresholds:\n  enabled: true\n  currency: USD\n  services:\n    ec2: {threshold}\n    s3: {threshold}\n"
    )
    settings = load_cost_threshold_settings(config, catalog)
    identifier = HighCostServiceIdentifier(catalog=catalog, ce_client=FakeCE(amounts))
    return CostThresholdDetector(settings, catalog, identifier).detect(start, end)


CROSSING = {S3: "0.5", EC2_COMPUTE: "0.001"}


# ============================================================ keys and fingerprints
def test_period_key_inside_one_month_and_across_months():
    assert period_key("2026-11-01", "2026-11-12") == "2026-11"
    assert period_key("2026-11-01", "2026-12-01") == "2026-11"      # full month (end exclusive)
    assert period_key("2026-10-01", "2026-12-01") == "2026-10-01..2026-12-01"
    assert period_key("garbage", "2026-12-01") == "garbage..2026-12-01"


def test_fingerprint_is_stable_and_sensitive_to_every_component():
    base = ("s3", "2026-11", Decimal("0.01"), "USD", "AmortizedCost")
    fp = alert_fingerprint(*base)
    assert fp == alert_fingerprint(*base) and len(fp) == 32
    assert fp == alert_fingerprint("s3", "2026-11", Decimal("0.0100"), "USD", "AmortizedCost")  # same number
    variants = [
        ("ec2", "2026-11", Decimal("0.01"), "USD", "AmortizedCost"),
        ("s3", "2026-12", Decimal("0.01"), "USD", "AmortizedCost"),
        ("s3", "2026-11", Decimal("0.02"), "USD", "AmortizedCost"),
        ("s3", "2026-11", Decimal("0.01"), "EUR", "AmortizedCost"),
        ("s3", "2026-11", Decimal("0.01"), "USD", "UnblendedCost"),
    ]
    assert len({fp, *(alert_fingerprint(*v) for v in variants)}) == 6


# ============================================================ de-duplication behaviour
def test_first_alert_is_new_and_marking_suppresses_the_repeat(catalog):
    dedupe = ThresholdAlertDeduplicator()
    first = dedupe.annotate(evaluated(catalog, CROSSING), mark=True)
    assert first["notifications"]["new"] == ["s3"] and first["notifications"]["already_notified"] == []
    s3 = next(s for s in first["services"] if s["service_key"] == "s3")
    assert s3["notification"]["notify"] is True and s3["notification"]["already_notified"] is False
    assert first["notifications"]["marked_as_notified"] is True

    again = dedupe.annotate(evaluated(catalog, CROSSING), mark=True)
    assert again["notifications"]["new"] == [] and again["notifications"]["already_notified"] == ["s3"]
    s3 = next(s for s in again["services"] if s["service_key"] == "s3")
    assert s3["notification"]["notify"] is False and s3["crossed"] is True     # result itself is unchanged
    assert s3["notification"]["fingerprint"] == next(
        s for s in first["services"] if s["service_key"] == "s3")["notification"]["fingerprint"]


def test_without_mark_nothing_is_recorded(catalog):
    dedupe = ThresholdAlertDeduplicator()
    for _ in range(3):
        out = dedupe.annotate(evaluated(catalog, CROSSING))          # mark defaults to False
        assert out["notifications"]["new"] == ["s3"] and out["notifications"]["marked_as_notified"] is False


def test_growing_cost_in_the_same_month_is_not_renotified(catalog):
    dedupe = ThresholdAlertDeduplicator()
    dedupe.annotate(evaluated(catalog, {S3: "0.02"}, end="2026-11-05"), mark=True)
    later = dedupe.annotate(evaluated(catalog, {S3: "9.99"}, end="2026-11-20"), mark=True)
    assert later["notifications"]["new"] == [] and later["notifications"]["already_notified"] == ["s3"]


def test_new_month_changed_threshold_or_other_service_is_new(catalog):
    dedupe = ThresholdAlertDeduplicator()
    dedupe.annotate(evaluated(catalog, CROSSING), mark=True)
    next_month = dedupe.annotate(evaluated(catalog, CROSSING, start="2026-12-01", end="2026-12-10"), mark=True)
    assert next_month["notifications"]["new"] == ["s3"]
    new_threshold = dedupe.annotate(evaluated(catalog, CROSSING, threshold=0.02))
    assert new_threshold["notifications"]["new"] == ["s3"]
    other = dedupe.annotate(evaluated(catalog, {EC2_COMPUTE: "1"}))
    assert other["notifications"]["new"] == ["ec2"]


def test_only_crossed_services_get_notification_data(catalog):
    out = ThresholdAlertDeduplicator().annotate(evaluated(catalog, CROSSING))
    ec2 = next(s for s in out["services"] if s["service_key"] == "ec2")
    assert ec2["crossed"] is False and "notification" not in ec2


def test_annotate_does_not_mutate_the_input(catalog):
    result = evaluated(catalog, CROSSING)
    snapshot = json.dumps(result, sort_keys=True)
    out = ThresholdAlertDeduplicator().annotate(result, mark=True)
    assert json.dumps(result, sort_keys=True) == snapshot and out is not result
    json.dumps(out)


@pytest.mark.parametrize("result", [
    {"status": "disabled", "enabled": False},
    {"status": "invalid_configuration", "errors": ["x"]},
    {"error": True, "code": "AccessDeniedException"},
    None,
])
def test_results_that_are_not_evaluated_pass_through(result):
    assert ThresholdAlertDeduplicator().annotate(result, mark=True) is result


def test_each_deduplicator_has_its_own_in_memory_history(catalog):
    a, b = ThresholdAlertDeduplicator(), ThresholdAlertDeduplicator()
    a.annotate(evaluated(catalog, CROSSING), mark=True)
    assert b.annotate(evaluated(catalog, CROSSING))["notifications"]["new"] == ["s3"]
    assert a.store_kind == "in_memory"


# ============================================================ stores
def test_in_memory_store_basics():
    store = InMemoryAlertStore()
    assert not store.contains("x")
    store.add("x")
    assert store.contains("x") and not store.contains("y")


def test_build_alert_store_defaults_to_memory_and_files_only_when_asked(tmp_path):
    assert isinstance(build_alert_store(None), InMemoryAlertStore)
    assert isinstance(build_alert_store(""), InMemoryAlertStore)
    assert isinstance(build_alert_store(str(tmp_path / "alerts.json")), JsonFileAlertStore)


def test_building_a_file_store_creates_no_file_until_something_is_recorded(tmp_path):
    path = tmp_path / "alerts.json"
    store = JsonFileAlertStore(str(path))
    assert not path.exists() and not store.contains("x") and store.warnings == []


def test_file_store_persists_across_instances_and_across_dedupers(tmp_path, catalog):
    path = str(tmp_path / "alerts.json")
    ThresholdAlertDeduplicator(JsonFileAlertStore(path)).annotate(evaluated(catalog, CROSSING), mark=True)
    again = ThresholdAlertDeduplicator(JsonFileAlertStore(path)).annotate(evaluated(catalog, CROSSING))
    assert again["notifications"]["already_notified"] == ["s3"] and again["notifications"]["store"] == "local_file"


def test_file_store_writes_only_fingerprints_and_timestamps(tmp_path, catalog):
    path = tmp_path / "alerts.json"
    out = ThresholdAlertDeduplicator(JsonFileAlertStore(str(path))).annotate(evaluated(catalog, CROSSING), mark=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "entries"} and len(data["entries"]) == 1
    fingerprint = next(s for s in out["services"] if s["service_key"] == "s3")["notification"]["fingerprint"]
    assert list(data["entries"]) == [fingerprint]
    text = path.read_text(encoding="utf-8")
    assert "0.5" not in text and "s3" not in text.lower().replace(fingerprint, "")
    assert [p.name for p in tmp_path.iterdir()] == ["alerts.json"]          # no leftover temporary file


@pytest.mark.parametrize("content", ["{not json", "[]", '{"entries": 5}', ""])
def test_corrupt_state_file_is_treated_as_empty_with_a_warning(tmp_path, content):
    path = tmp_path / "alerts.json"
    path.write_text(content, encoding="utf-8")
    store = JsonFileAlertStore(str(path))
    assert not store.contains("anything") and store.warnings
    store.add("fp")                                                          # recovers: next write replaces it
    assert JsonFileAlertStore(str(path)).contains("fp")


def test_unwritable_location_keeps_state_in_memory_and_does_not_raise(tmp_path):
    store = JsonFileAlertStore(str(tmp_path / "missing-dir" / "alerts.json"))
    store.add("fp")
    assert store.contains("fp") and any("kept in memory only" in w for w in store.warnings)
    assert not (tmp_path / "missing-dir").exists()                           # directories are never created


def test_store_warnings_are_surfaced_in_the_notification_block(tmp_path, catalog):
    store = JsonFileAlertStore(str(tmp_path / "missing-dir" / "alerts.json"))
    out = ThresholdAlertDeduplicator(store).annotate(evaluated(catalog, CROSSING), mark=True)
    assert out["notifications"]["store_warnings"]


def test_file_store_prunes_the_oldest_entries(tmp_path):
    path = str(tmp_path / "alerts.json")
    store = JsonFileAlertStore(path, max_entries=3)
    for i in range(5):
        store.add(f"fp{i}")
    assert len(json.loads(open(path, encoding="utf-8").read())["entries"]) == 3
    assert store.contains("fp4") and store.contains("fp3") and len(store._entries) == 3


def test_file_store_rejects_bad_arguments(tmp_path):
    with pytest.raises(ValueError):
        JsonFileAlertStore("   ")
    with pytest.raises(ValueError):
        JsonFileAlertStore(str(tmp_path / "a.json"), max_entries=0)
