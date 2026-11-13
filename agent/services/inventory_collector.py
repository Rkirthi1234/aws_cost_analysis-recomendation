"""Inventory collection for the alternatives feature (read-only).

Collects *what exists* in an account/region so later phases can judge
compatibility. It performs List/Describe calls only, through the allowlisted
read-only clients in ``readonly_guard``. It never creates, modifies, stops or
migrates anything.

Every collector returns a JSON-serializable dict and never raises for AWS
failures: problems are reported in ``errors`` (with the missing IAM action when
the cause is access denied) so a role that has not been granted the Phase 2
permissions yet degrades gracefully instead of crashing the agent.
"""
import logging
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from botocore.exceptions import ClientError

from agent.services.readonly_guard import IAM_ACTIONS, ReadOnlyViolation

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESOURCES = 200
MAX_PAGES = 50
EC2_ACTIVE_STATES = ["pending", "running", "stopping", "stopped"]
_DENIED_CODES = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")
ASG_TAG = "aws:autoscaling:groupName"
SOURCE_RDS_ENGINES = {"mysql", "postgres"}


class InventoryCollector:
    """Read-only inventory of EC2, Lambda, ECS/Fargate, RDS/Aurora and NAT resources."""

    def __init__(
        self,
        session_manager: Any,
        *,
        max_resources: int = DEFAULT_MAX_RESOURCES,
        max_pages: int = MAX_PAGES,
    ):
        if max_resources < 1:
            raise ValueError("max_resources must be >= 1")
        self._sm = session_manager
        self._max_resources = int(max_resources)
        self._max_pages = int(max_pages)

    # ---------------------------------------------------------------------- EC2
    def collect_ec2_instances(
        self,
        account_id: str,
        region: Optional[str] = None,
        *,
        ecs_instance_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        env = self._envelope("ec2_instance", account_id, region)
        ec2 = self._client(account_id, "ec2", region, env)
        if ec2 is None:
            return env

        items = self._paginate(
            env, "ec2", "describe_instances", ec2.describe_instances,
            {"Filters": [{"Name": "instance-state-name", "Values": EC2_ACTIVE_STATES}], "MaxResults": 500},
            token_in="NextToken", token_out="NextToken",
            extract=lambda r: [i for res in r.get("Reservations", []) for i in res.get("Instances", [])],
        )
        items.sort(key=lambda i: i.get("InstanceId", ""))
        items = self._cap(env, items)

        types = sorted({i.get("InstanceType") for i in items if i.get("InstanceType")})
        specs = self._instance_type_specs(ec2, types, env)
        ecs_ids = set(ecs_instance_ids or [])
        env["resources"] = [self._norm_instance(i, specs.get(i.get("InstanceType")), ecs_ids) for i in items]
        env["count"] = len(env["resources"])
        env["summary"] = {
            "by_state": dict(Counter(r["state"] for r in env["resources"])),
            "by_platform": dict(Counter(r["platform"] for r in env["resources"])),
            "by_instance_type": dict(Counter(r["instance_type"] for r in env["resources"])),
            "in_autoscaling_group": sum(1 for r in env["resources"] if r["autoscaling_group"]),
            "ecs_container_instances": sum(1 for r in env["resources"] if r["is_ecs_container_instance"]),
            "instance_type_specs_available": bool(specs),
        }
        return env

    def _instance_type_specs(self, ec2: Any, types: Sequence[str], env: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        specs: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(types), 50):
            chunk = list(types[i:i + 50])
            infos = self._paginate(
                env, "ec2", "describe_instance_types", ec2.describe_instance_types,
                {"InstanceTypes": chunk}, token_in="NextToken", token_out="NextToken",
                extract=lambda r: r.get("InstanceTypes", []), optional=True,
            )
            for info in infos:
                specs[info["InstanceType"]] = {
                    "vcpus": info.get("VCpuInfo", {}).get("DefaultVCpus"),
                    "memory_mib": info.get("MemoryInfo", {}).get("SizeInMiB"),
                    "gpu_count": sum(g.get("Count", 0) for g in info.get("GpuInfo", {}).get("Gpus", [])),
                    "inference_accelerator_count": sum(
                        a.get("Count", 0)
                        for a in info.get("InferenceAcceleratorInfo", {}).get("Accelerators", [])
                    ),
                    "instance_store_gb": info.get("InstanceStorageInfo", {}).get("TotalSizeInGB", 0),
                    "bare_metal": bool(info.get("BareMetal", False)),
                    "burstable": bool(info.get("BurstablePerformanceSupported", False)),
                }
            if not infos:
                break  # failed (already recorded) or empty: do not keep retrying chunks
        return specs

    @staticmethod
    def _norm_instance(inst: Dict[str, Any], spec: Optional[Dict[str, Any]], ecs_ids: set) -> Dict[str, Any]:
        tags = {t.get("Key"): t.get("Value") for t in inst.get("Tags", []) if t.get("Key")}
        platform = inst.get("PlatformDetails") or ("Windows" if inst.get("Platform") == "windows" else "Linux/UNIX")
        launch = inst.get("LaunchTime")
        iid = inst.get("InstanceId")
        out = {
            "instance_id": iid,
            "name": tags.get("Name"),
            "instance_type": inst.get("InstanceType"),
            "state": inst.get("State", {}).get("Name"),
            "platform": platform,
            "architecture": inst.get("Architecture"),
            "lifecycle": inst.get("InstanceLifecycle", "on-demand"),
            "tenancy": inst.get("Placement", {}).get("Tenancy"),
            "availability_zone": inst.get("Placement", {}).get("AvailabilityZone"),
            "launch_time": launch.isoformat() if hasattr(launch, "isoformat") else launch,
            "ebs_volume_count": sum(1 for b in inst.get("BlockDeviceMappings", []) if b.get("Ebs")),
            "network_interface_count": len(inst.get("NetworkInterfaces", [])),
            "autoscaling_group": tags.get(ASG_TAG),
            "is_ecs_container_instance": iid in ecs_ids,
            "tag_count": len(tags),
        }
        out["specs"] = spec
        return out

    # ------------------------------------------------------------------- Lambda
    def collect_lambda_functions(
        self, account_id: str, region: Optional[str] = None, *, presence_only: bool = False
    ) -> Dict[str, Any]:
        env = self._envelope("lambda_function", account_id, region)
        lam = self._client(account_id, "lambda", region, env)
        if lam is None:
            return env

        # presence_only is a deliberate single-page probe (no truncation warning). The full listing
        # passes max_pages=None so hitting the page limit produces an explicit warning in _paginate.
        funcs = self._paginate(
            env, "lambda", "list_functions", lam.list_functions, {"MaxItems": 50},
            token_in="Marker", token_out="NextMarker", extract=lambda r: r.get("Functions", []),
            max_pages=1 if presence_only else None,
        )
        if presence_only:
            env["present"] = bool(funcs)
            env["count"] = len(funcs)
            env["summary"] = {"in_use": bool(funcs), "presence_check_only": True}
            return env

        funcs.sort(key=lambda f: f.get("FunctionName", ""))
        funcs = self._cap(env, funcs)
        triggers = self._event_source_mappings(lam, env)
        resources = []
        for f in funcs:
            maps = triggers.get(_base_function_arn(f.get("FunctionArn", "")), [])
            resources.append({
                "function_name": f.get("FunctionName"),
                "runtime": f.get("Runtime"),
                "package_type": f.get("PackageType"),
                "memory_mb": f.get("MemorySize"),
                "timeout_seconds": f.get("Timeout"),
                "architectures": f.get("Architectures", []),
                "ephemeral_storage_mb": f.get("EphemeralStorage", {}).get("Size"),
                "vpc_attached": bool(f.get("VpcConfig", {}).get("VpcId")),
                "layer_count": len(f.get("Layers", [])),
                "last_modified": f.get("LastModified"),
                "event_source_mappings": maps,
            })
        env["resources"] = resources
        env["count"] = len(resources)
        env["summary"] = {
            "in_use": bool(resources),
            "by_package_type": dict(Counter(r["package_type"] for r in resources)),
            "with_event_source_mappings": sum(1 for r in resources if r["event_source_mappings"]),
        }
        env["warnings"].append(
            "Triggers other than event source mappings (API Gateway, S3, EventBridge, function URLs) are not collected."
        )
        return env

    def _event_source_mappings(self, lam: Any, env: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        mappings = self._paginate(
            env, "lambda", "list_event_source_mappings", lam.list_event_source_mappings, {"MaxItems": 100},
            token_in="Marker", token_out="NextMarker",
            extract=lambda r: r.get("EventSourceMappings", []), optional=True,
        )
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for m in mappings:
            parts = str(m.get("EventSourceArn", "")).split(":")
            grouped.setdefault(_base_function_arn(m.get("FunctionArn", "")), []).append({
                "source_type": parts[2] if len(parts) > 2 else None,
                "state": m.get("State"),
            })
        return grouped

    # ---------------------------------------------------------------------- ECS
    def collect_ecs_usage(
        self, account_id: str, region: Optional[str] = None, *, include_container_instances: bool = True
    ) -> Dict[str, Any]:
        env = self._envelope("ecs_cluster", account_id, region)
        ecs = self._client(account_id, "ecs", region, env)
        if ecs is None:
            return env

        arns = self._paginate(
            env, "ecs", "list_clusters", ecs.list_clusters, {"maxResults": 100},
            token_in="nextToken", token_out="nextToken", extract=lambda r: r.get("clusterArns", []),
        )
        arns = self._cap(env, sorted(arns))
        clusters: List[Dict[str, Any]] = []
        for i in range(0, len(arns), 100):
            try:
                resp = ecs.describe_clusters(clusters=arns[i:i + 100], include=["STATISTICS"])
            except Exception as e:
                self._record(env, "ecs", "describe_clusters", e)
                break
            for c in resp.get("clusters", []):
                stats = {s.get("name", "").lower(): _to_int(s.get("value")) for s in c.get("statistics", [])}
                clusters.append({
                    "cluster_name": c.get("clusterName"),
                    "cluster_arn": c.get("clusterArn"),
                    "status": c.get("status"),
                    "registered_container_instances": c.get("registeredContainerInstancesCount", 0),
                    "running_tasks": c.get("runningTasksCount", 0),
                    "active_services": c.get("activeServicesCount", 0),
                    "active_fargate_services": stats.get("activefargateservicecount", 0),
                    "running_fargate_tasks": stats.get("runningfargatetaskscount", 0),
                })
        env["resources"] = clusters
        env["count"] = len(clusters)
        ec2_ids: List[str] = []
        if include_container_instances:
            ec2_ids = self._container_instance_ec2_ids(ecs, clusters, env)
        env["container_instance_ec2_ids"] = ec2_ids
        env["summary"] = {
            "ecs_in_use": bool(clusters),
            "fargate_in_use": any(c["active_fargate_services"] > 0 or c["running_fargate_tasks"] > 0 for c in clusters),
            "container_instance_count": len(ec2_ids),
        }
        return env

    def _container_instance_ec2_ids(self, ecs: Any, clusters: List[Dict[str, Any]], env: Dict[str, Any]) -> List[str]:
        ids: List[str] = []
        for c in clusters:
            if not c["registered_container_instances"]:
                continue
            arns = self._paginate(
                env, "ecs", "list_container_instances", ecs.list_container_instances,
                {"cluster": c["cluster_arn"], "maxResults": 100},
                token_in="nextToken", token_out="nextToken",
                extract=lambda r: r.get("containerInstanceArns", []), optional=True,
            )
            for i in range(0, len(arns), 100):
                try:
                    resp = ecs.describe_container_instances(cluster=c["cluster_arn"], containerInstances=arns[i:i + 100])
                except Exception as e:
                    self._record(env, "ecs", "describe_container_instances", e, optional=True)
                    break
                ids.extend(ci["ec2InstanceId"] for ci in resp.get("containerInstances", []) if ci.get("ec2InstanceId"))
        return sorted(set(ids))

    # ---------------------------------------------------------------------- RDS
    def collect_rds(self, account_id: str, region: Optional[str] = None) -> Dict[str, Any]:
        env = self._envelope("rds_instance", account_id, region)
        rds = self._client(account_id, "rds", region, env)
        if rds is None:
            return env

        instances = self._paginate(
            env, "rds", "describe_db_instances", rds.describe_db_instances, {"MaxRecords": 100},
            token_in="Marker", token_out="Marker", extract=lambda r: r.get("DBInstances", []),
        )
        instances.sort(key=lambda d: d.get("DBInstanceIdentifier", ""))
        instances = self._cap(env, instances)
        clusters = self._paginate(
            env, "rds", "describe_db_clusters", rds.describe_db_clusters, {"MaxRecords": 100},
            token_in="Marker", token_out="Marker", extract=lambda r: r.get("DBClusters", []), optional=True,
        )

        resources = []
        for d in instances:
            engine = str(d.get("Engine", "")).lower()
            created = d.get("InstanceCreateTime")
            resources.append({
                "db_instance_id": d.get("DBInstanceIdentifier"),
                "engine": d.get("Engine"),
                "engine_version": d.get("EngineVersion"),
                "instance_class": d.get("DBInstanceClass"),
                "status": d.get("DBInstanceStatus"),
                "multi_az": d.get("MultiAZ"),
                "storage_type": d.get("StorageType"),
                "allocated_storage_gb": d.get("AllocatedStorage"),
                "is_aurora": engine.startswith("aurora"),
                "is_mysql_or_postgres": engine in SOURCE_RDS_ENGINES,
                "in_cluster": bool(d.get("DBClusterIdentifier")),
                "is_read_replica": bool(d.get("ReadReplicaSourceDBInstanceIdentifier")),
                "serverless_v2_instance": d.get("DBInstanceClass") == "db.serverless",
                "created_time": created.isoformat() if hasattr(created, "isoformat") else created,
            })
        cluster_info = []
        for c in clusters:
            sv2 = c.get("ServerlessV2ScalingConfiguration")
            cluster_info.append({
                "cluster_id": c.get("DBClusterIdentifier"),
                "engine": c.get("Engine"),
                "engine_version": c.get("EngineVersion"),
                "serverless_v2": bool(sv2),
                "min_acu": sv2.get("MinCapacity") if sv2 else None,
                "max_acu": sv2.get("MaxCapacity") if sv2 else None,
                "member_count": len(c.get("DBClusterMembers", [])),
            })
        env["resources"] = resources
        env["clusters"] = cluster_info
        env["count"] = len(resources)
        env["summary"] = {
            "mysql_or_postgres_instances": sum(1 for r in resources if r["is_mysql_or_postgres"]),
            "aurora_clusters": sum(1 for c in cluster_info if str(c["engine"]).lower().startswith("aurora")),
            "serverless_v2_in_use": any(c["serverless_v2"] for c in cluster_info)
            or any(r["serverless_v2_instance"] for r in resources),
        }
        return env

    # ---------------------------------------------------------------------- NAT
    def collect_nat_gateways(self, account_id: str, region: Optional[str] = None) -> Dict[str, Any]:
        env = self._envelope("nat_gateway", account_id, region)
        ec2 = self._client(account_id, "ec2", region, env)
        if ec2 is None:
            return env

        nats = self._paginate(
            env, "ec2", "describe_nat_gateways", ec2.describe_nat_gateways,
            {"Filter": [{"Name": "state", "Values": ["available"]}]},
            token_in="NextToken", token_out="NextToken", extract=lambda r: r.get("NatGateways", []),
        )
        endpoints = self._paginate(
            env, "ec2", "describe_vpc_endpoints", ec2.describe_vpc_endpoints, {},
            token_in="NextToken", token_out="NextToken", extract=lambda r: r.get("VpcEndpoints", []), optional=True,
        )
        gateway_eps: Dict[str, set] = {}
        for ep in endpoints:
            if ep.get("VpcEndpointType") != "Gateway":
                continue
            service = str(ep.get("ServiceName", "")).rsplit(".", 1)[-1]
            if service in ("s3", "dynamodb"):
                gateway_eps.setdefault(ep.get("VpcId"), set()).add(service)

        nats.sort(key=lambda n: n.get("NatGatewayId", ""))
        nats = self._cap(env, nats)
        resources = []
        for n in nats:
            created = n.get("CreateTime")
            have = gateway_eps.get(n.get("VpcId"), set())
            resources.append({
                "nat_gateway_id": n.get("NatGatewayId"),
                "vpc_id": n.get("VpcId"),
                "subnet_id": n.get("SubnetId"),
                "state": n.get("State"),
                "connectivity_type": n.get("ConnectivityType"),
                "created_time": created.isoformat() if hasattr(created, "isoformat") else created,
                "vpc_has_s3_gateway_endpoint": "s3" in have,
                "vpc_has_dynamodb_gateway_endpoint": "dynamodb" in have,
            })
        env["resources"] = resources
        env["count"] = len(resources)
        env["summary"] = {
            "nat_gateways": len(resources),
            "with_s3_gateway_endpoint": sum(1 for r in resources if r["vpc_has_s3_gateway_endpoint"]),
            "with_dynamodb_gateway_endpoint": sum(1 for r in resources if r["vpc_has_dynamodb_gateway_endpoint"]),
        }
        env["warnings"].append(
            "The share of NAT traffic destined to S3/DynamoDB is not available from CloudWatch or the "
            "inventory; it needs VPC Flow Logs or CUR."
        )
        return env

    # ---------------------------------------------------------------- internals
    def _envelope(self, resource_type: str, account_id: str, region: Optional[str]) -> Dict[str, Any]:
        return {
            "resource_type": resource_type,
            "account_id": account_id,
            "region": region,
            "count": 0,
            "total_seen": 0,
            "truncated": False,
            "resources": [],
            "errors": [],
            "warnings": [],
        }

    def _client(self, account_id: str, service: str, region: Optional[str], env: Dict[str, Any]) -> Any:
        try:
            return self._sm.get_client(account_id, service, region)
        except ReadOnlyViolation:
            raise
        except Exception as e:
            env["errors"].append({"api": f"{service}.get_client", "code": type(e).__name__, "message": str(e)})
            return None

    def _cap(self, env: Dict[str, Any], items: List[Any]) -> List[Any]:
        env["total_seen"] = max(env["total_seen"], len(items))
        if len(items) > self._max_resources:
            env["truncated"] = True
            env["warnings"].append(
                f"Showing {self._max_resources} of {len(items)} resources; raise max_resources to see more."
            )
            return items[: self._max_resources]
        return items

    def _paginate(
        self,
        env: Dict[str, Any],
        service: str,
        op: str,
        call: Callable[..., Dict[str, Any]],
        params: Dict[str, Any],
        *,
        token_in: str,
        token_out: str,
        extract: Callable[[Dict[str, Any]], List[Any]],
        optional: bool = False,
        max_pages: Optional[int] = None,
    ) -> List[Any]:
        items: List[Any] = []
        token = None
        limit = max_pages or self._max_pages
        for _ in range(limit):
            kwargs = dict(params)
            if token:
                kwargs[token_in] = token
            try:
                resp = call(**kwargs)
            except ReadOnlyViolation:
                raise
            except Exception as e:
                self._record(env, service, op, e, optional=optional)
                return items
            items.extend(extract(resp))
            token = resp.get(token_out)
            if not token:
                return items
        if max_pages is None:
            env["warnings"].append(f"{service}.{op} pagination stopped after {limit} pages; results may be incomplete")
        return items

    @staticmethod
    def _record(env: Dict[str, Any], service: str, op: str, exc: Exception, optional: bool = False) -> None:
        err: Dict[str, Any] = {"api": f"{service}.{op}", "optional": optional}
        if isinstance(exc, ClientError):
            e = exc.response.get("Error", {})
            err["code"] = e.get("Code", "Unknown")
            err["message"] = e.get("Message", str(exc))
            if err["code"] in _DENIED_CODES:
                action = IAM_ACTIONS.get(service, {}).get(op)
                if action:
                    err["missing_permission"] = action
                    err["hint"] = f"The role needs {action}."
        else:
            err["code"] = type(exc).__name__
            err["message"] = str(exc)
        env["errors"].append(err)


def _base_function_arn(arn: str) -> str:
    """Strip a version/alias qualifier: arn:aws:lambda:region:acct:function:name[:qualifier]."""
    return ":".join(str(arn).split(":")[:7])


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
