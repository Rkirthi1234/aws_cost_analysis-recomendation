"""On-demand threshold -> evidence -> compatibility -> cost -> report workflow."""
from datetime import datetime, timezone
from decimal import Decimal

from agent.services.alternative_decision_settings import blocked_decision, load_alternative_decision_settings
from agent.services.alternative_pricing import AWSPriceList, AlternativeCostComparison
from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.compatibility_evaluator import CompatibilityEvaluator
from agent.services.cost_thresholds import CostThresholdDetector, load_cost_threshold_settings
from agent.services.high_cost_identifier import HighCostServiceIdentifier
from agent.services.recommendation_evidence import RecommendationEvidence
from agent.services.readonly_guard import ReadOnlySessionManager
from agent.services.usage_evidence import UsageEvidenceCollector
from agent.services.usage_pattern_config import load_usage_pattern_settings


class RecommendationService:
    def __init__(self, config, session_manager, registry, *, catalog=None, now_fn=None,
                 collector=None, resolver=None, prices=None, cost_source=None):
        self.config, self.sm, self.registry = config, session_manager, registry
        self.catalog = catalog or AlternativesCatalog.load()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.settings = load_alternative_decision_settings(config)
        usage = load_usage_pattern_settings(config)
        self.collector = collector or UsageEvidenceCollector(session_manager, self.catalog, registry,
                                                            window=usage.window, thresholds=usage.thresholds, now_fn=self.now_fn)
        self.resolver = resolver or RecommendationEvidence(session_manager, self.settings, self.now_fn)
        self.evaluator = CompatibilityEvaluator(self.settings, self.now_fn)
        self.prices = prices or AWSPriceList(session_manager, self.settings.value_of("cache.ttl_seconds") or 86400, self.now_fn)
        self.comparison = AlternativeCostComparison(self.prices, self.settings, self.now_fn)
        self.cost_source = cost_source

    def _scope(self, account_id, regions):
        if account_id is not None and account_id not in self.registry.entries:
            raise ValueError("Only configured accounts can be inspected.")
        accounts = [account_id] if account_id else list(self.registry.entries)
        if regions is not None and (not isinstance(regions, list) or not 1 <= len(regions) <= 10
                                    or any(not isinstance(r, str) or not r.strip() for r in regions)):
            raise ValueError("regions must be a nonempty list of up to ten AWS regions.")
        return {a: list(dict.fromkeys(regions or [self.registry.get_account(a).region])) for a in accounts}

    def evaluate(self, alternative_id, account_id=None, regions=None, workload_inputs=None, resource_id=None):
        entry = next((e for e in self.catalog.approved_entries() if e.id == alternative_id), None)
        if entry is None:
            raise ValueError("Unknown or unapproved alternative_id.")
        scope = self._scope(account_id, regions)
        profiles = [] if workload_inputs is None else workload_inputs
        if not isinstance(profiles, list) or len(profiles) > 200:
            raise ValueError("workload_inputs must be a list of at most 200 scoped resource profiles.")
        indexed = {}
        for p in profiles:
            if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p[k] for k in ("account_id", "region", "resource_id")):
                raise ValueError("Each profile needs account_id, region and resource_id.")
            if p["account_id"] not in scope or p["region"] not in scope[p["account_id"]]:
                raise ValueError("Profile scope is outside the inspected accounts/regions.")
            key = tuple(p[k] for k in ("account_id", "region", "resource_id"))
            if key in indexed:
                raise ValueError("Duplicate resource profiles are ambiguous.")
            indexed[key] = p
        reports, warnings, errors, scanned, limited = [], [], [], 0, False
        for aid, rs in scope.items():
            try:
                evidence = self.collector.collect(alternative_id, aid, rs)
            except Exception as exc:
                errors.append({"account_id": aid, "code": type(exc).__name__, "message": str(exc)})
                continue
            if evidence.get("error"):
                errors.append({"account_id": aid, **evidence})
                continue
            errors.extend(evidence.get("errors", []))
            region_results = evidence.get("regions", [])
            # Usage is checked over every region in this explicitly reported scope.
            presence = self._presence(entry, region_results)
            for rr in region_results:
                warnings.extend(rr.get("inventory", {}).get("warnings", []))
                warnings.extend(rr.get("metrics", {}).get("warnings", []))
                resources = rr.get("inventory", {}).get("resources", [])
                if not resources:
                    warnings.append(f"{aid}/{rr.get('region')}: no source resources found; no migration recommendation produced.")
                for resource in resources:
                    rid = next((resource[k] for k in ("instance_id", "function_name", "db_instance_id", "nat_gateway_id") if resource.get(k)), None)
                    if resource_id and rid != resource_id:
                        continue
                    scanned += 1
                    if len(reports) >= 25:
                        limited = True
                        continue
                    profile = indexed.get((aid, rr["region"], rid), {})
                    try:
                        resource_scope = dict(account_id=aid, region=rr["region"], resource_id=rid)
                        if blocked_decision(self.settings, entry.id, entry.decision_setting_paths(), **resource_scope):
                            observations, issues = {}, []
                        else:
                            observations, issues, resource_scope = self.resolver.resolve(entry, rr, resource, aid, profile)
                        subjects = ("s3", "dynamodb") if entry.id.startswith("nat-") else (None,)
                        for subject in subjects:
                            if len(reports) >= 25:
                                limited = True
                                break
                            decision = self.evaluator.evaluate(entry, observations, **resource_scope, subject=subject,
                                                               target_in_use=presence, collection_issues=issues)
                            pricing = self.comparison.compare(entry, decision, resource, profile)
                            if decision["verdict"] == "compatible" and pricing.get("recommend"):
                                status = "recommended"
                                explanation = (f"{entry.source_name} → {entry.alternative_name}: the catalog compatibility rules passed. "
                                               f"Estimated monthly cost ${pricing['current_monthly_cost_usd']} → ${pricing['alternative_monthly_cost_usd']}; "
                                               f"estimated savings ${pricing['monthly_savings_usd']} ({pricing['savings_pct']}%).")
                            elif decision["verdict"] == "compatible" and pricing.get("status") == "estimated":
                                status = "no_cost_advantage"
                                explanation = "Compatible under the evaluated rules, but the savings are not positive or do not meet configured minimums."
                            else:
                                status = "already_in_use" if decision["verdict"] == "already_in_use" else "incompatible" if decision["verdict"] == "incompatible" else "needs_information"
                                explanation = "No migration recommendation: " + (pricing.get("reason", "") if decision["verdict"] == "compatible" else
                                              "; ".join(r["description"] for r in decision["matched_rules"]) or
                                              "; ".join(r["message"] for r in decision["reasons"]))
                            reports.append({"status": status, "source": entry.source_name, "alternative": entry.alternative_name,
                                            "decision": decision, "cost_comparison": pricing, "explanation": explanation,
                                            "collection_issues": issues, "evidence_gaps": evidence.get("evidence_gaps", [])})
                    except Exception as exc:
                        errors.append({"account_id": aid, "region": rr.get("region"), "resource_id": rid,
                                       "code": type(exc).__name__, "message": str(exc)})
        if resource_id and scanned == 0:
            warnings.append("The requested resource was not found in the inspected source inventory.")
        if limited:
            warnings.append("Report details are capped at 25; remaining resources were not evaluated. Request a resource_id for each remaining resource.")
        return {"alternative_id": entry.id, "status": "partial" if errors or limited else "evaluated",
                "scope": scope, "target_usage_scope": "ECS Fargate / target inventory in the listed regions only; no claim about uninspected regions",
                "resources_seen": scanned, "reports": reports, "reports_shown": len(reports),
                "errors": errors, "warnings": list(dict.fromkeys(warnings)),
                "recommendations": sum(r["status"] == "recommended" for r in reports),
                "settings": self.settings.as_dict(), "aws_resources_changed": False}

    @staticmethod
    def _presence(entry, regions):
        if entry.id.startswith("nat-"):
            # NAT endpoint usage is evaluated by route-table subject rules, not VPC-wide flags.
            return False
        values = []
        for rr in regions:
            if entry.id == "ec2-short-event-driven-to-lambda":
                body = rr.get("lambda_presence", {})
                known = "count" in body
                used = (body.get("count") or 0) > 0
            elif "fargate" in entry.id:
                body = rr.get("ecs", {})
                known = "fargate_in_use" in body.get("summary", {}) and body.get("fargate_presence_complete", True)
                used = body.get("summary", {}).get("fargate_in_use") is True
            else:
                body = rr.get("inventory", {})
                known = "serverless_v2_in_use" in body.get("summary", {})
                used = body.get("summary", {}).get("serverless_v2_in_use") is True
            if body.get("errors") or body.get("truncated") or not known:
                values.append(None)
            else:
                values.append(used)
        return True if True in values else None if not values or None in values else False

    def run(self, start_date=None, end_date=None, account_id=None, regions=None, workload_inputs=None):
        self._scope(account_id, regions)
        threshold_settings = load_cost_threshold_settings(self.config, self.catalog)
        source = self.cost_source
        if threshold_settings.valid and threshold_settings.enabled and source is None:
            # Screen every thresholded service rather than only the default top-five high-cost list.
            source = HighCostServiceIdentifier(ReadOnlySessionManager(self.sm), self.registry, self.catalog)
        alerts = CostThresholdDetector(threshold_settings, self.catalog, source).detect(
            start_date, end_date, today=self.now_fn().date())
        if alerts.get("error") or alerts.get("status") != "evaluated":
            return {"status": alerts.get("status", "failed"), "thresholds": alerts, "alternatives": [],
                    "notification_sent": False, "aws_resources_changed": False}
        crossed = set(alerts.get("crossed_service_keys", []))
        entries = [e for e in self.catalog.approved_entries() if crossed.intersection(e.source_services)]
        results = []
        for entry in entries:
            results.append(self.evaluate(entry.id, account_id, regions, workload_inputs))
        unmapped = sorted(s for s in crossed if not any(s in e.source_services for e in entries))
        preferred = {}
        for result in results:
            for report in result["reports"]:
                if report["status"] != "recommended":
                    continue
                decision, cost = report["decision"], report["cost_comparison"]
                key = tuple(decision.get(k) for k in ("account_id", "region", "resource_id", "subject"))
                candidate = {k: decision.get(k) for k in ("account_id", "region", "resource_id", "subject", "alternative_id")}
                candidate.update(current_monthly_cost_usd=cost["current_monthly_cost_usd"],
                                 alternative_monthly_cost_usd=cost["alternative_monthly_cost_usd"],
                                 monthly_savings_usd=cost["monthly_savings_usd"], selection_basis="lowest estimated target cost among validated candidates")
                exact = Decimal(cost["exact"]["alternative"])
                if key not in preferred or exact < preferred[key][0]:
                    preferred[key] = (exact, candidate)
        return {"status": "partial" if any(r["status"] == "partial" for r in results) else "evaluated",
                "thresholds": alerts, "alternatives": results, "unmapped_services": unmapped,
                "recommendations": sum(r["recommendations"] for r in results),
                "preferred_recommendations": [item for _, item in preferred.values()],
                "notification": "This report is the on-demand alert; no email/SNS was sent.",
                "notification_sent": False, "aws_resources_changed": False,
                "warnings": ["Threshold spend is payer-level service spending; resource savings use separate resource-attributed evidence.",
                             "Do not add savings for competing alternatives to the same workload.",
                             "Services without a catalog mapping receive a threshold alert only."]}
