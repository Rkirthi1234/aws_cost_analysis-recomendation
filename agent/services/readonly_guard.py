"""Read-only enforcement for Phase 2 evidence collection.

Every AWS client used by the Phase 2 collectors is wrapped in ``ReadOnlyClient``.
Only the exact API operations listed in ``ALLOWED_OPERATIONS`` can be called; any
other attribute access (a write call, ``get_paginator``, ``meta`` ...) raises
``ReadOnlyViolation`` before a request can be built. This is defence in depth on
top of IAM: even a role that was granted too much cannot be used to create,
modify, stop or migrate a resource through this code.
"""
from typing import Any, Dict, FrozenSet

# service name -> exact boto3 method names that may be called. All are read-only
# List*/Describe*/Get* operations.
ALLOWED_OPERATIONS: Dict[str, FrozenSet[str]] = {
    "ec2": frozenset({
        "describe_instances",
        "describe_instance_types",
        "describe_nat_gateways",
        "describe_vpc_endpoints",
        "describe_route_tables",
    }),
    "lambda": frozenset({"list_functions", "list_event_source_mappings"}),
    "ecs": frozenset({
        "list_clusters",
        "describe_clusters",
        "list_container_instances",
        "describe_container_instances",
    }),
    "rds": frozenset({"describe_db_instances", "describe_db_clusters",
                      "describe_db_engine_versions", "describe_orderable_db_instance_options"}),
    "cloudwatch": frozenset({"get_metric_data"}),
    # Cost Explorer: the Phase 1 high-cost screen only ever calls get_cost_and_usage.
    "ce": frozenset({"get_cost_and_usage"}),
    "pricing": frozenset({"get_products"}),
}

# boto3 method -> IAM action, used for error hints and the permission report.
IAM_ACTIONS: Dict[str, Dict[str, str]] = {
    "ec2": {
        "describe_instances": "ec2:DescribeInstances",
        "describe_instance_types": "ec2:DescribeInstanceTypes",
        "describe_nat_gateways": "ec2:DescribeNatGateways",
        "describe_vpc_endpoints": "ec2:DescribeVpcEndpoints",
        "describe_route_tables": "ec2:DescribeRouteTables",
    },
    "lambda": {
        "list_functions": "lambda:ListFunctions",
        "list_event_source_mappings": "lambda:ListEventSourceMappings",
    },
    "ecs": {
        "list_clusters": "ecs:ListClusters",
        "describe_clusters": "ecs:DescribeClusters",
        "list_container_instances": "ecs:ListContainerInstances",
        "describe_container_instances": "ecs:DescribeContainerInstances",
    },
    "rds": {
        "describe_db_instances": "rds:DescribeDBInstances",
        "describe_db_clusters": "rds:DescribeDBClusters",
        "describe_db_engine_versions": "rds:DescribeDBEngineVersions",
        "describe_orderable_db_instance_options": "rds:DescribeOrderableDBInstanceOptions",
    },
    "cloudwatch": {"get_metric_data": "cloudwatch:GetMetricData"},
    "ce": {"get_cost_and_usage": "ce:GetCostAndUsage"},
    "pricing": {"get_products": "pricing:GetProducts"},
}


def required_iam_actions() -> Dict[str, list]:
    """Every IAM action Phase 2 can use, grouped by service (for the permission report)."""
    return {svc: sorted(actions.values()) for svc, actions in IAM_ACTIONS.items()}


class ReadOnlyViolation(PermissionError):
    """Raised when code attempts an operation outside the read-only allowlist."""


class ReadOnlyClient:
    """Proxy that exposes only allowlisted read operations of a boto3 client."""

    __slots__ = ("_client", "_service", "_allowed")

    def __init__(self, client: Any, service: str):
        if service not in ALLOWED_OPERATIONS:
            raise ReadOnlyViolation(f"Service '{service}' is not allowed for evidence collection")
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_service", service)
        object.__setattr__(self, "_allowed", ALLOWED_OPERATIONS[service])

    def __getattribute__(self, name: str) -> Any:
        # __getattribute__ (not __getattr__) so that the slot attributes below,
        # including ``_client``, cannot be reached through normal attribute lookup.
        allowed = object.__getattribute__(self, "_allowed")
        if name not in allowed:
            service = object.__getattribute__(self, "_service")
            raise ReadOnlyViolation(
                f"'{service}.{name}' is not an allowed read-only operation"
            )
        return getattr(object.__getattribute__(self, "_client"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise ReadOnlyViolation("ReadOnlyClient is immutable")


class ReadOnlySessionManager:
    """Wraps a SessionManager so every client it hands out is read-only."""

    def __init__(self, session_manager: Any):
        self._session_manager = session_manager

    def get_client(self, account_id: str, service_name: str, region: str = None) -> ReadOnlyClient:
        if service_name not in ALLOWED_OPERATIONS:
            raise ReadOnlyViolation(f"Service '{service_name}' is not allowed for evidence collection")
        client = self._session_manager.get_client(account_id, service_name, region)
        return ReadOnlyClient(client, service_name)
