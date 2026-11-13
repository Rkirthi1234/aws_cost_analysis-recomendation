"""Resource-scoped evidence resolution and read-only supplementary collectors.

Application facts are dated attestations. Measurements must carry their source,
scope, date and coverage; none may overwrite AWS inventory or a derived fact.
"""
from datetime import datetime, timezone

from agent.services.compatibility_evaluator import number, timestamp, valid_value
from agent.services.metric_coverage import calculate_series_coverage
from agent.services.readonly_guard import ReadOnlySessionManager


def get_path(body, path):
    for part in path.split("."):
        if not isinstance(body, dict) or part not in body:
            return None
        body = body[part]
    return body


def available(value, source, **extra):
    return {"value": value, "status": "available", "source": source, **extra}


def unavailable(message, failed=False):
    return {"status": "failed" if failed else "missing", "message": message}


def validate_record(record, scope, settings, now, *, attestation=False):
    if not isinstance(record, dict):
        return "A dated evidence record is required."
    if any(record.get(k) != v for k, v in scope.items()):
        return "Evidence account, region and resource must match the evaluated resource."
    when = timestamp(record.get("observed_at"))
    if when is None or when > now:
        return "Evidence needs a timezone-aware, non-future observed_at."
    max_age = settings.value_of("attestation.max_age_days") if attestation else None
    if not attestation:
        ttl = settings.value_of("cache.ttl_seconds")
        if ttl is None or (now - when).total_seconds() > ttl:
            return "Measurement is stale; refresh it within cache.ttl_seconds."
    if attestation and max_age is not None and (now - when).total_seconds() > max_age * 86400:
        return "Attestation has expired; obtain an updated workload-owner assessment."
    if not isinstance(record.get("source"), str) or not record["source"].strip():
        return "Evidence needs a source (user statement, benchmark, CUR query or Flow Logs query)."
    return None


class RecommendationEvidence:
    def __init__(self, session_manager, settings, now_fn=None):
        self.sm = ReadOnlySessionManager(session_manager)
        self.settings = settings
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def resolve(self, entry, region_result, resource, account_id, inputs=None):
        inputs = inputs or {}
        rid = next((resource[k] for k in ("instance_id", "function_name", "db_instance_id", "nat_gateway_id") if resource.get(k)), None)
        region = region_result["region"]
        scope = dict(account_id=account_id, region=region, resource_id=rid)
        metric_env = region_result.get("metrics", {})
        metric = metric_env.get("resources", {}).get(rid, {})
        observations, issues = {}, []
        assessment = inputs.get("workload_assessment")
        assessment_error = validate_record(assessment, scope, self.settings, self.now_fn(), attestation=True)
        if assessment_error or assessment.get("value") is not True:
            issues.append("A dated workload_assessment confirming state, storage, networking, runtime, security and availability requirements is required.")
        for section, body in region_result.items():
            if isinstance(body, dict):
                issues.extend(f"{section}: {e.get('code', 'collection_error')}" for e in body.get("errors", []))
                if body.get("truncated"):
                    issues.append(f"{section}: inventory was truncated; presence and evidence are incomplete.")
        if metric_env.get("warnings"):
            issues.extend(metric_env["warnings"])
        for req in entry.evidence_requirements:
            if req.origin == "collected":
                path = req.source_path or ""
                if path == "regions[].region":
                    val = region
                elif ".inventory.resources[]." in path:
                    val = get_path(resource, path.split(".inventory.resources[].", 1)[1])
                elif ".metrics.resources[" in path:
                    suffix = path.split("].", 1)[1] if "]." in path else ""
                    # Split after the RESOURCE selector, not the leading regions[].
                    suffix = path.split("].", 2)[-1]
                    suffix = suffix.replace("{statistic}", self.settings.value_of("ec2_to_fargate.sizing_statistic") or "max")
                    val = get_path(metric, suffix)
                else:
                    val = None
                if val is not None:
                    observations[req.key] = available(val, "AWS read APIs", observed_at=self.now_fn().isoformat())
                    if req.requires_metric_coverage:
                        observations[req.key].update(coverage=metric.get("metric_coverage", {}).get(req.coverage_series),
                                                     collection_complete=metric.get("collection_complete", False))
            elif req.origin in ("attestation", "planned"):
                record = inputs.get("evidence", {}).get(req.key)
                if record is not None:
                    error = validate_record(record, scope, self.settings, self.now_fn(), attestation=req.origin == "attestation")
                    if error:
                        observations[req.key] = unavailable(error)
                        if "expired" in error:
                            observations[req.key]["status"] = "expired"
                    elif not valid_value(req, record.get("value")):
                        observations[req.key] = unavailable(f"Invalid value for {req.key}.")
                    else:
                        observations[req.key] = {**record, "status": "available", "origin": req.origin}
                        if req.key.startswith("nat.") and req.key.endswith("_bytes"):
                            start, end = timestamp(record.get("period_start")), timestamp(record.get("period_end_exclusive"))
                            if (start is None or end is None or end <= start or end > self.now_fn()
                                    or record.get("collection_complete") is not True
                                    or (end - start).total_seconds() < self.settings.window_minimum_days * 86400):
                                observations[req.key] = unavailable("NAT destination measurements require a complete, representative observed time window.")
        if entry.id.startswith("ec2-"):
            self._memory(account_id, region, rid, metric_env.get("window", {}), observations)
        if entry.id.startswith("nat-"):
            self._routes(account_id, region, resource, observations, issues)
        if entry.id.startswith("rds-"):
            self._aurora(account_id, region, resource, observations, issues)
        self._derive(entry, observations)
        return observations, issues, scope

    def _memory(self, account, region, rid, window, out):
        # Uses existing metrics only; does not install an agent or create a metric.
        key = "ec2.memory_utilization_max_pct"
        if key in out and out[key].get("status") == "available":
            return
        start, end = timestamp(window.get("start")), timestamp(window.get("end_exclusive"))
        if start is None or end is None:
            out[key] = unavailable("No valid metric window for the CWAgent memory query.")
            return
        points, token, complete = {}, None, True
        try:
            client = self.sm.get_client(account, "cloudwatch", region)
            for _ in range(20):
                params = dict(StartTime=start, EndTime=end, MetricDataQueries=[{
                    "Id": "memory", "MetricStat": {"Metric": {"Namespace": "CWAgent", "MetricName": "mem_used_percent",
                    "Dimensions": [{"Name": "InstanceId", "Value": rid}]}, "Period": 3600, "Stat": "Maximum"},
                    "ReturnData": True}])
                if token:
                    params["NextToken"] = token
                result = client.get_metric_data(**params)
                token = result.get("NextToken")
                series = result.get("MetricDataResults", [])
                if not series:
                    complete = False
                for row in series:
                    if row.get("StatusCode") not in ("Complete", "PartialData") or (row.get("StatusCode") == "PartialData" and not token):
                        complete = False
                    for t, v in zip(row.get("Timestamps", []), row.get("Values", [])):
                        if start <= t < end and number(v) and 0 <= v <= 100:
                            points[t] = max(points.get(t, 0), v)
                if not token:
                    break
            else:
                complete = False
            if not points:
                out[key] = unavailable("CWAgent mem_used_percent with InstanceId-only dimensions is absent; supply a measured record with raw coverage for another metric schema.")
            else:
                cov = calculate_series_coverage(len(points), (end - start).total_seconds() / 86400).as_dict()
                out[key] = available(max(points.values()), "CloudWatch CWAgent/mem_used_percent Maximum", coverage=cov,
                                     collection_complete=complete, observed_at=self.now_fn().isoformat())
        except Exception as exc:
            out[key] = unavailable(f"Memory collection failed: {type(exc).__name__}: {exc}", True)

    def _routes(self, account, region, resource, out, issues):
        try:
            client = self.sm.get_client(account, "ec2", region)
            tables = self._pages(client.describe_route_tables, "RouteTables", "NextToken",
                                 Filters=[{"Name": "vpc-id", "Values": [resource["vpc_id"]]}])
            endpoints = self._pages(client.describe_vpc_endpoints, "VpcEndpoints", "NextToken",
                                    Filters=[{"Name": "vpc-id", "Values": [resource["vpc_id"]]}])
            used = sorted(t["RouteTableId"] for t in tables if any(
                r.get("NatGatewayId") == resource["nat_gateway_id"] and r.get("State") == "active"
                for r in t.get("Routes", [])))
            out["nat.route_tables_using_nat"] = available(used, "ec2:DescribeRouteTables")
            for service in ("s3", "dynamodb"):
                # A real active prefix-list route is required, not merely a VPC-level endpoint flag.
                eps = {e["VpcEndpointId"]: set(e.get("RouteTableIds", [])) for e in endpoints
                       if e.get("VpcEndpointType") == "Gateway" and e.get("State") == "available"
                       and e.get("ServiceName") == f"com.amazonaws.{region}.{service}"}
                covered = sorted(t["RouteTableId"] for t in tables if any(
                    r.get("GatewayId") in eps and t["RouteTableId"] in eps[r["GatewayId"]]
                    and r.get("DestinationPrefixListId") and r.get("State") == "active" for r in t.get("Routes", [])))
                out[f"nat.{service}.covered_route_table_ids"] = available(covered, "ec2:DescribeRouteTables+DescribeVpcEndpoints")
        except Exception as exc:
            issues.append(f"Route coverage collection failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _pages(method, key, token_key, **params):
        items, token, seen = [], None, set()
        for _ in range(50):
            result = method(**params, **({token_key: token} if token else {}))
            items.extend(result.get(key, []))
            token = result.get(token_key)
            if not token:
                return items
            if token in seen:
                break
            seen.add(token)
        raise ValueError("Read API pagination incomplete")

    def _aurora(self, account, region, resource, out, issues):
        record = out.get("rds.aurora_version_mapping", {})
        mapping = record.get("value", {})
        expected = {"mysql": "aurora-mysql", "postgres": "aurora-postgresql"}.get(resource.get("engine"))
        if (record.get("status") != "available" or mapping.get("source_engine") != resource.get("engine")
                or mapping.get("source_version") != resource.get("engine_version") or mapping.get("target_engine") != expected
                or not isinstance(mapping.get("target_version"), str) or not mapping["target_version"]
                or mapping.get("migration_assessed") is not True):
            out["rds.aurora_version_mapping"] = unavailable("A verified exact-version Aurora migration mapping is required.")
            return
        try:
            rds = self.sm.get_client(account, "rds", region)
            args = dict(Engine=expected, EngineVersion=mapping["target_version"])
            versions = self._pages(rds.describe_db_engine_versions, "DBEngineVersions", "Marker", **args)
            options = self._pages(rds.describe_orderable_db_instance_options, "OrderableDBInstanceOptions", "Marker",
                                  DBInstanceClass="db.serverless", **args)
            supported = bool(options) and bool(versions)
            out["rds.serverless_v2_region_version_support"] = available("supported" if supported else "unsupported",
                                                                                  "rds:DescribeOrderableDBInstanceOptions")
            features = versions[0].get("ServerlessV2FeaturesSupport", {}) if versions else {}
            low, high = features.get("MinCapacity"), features.get("MaxCapacity")
            if number(low) and number(high) and high >= low:
                out["rds.serverless_v2_capacity_limits"] = available({"min_acu": low, "max_acu": high},
                                                                                       "rds:DescribeDBEngineVersions")
            estimate = out.get("rds.workload_capacity_estimate", {}).get("value", {})
            limits = out.get("rds.serverless_v2_capacity_limits", {}).get("value", {})
            if not (all(number(estimate.get(k)) for k in ("min_acu", "average_acu", "peak_acu"))
                    and number(limits.get("min_acu")) and number(limits.get("max_acu"))
                    and limits["min_acu"] <= estimate["min_acu"] <= estimate["average_acu"] <= estimate["peak_acu"] <= limits["max_acu"]
                    and estimate.get("features_assessed") is True and estimate.get("performance_assessed") is True):
                out["rds.workload_capacity_estimate"] = unavailable("A measured capacity estimate within verified Aurora limits, with feature and performance assessment, is required.")
        except Exception as exc:
            issues.append(f"Aurora support collection failed: {type(exc).__name__}: {exc}")
            out["rds.serverless_v2_region_version_support"] = unavailable("Aurora support lookup failed.", True)

    def _derive(self, entry, out):
        # Definitions are topologically ordered in the shipped catalog. Iterate for extensibility.
        for _ in range(len(entry.evidence_requirements)):
            changed = False
            for req in entry.evidence_requirements:
                if req.origin != "derived" or req.key in out:
                    continue
                deps = [out.get(k) for k in req.derived_from]
                if not deps or any(not d or d.get("status") != "available" for d in deps):
                    continue
                # Derived values inherit unavailable/low-coverage dependencies rather than bypassing gates.
                valid = True
                for key, obs in zip(req.derived_from, deps):
                    parent = entry.evidence_requirement(key)
                    if not valid_value(parent, obs.get("value")):
                        valid = False
                    if parent.requires_metric_coverage:
                        cov = obs.get("coverage", {}) or {}
                        valid &= (number(cov.get("coverage_pct")) and number(cov.get("window_days"))
                                  and cov["coverage_pct"] >= self.settings.min_coverage_pct
                                  and cov["window_days"] >= self.settings.window_minimum_days
                                  and obs.get("collection_complete") is True)
                if not valid:
                    out[req.key] = unavailable(f"A dependency of {req.key} is invalid or has insufficient raw coverage.")
                    continue
                vals = [d["value"] for d in deps]
                a = vals[0]
                name, result = req.derivation, None
                if name == "route_tables_without_endpoint_route":
                    result = sorted(set(a) - set(vals[1]))
                elif name == "gateway_endpoint_route_table_coverage" and a:
                    overlap = set(a) & set(vals[1])
                    result = "full" if overlap == set(a) else "partial" if overlap else "none"
                elif name == "monthly_invocations_from_hourly_mean":
                    hours = self.settings.value_of("pricing.hours_per_month")
                    result = a * hours if hours is not None else None
                elif name == "average_concurrency_from_invocations_and_duration":
                    result = a * vals[1] / 3600000
                elif name == "fraction_to_percent" and 0 <= a <= 1:
                    result = a * 100
                elif name == "peak_memory_mib_from_utilization_and_instance_memory" and 0 <= vals[1] <= 100:
                    result = a * vals[1] / 100
                elif name == "peak_vcpus_from_cpu_pct_and_vcpus" and 0 <= a <= 100:
                    result = a * vals[1] / 100
                elif name == "fargate_os_family_from_ec2_platform":
                    result = "WINDOWS" if a == "Windows" else "LINUX" if a == "Linux/UNIX" else "UNSUPPORTED"
                if result is not None:
                    out[req.key] = available(result, f"derived:{name}", derived_from=list(req.derived_from))
                    changed = True
            if not changed:
                break
