"""Phase 2 evidence collection: inventory + CloudWatch metrics + usage patterns.

Given an approved catalog entry, collects the descriptive evidence that entry
lists under ``required_evidence`` as far as AWS read APIs allow, and says
plainly what could not be collected. It does NOT evaluate compatibility,
assign a confidence level, recommend anything or produce a savings figure;
those belong to a later phase.

All AWS access goes through ``ReadOnlySessionManager`` (allowlisted
List/Describe/Get operations only).
"""
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from agent.services.alternatives_catalog import AlternativeEntry, AlternativesCatalog
from agent.services.inventory_collector import DEFAULT_MAX_RESOURCES, InventoryCollector
from agent.services.metrics_collector import MetricsCollector
from agent.services.readonly_guard import ReadOnlySessionManager
from agent.services.usage_patterns import MetricsWindow, PatternThresholds

logger = logging.getLogger(__name__)

PLAN_EC2_LAMBDA = "ec2_to_lambda"
PLAN_EC2_FARGATE = "ec2_to_fargate"
PLAN_LAMBDA = "lambda_to_fargate"
PLAN_RDS = "rds_to_aurora_sv2"
PLAN_NAT = "nat_to_gateway_endpoints"

# catalog entry id -> evidence plan
EVIDENCE_PLANS: Dict[str, str] = {
    "ec2-short-event-driven-to-lambda": PLAN_EC2_LAMBDA,
    "ec2-containerized-to-fargate": PLAN_EC2_FARGATE,
    "lambda-steady-high-volume-to-fargate": PLAN_LAMBDA,
    "rds-mysql-postgres-to-aurora-serverless-v2": PLAN_RDS,
    "nat-gateway-to-gateway-endpoints": PLAN_NAT,
}

# Evidence a plan cannot observe from AWS read APIs. Always reported.
STATIC_GAPS: Dict[str, List[str]] = {
    PLAN_EC2_LAMBDA: [
        "Memory utilization is not collected (it requires CloudWatch agent metrics).",
        "Whether the workload is event-driven, and its duration and payload per event, are application-level and "
        "cannot be derived from EC2 metrics.",
        "Load balancer, connection and local-state dependencies are not collected.",
    ],
    PLAN_EC2_FARGATE: [
        "Memory utilization is not collected (it requires CloudWatch agent metrics).",
        "Whether the workload is containerized is only inferred from ECS container-instance registration.",
        "Local state beyond instance store size and EBS volume count, and networking requirements, are not collected.",
    ],
    PLAN_LAMBDA: [
        "Triggers other than event source mappings (API Gateway, S3, EventBridge, function URLs) are not collected.",
    ],
    PLAN_RDS: [
        "Database extensions, parameter groups and application use of engine-specific features are not collected.",
    ],
    PLAN_NAT: [
        "The share of NAT traffic destined to S3 and DynamoDB needs VPC Flow Logs or CUR; it is not in CloudWatch.",
    ],
}

NOTE = (
    "Descriptive evidence only. No compatibility evaluation, confidence level, recommendation or "
    "savings figure has been produced."
)


class UsageEvidenceCollector:
    def __init__(
        self,
        session_manager: Any,
        catalog: AlternativesCatalog,
        account_registry: Any = None,
        *,
        window: Optional[MetricsWindow] = None,
        thresholds: Optional[PatternThresholds] = None,
        max_resources: int = DEFAULT_MAX_RESOURCES,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        ro = ReadOnlySessionManager(session_manager)
        self._catalog = catalog
        self._registry = account_registry
        self._inventory = InventoryCollector(ro, max_resources=max_resources)
        self._metrics = MetricsCollector(ro, window, thresholds, now_fn=now_fn)

    # ------------------------------------------------------------------ public
    def collect(
        self, alternative_id: str, account_id: str, regions: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        entry = next((e for e in self._catalog.approved_entries() if e.id == alternative_id), None)
        if entry is None:
            return {"error": True, "code": "UnknownAlternative",
                    "message": f"'{alternative_id}' is not an approved catalog entry"}
        plan = EVIDENCE_PLANS.get(alternative_id)
        if plan is None:
            return {"error": True, "code": "NoEvidencePlan",
                    "message": f"No evidence plan exists for '{alternative_id}'",
                    "supported": sorted(EVIDENCE_PLANS)}

        region_list = self._resolve_regions(account_id, regions)
        region_results = [self._collect_region(plan, account_id, r) for r in region_list]
        return self._assemble(entry, plan, account_id, region_results)

    # ---------------------------------------------------------------- internals
    def _resolve_regions(self, account_id: str, regions: Optional[List[str]]) -> List[Optional[str]]:
        if regions:
            return list(dict.fromkeys(regions))
        if self._registry is not None:
            try:
                return [self._registry.get_account(account_id).region]
            except KeyError:
                pass
        return [None]

    def _collect_region(self, plan: str, account_id: str, region: Optional[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"region": region}
        if plan in (PLAN_EC2_LAMBDA, PLAN_EC2_FARGATE):
            ecs = self._inventory.collect_ecs_usage(account_id, region, include_container_instances=True)
            out["ecs"] = _without_resources(ecs)
            if plan == PLAN_EC2_LAMBDA:
                out["lambda_presence"] = _without_resources(
                    self._inventory.collect_lambda_functions(account_id, region, presence_only=True)
                )
            inv = self._inventory.collect_ec2_instances(
                account_id, region, ecs_instance_ids=ecs.get("container_instance_ec2_ids", [])
            )
            out["inventory"] = inv
            running = [r for r in inv["resources"] if r["state"] == "running"]
            out["metrics_skipped_not_running"] = len(inv["resources"]) - len(running)
            out["metrics"] = self._metrics.collect(
                account_id, region, "ec2", [r["instance_id"] for r in running],
                created_at=_parse_times((r["instance_id"], r["launch_time"]) for r in running),
            )
        elif plan == PLAN_LAMBDA:
            inv = self._inventory.collect_lambda_functions(account_id, region)
            out["inventory"] = inv
            out["ecs"] = _without_resources(
                self._inventory.collect_ecs_usage(account_id, region, include_container_instances=False)
            )
            out["metrics"] = self._metrics.collect(
                account_id, region, "lambda", [r["function_name"] for r in inv["resources"]]
            )
        elif plan == PLAN_RDS:
            inv = self._inventory.collect_rds(account_id, region)
            out["inventory"] = inv
            sources = [r for r in inv["resources"] if r["is_mysql_or_postgres"]]
            out["metrics"] = self._metrics.collect(
                account_id, region, "rds", [r["db_instance_id"] for r in sources],
                created_at=_parse_times((r["db_instance_id"], r["created_time"]) for r in sources),
            )
        elif plan == PLAN_NAT:
            inv = self._inventory.collect_nat_gateways(account_id, region)
            out["inventory"] = inv
            out["metrics"] = self._metrics.collect(
                account_id, region, "nat_gateway", [r["nat_gateway_id"] for r in inv["resources"]],
                created_at=_parse_times((r["nat_gateway_id"], r["created_time"]) for r in inv["resources"]),
            )
        return out

    def _assemble(
        self, entry: AlternativeEntry, plan: str, account_id: str, region_results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        errors: List[Dict[str, Any]] = []
        for rr in region_results:
            for section, body in rr.items():
                if isinstance(body, dict):
                    for err in body.get("errors", []):
                        errors.append({"region": rr["region"], "section": section, **err})

        missing = sorted({e["missing_permission"] for e in errors if e.get("missing_permission")})
        total_resources = sum(
            rr.get("inventory", {}).get("count", 0) for rr in region_results
        )
        if missing:
            status = "partial_missing_permissions"
        elif errors:
            status = "partial_errors"
        elif total_resources == 0:
            status = "no_resources_found"
        else:
            status = "evidence_collected"

        return {
            "alternative_id": entry.id,
            "source": entry.source_name,
            "alternative": entry.alternative_name,
            "account_id": account_id,
            "status": status,
            "metrics_window": self._metrics.window.as_dict(),
            "thresholds_used": self._metrics_thresholds(region_results),
            "regions": region_results,
            "required_evidence": list(entry.required_evidence),
            "evidence_gaps": list(STATIC_GAPS[plan]),
            "missing_permissions": missing,
            "errors": errors,
            "note": NOTE,
        }

    @staticmethod
    def _metrics_thresholds(region_results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for rr in region_results:
            m = rr.get("metrics")
            if m:
                return m.get("thresholds_used")
        return None


def _without_resources(env: Dict[str, Any]) -> Dict[str, Any]:
    """Keep summaries and errors; drop bulky resource lists used only for presence checks."""
    slim = {k: v for k, v in env.items() if k != "resources"}
    return slim


def _parse_times(pairs) -> Dict[str, datetime]:
    out: Dict[str, datetime] = {}
    for rid, value in pairs:
        if not value:
            continue
        try:
            ts = datetime.fromisoformat(value) if isinstance(value, str) else value
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out[rid] = ts
    return out
