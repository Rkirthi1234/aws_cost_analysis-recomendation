"""Phase 2 tests: read-only guard (no AWS calls)."""
import boto3
import pytest
from botocore.stub import Stubber

from agent.services.readonly_guard import (
    ALLOWED_OPERATIONS,
    IAM_ACTIONS,
    ReadOnlyClient,
    ReadOnlySessionManager,
    ReadOnlyViolation,
    required_iam_actions,
)


def make_client(service):
    return boto3.client(
        service, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )


@pytest.mark.parametrize(
    "service,op",
    [
        ("ec2", "run_instances"),
        ("ec2", "terminate_instances"),
        ("ec2", "stop_instances"),
        ("ec2", "create_nat_gateway"),
        ("lambda", "invoke"),
        ("lambda", "create_function"),
        ("lambda", "update_function_configuration"),
        ("ecs", "run_task"),
        ("ecs", "create_service"),
        ("rds", "stop_db_instance"),
        ("rds", "modify_db_instance"),
        ("cloudwatch", "put_metric_data"),
        ("cloudwatch", "put_metric_alarm"),
        ("cloudwatch", "delete_alarms"),
    ],
)
def test_write_operations_are_blocked(service, op):
    client = ReadOnlyClient(make_client(service), service)
    with pytest.raises(ReadOnlyViolation):
        getattr(client, op)


@pytest.mark.parametrize("name", ["get_paginator", "meta", "_make_api_call", "_client", "exceptions", "close"])
def test_non_allowlisted_and_private_attributes_are_blocked(name):
    client = ReadOnlyClient(make_client("ec2"), "ec2")
    with pytest.raises(ReadOnlyViolation):
        getattr(client, name)


def test_allowlisted_read_operations_work_and_hit_the_real_client():
    raw = make_client("ec2")
    with Stubber(raw) as stub:
        stub.add_response("describe_instances", {"Reservations": []})
        client = ReadOnlyClient(raw, "ec2")
        assert client.describe_instances() == {"Reservations": []}
        stub.assert_no_pending_responses()


def test_every_allowlisted_operation_is_a_read_verb_and_exists():
    for service, ops in ALLOWED_OPERATIONS.items():
        raw = make_client(service)
        for op in ops:
            assert op.startswith(("describe_", "list_", "get_")), op
            assert hasattr(raw, op), f"{service}.{op} does not exist in boto3"


def test_iam_action_map_matches_allowlist_exactly():
    assert {s: set(v) for s, v in IAM_ACTIONS.items()} == {s: set(v) for s, v in ALLOWED_OPERATIONS.items()}


def test_every_iam_action_is_read_only():
    for actions in required_iam_actions().values():
        for action in actions:
            verb = action.split(":")[1]
            assert verb.startswith(("Describe", "List", "Get")), action


def test_client_is_immutable():
    client = ReadOnlyClient(make_client("ec2"), "ec2")
    with pytest.raises(ReadOnlyViolation):
        client.describe_instances = lambda **kw: {}


def test_disallowed_service_is_rejected():
    with pytest.raises(ReadOnlyViolation):
        ReadOnlyClient(make_client("s3"), "s3")


class FakeSessionManager:
    def __init__(self):
        self.requested = []

    def get_client(self, account_id, service_name, region=None):
        self.requested.append((account_id, service_name, region))
        return make_client(service_name)


def test_session_manager_wrapper_returns_read_only_clients_only():
    sm = FakeSessionManager()
    view = ReadOnlySessionManager(sm)
    client = view.get_client("111122223333", "ec2", "us-east-1")
    assert isinstance(client, ReadOnlyClient)
    with pytest.raises(ReadOnlyViolation):
        client.terminate_instances
    assert sm.requested == [("111122223333", "ec2", "us-east-1")]


def test_session_manager_wrapper_refuses_other_services_without_calling_through():
    sm = FakeSessionManager()
    with pytest.raises(ReadOnlyViolation):
        ReadOnlySessionManager(sm).get_client("111122223333", "iam", "us-east-1")
    assert sm.requested == []


# ------------------------------------------------------------ Cost Explorer (R-4)
@pytest.mark.parametrize(
    "op",
    [
        "create_cost_category_definition",
        "update_cost_category_definition",
        "delete_cost_category_definition",
        "create_anomaly_monitor",
        "update_anomaly_monitor",
        "delete_anomaly_monitor",
        "create_anomaly_subscription",
        "get_cost_forecast",       # read-only, but not used by this feature, so not allowlisted
        "get_dimension_values",
    ],
)
def test_cost_explorer_only_allows_get_cost_and_usage(op):
    client = ReadOnlyClient(make_client("ce"), "ce")
    with pytest.raises(ReadOnlyViolation):
        getattr(client, op)


def test_cost_explorer_get_cost_and_usage_hits_the_real_client():
    raw = make_client("ce")
    with Stubber(raw) as stub:
        stub.add_response("get_cost_and_usage", {"ResultsByTime": []})
        client = ReadOnlyClient(raw, "ce")
        assert client.get_cost_and_usage(
            TimePeriod={"Start": "2026-10-01", "End": "2026-11-01"}, Granularity="MONTHLY", Metrics=["AmortizedCost"]
        ) == {"ResultsByTime": []}
        stub.assert_no_pending_responses()


def test_cost_explorer_allowlist_and_iam_action_are_registered():
    assert ALLOWED_OPERATIONS["ce"] == frozenset({"get_cost_and_usage"})
    assert IAM_ACTIONS["ce"] == {"get_cost_and_usage": "ce:GetCostAndUsage"}
    assert required_iam_actions()["ce"] == ["ce:GetCostAndUsage"]


def test_session_manager_wrapper_returns_read_only_cost_explorer_clients():
    sm = FakeSessionManager()
    client = ReadOnlySessionManager(sm).get_client("111122223333", "ce")
    assert isinstance(client, ReadOnlyClient)
    with pytest.raises(ReadOnlyViolation):
        client.create_anomaly_monitor
    assert sm.requested == [("111122223333", "ce", None)]
