"""Phase 2 tests: evidence orchestration over the real catalog (Stubber-validated AWS shapes)."""
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import Stubber

from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.usage_evidence import EVIDENCE_PLANS, UsageEvidenceCollector

NOW = datetime(2026, 11, 11, 12, 30, tzinfo=timezone.utc)
END = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
ACCT = "111122223333"
LAUNCH = datetime(2026, 6, 1, tzinfo=timezone.utc)
FORBIDDEN_KEYS = {"confidence", "recommendation", "recommended", "savings", "estimated_savings", "monthly_savings"}


class SM:
    def __init__(self, *services):
        self.clients, self.stubs = {}, {}
        for s in services:
            c = boto3.client(s, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
            self.clients[s], self.stubs[s] = c, Stubber(c)
            self.stubs[s].activate()

    def get_client(self, account_id, service_name, region=None):
        return self.clients[service_name]

    def done(self):
        for st in self.stubs.values():
            st.assert_no_pending_responses()


@pytest.fixture(scope="module")
def catalog():
    return AlternativesCatalog.load()


def collector(sm, catalog):
    return UsageEvidenceCollector(sm, catalog, now_fn=lambda: NOW)


def inst(iid, state="running", itype="m5.large"):
    return {"InstanceId": iid, "InstanceType": itype, "State": {"Name": state, "Code": 16},
            "PlatformDetails": "Linux/UNIX", "Architecture": "x86_64", "LaunchTime": LAUNCH,
            "Placement": {"AvailabilityZone": "us-east-1a", "Tenancy": "default"},
            "BlockDeviceMappings": [], "NetworkInterfaces": [], "Tags": []}


M5 = {"InstanceType": "m5.large", "VCpuInfo": {"DefaultVCpus": 2}, "MemoryInfo": {"SizeInMiB": 8192},
      "BareMetal": False, "BurstablePerformanceSupported": False}


def steady_points(days=30):
    n = days * 24
    return [(END - timedelta(hours=n - i), 40.0 + (i % 3)) for i in range(n)]


def cw_ok(sm, count=1):
    pts = steady_points()
    sm.stubs["cloudwatch"].add_response("get_metric_data", {"MetricDataResults": [
        {"Id": f"m{i}", "Label": "x", "StatusCode": "Complete",
         "Timestamps": [t for t, _ in pts], "Values": [v for _, v in pts]} for i in range(4 * count)]})


def keys_recursive(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from keys_recursive(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from keys_recursive(v)


# ---------------------------------------------------------------- plan coverage
def test_every_approved_catalog_entry_has_an_evidence_plan(catalog):
    assert {e.id for e in catalog.approved_entries()} == set(EVIDENCE_PLANS)


def test_unknown_alternative_returns_error(catalog):
    out = collector(SM(), catalog).collect("nope", ACCT, ["us-east-1"])
    assert out["error"] and out["code"] == "UnknownAlternative"


def test_no_graviton_or_spot_alternative_in_catalog(catalog):
    names = " ".join(e.alternative_name.lower() for e in catalog.approved_entries())
    assert "graviton" not in names and "spot" not in names


# ------------------------------------------------------------ EC2 -> Fargate
def test_ec2_to_fargate_collects_inventory_metrics_and_only_for_running_instances(catalog):
    sm = SM("ec2", "ecs", "lambda", "cloudwatch")
    sm.stubs["ecs"].add_response("list_clusters", {"clusterArns": []})
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [
        {"Instances": [inst("i-run"), inst("i-stopped", state="stopped")]}]})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    cw_ok(sm, count=1)   # metrics only for the ONE running instance

    out = collector(sm, catalog).collect("ec2-containerized-to-fargate", ACCT, ["us-east-1"])
    sm.done()

    assert out["status"] == "evidence_collected"
    r = out["regions"][0]
    assert r["inventory"]["count"] == 2
    assert r["metrics_skipped_not_running"] == 1
    assert list(r["metrics"]["resources"]) == ["i-run"]
    assert r["metrics"]["resources"]["i-run"]["usage_pattern"]["pattern"] == "steady"
    assert out["metrics_window"] == {"preferred_days": 30, "minimum_days": 14}
    assert out["thresholds_used"]["steady_max_cv"] == 0.5
    assert out["evidence_gaps"] and out["required_evidence"]
    assert "memory utilization" in " ".join(out["evidence_gaps"]).lower()


def test_ecs_container_instances_are_flagged(catalog):
    sm = SM("ec2", "ecs", "lambda", "cloudwatch")
    arn = "arn:aws:ecs:us-east-1:111122223333:cluster/c"
    sm.stubs["ecs"].add_response("list_clusters", {"clusterArns": [arn]})
    sm.stubs["ecs"].add_response("describe_clusters", {"clusters": [{
        "clusterName": "c", "clusterArn": arn, "status": "ACTIVE", "registeredContainerInstancesCount": 1,
        "runningTasksCount": 2, "activeServicesCount": 1, "statistics": []}]})
    sm.stubs["ecs"].add_response("list_container_instances", {"containerInstanceArns": ["ci-1"]})
    sm.stubs["ecs"].add_response("describe_container_instances", {"containerInstances": [{"ec2InstanceId": "i-run"}]})
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-run")]}]})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    cw_ok(sm)
    out = collector(sm, catalog).collect("ec2-containerized-to-fargate", ACCT, ["us-east-1"])
    sm.done()
    res = out["regions"][0]["inventory"]["resources"][0]
    assert res["is_ecs_container_instance"] is True
    assert out["regions"][0]["inventory"]["summary"]["ecs_container_instances"] == 1


# --------------------------------------------------------------- EC2 -> Lambda
def test_ec2_to_lambda_also_checks_lambda_presence(catalog):
    sm = SM("ec2", "ecs", "lambda", "cloudwatch")
    sm.stubs["ecs"].add_response("list_clusters", {"clusterArns": []})
    sm.stubs["lambda"].add_response("list_functions", {"Functions": []})
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-run")]}]})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    cw_ok(sm)
    out = collector(sm, catalog).collect("ec2-short-event-driven-to-lambda", ACCT, ["us-east-1"])
    sm.done()
    assert out["regions"][0]["lambda_presence"]["present"] is False
    assert "event-driven" in " ".join(out["evidence_gaps"]).lower()


# -------------------------------------------------------- permissions / failures
def test_missing_permissions_are_reported_not_raised(catalog):
    sm = SM("ec2", "ecs", "lambda", "cloudwatch")
    sm.stubs["ecs"].add_client_error("list_clusters", service_error_code="AccessDeniedException",
                                     service_message="no", http_status_code=400)
    sm.stubs["ec2"].add_client_error("describe_instances", service_error_code="UnauthorizedOperation",
                                     service_message="no", http_status_code=403)
    out = collector(sm, catalog).collect("ec2-containerized-to-fargate", ACCT, ["us-east-1"])
    assert out["status"] == "partial_missing_permissions"
    assert out["missing_permissions"] == ["ec2:DescribeInstances", "ecs:ListClusters"]


def test_empty_account_reports_no_resources_found(catalog):
    sm = SM("ec2", "ecs", "lambda", "cloudwatch")
    sm.stubs["ecs"].add_response("list_clusters", {"clusterArns": []})
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": []})
    out = collector(sm, catalog).collect("ec2-containerized-to-fargate", ACCT, ["us-east-1"])
    assert out["status"] == "no_resources_found"


# ------------------------------------------------------------------- other plans
def test_nat_plan_reports_flow_log_gap(catalog):
    sm = SM("ec2", "cloudwatch")
    sm.stubs["ec2"].add_response("describe_nat_gateways", {"NatGateways": []})
    sm.stubs["ec2"].add_response("describe_vpc_endpoints", {"VpcEndpoints": []})
    out = collector(sm, catalog).collect("nat-gateway-to-gateway-endpoints", ACCT, ["us-east-1"])
    assert any("Flow Logs" in g for g in out["evidence_gaps"])


# --------------------------------------------------- Phase 2 boundary guarantees
def test_output_is_json_serializable_and_has_no_recommendation_confidence_or_savings(catalog):
    sm = SM("ec2", "ecs", "lambda", "cloudwatch")
    sm.stubs["ecs"].add_response("list_clusters", {"clusterArns": []})
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-run")]}]})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    cw_ok(sm)
    out = collector(sm, catalog).collect("ec2-containerized-to-fargate", ACCT, ["us-east-1"])
    json.dumps(out)
    assert not (set(keys_recursive(out)) & FORBIDDEN_KEYS)
    assert "No compatibility evaluation" in out["note"]


def test_collection_never_calls_any_write_operation(catalog):
    """Every stubbed client records its calls; all operations must be List/Describe/Get."""
    sm = SM("ec2", "ecs", "lambda", "cloudwatch")
    seen = []
    for c in sm.clients.values():
        c.meta.events.register("before-parameter-build.*.*", lambda model, **kw: seen.append(model.name))
    sm.stubs["ecs"].add_response("list_clusters", {"clusterArns": []})
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-run")]}]})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    cw_ok(sm)
    collector(sm, catalog).collect("ec2-containerized-to-fargate", ACCT, ["us-east-1"])
    assert seen and all(n.startswith(("List", "Describe", "Get")) for n in seen), seen
