"""Date-based cost ranking and saved-analysis handoff to the existing evaluator.

Cost Explorer reads remain read-only. Only analysis snapshots are saved locally;
credentials and workload inputs are never stored in the analysis database.
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from agent.services.high_cost_identifier import HighCostServiceIdentifier
from agent.services.readonly_guard import ReadOnlySessionManager


class WorkflowError(Exception):
    def __init__(self, code, message, status_code=422):
        super().__init__(message)
        self.code, self.status_code = code, status_code


class AnalysisStore:
    """Small, bounded SQLite snapshot store, shared across local API workers/restarts."""
    def __init__(self, path, ttl_seconds=86400, max_entries=1024):
        self.path = str(Path(path))
        self.ttl, self.max_entries = ttl_seconds, max_entries

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS cost_analyses (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)")
                yield connection
        finally:
            connection.close()

    def save(self, report, fingerprint, now):
        payload = json.dumps({"fingerprint": fingerprint, "report": report})
        with self._connect() as con:
            con.execute("DELETE FROM cost_analyses WHERE created <= ?", (now - self.ttl,))
            con.execute("INSERT INTO cost_analyses VALUES (?, ?, ?)", (report["analysis_id"], now, payload))
            con.execute("DELETE FROM cost_analyses WHERE rowid NOT IN (SELECT rowid FROM cost_analyses ORDER BY rowid DESC LIMIT ?)", (self.max_entries,))

    def load(self, analysis_id, fingerprint, now):
        with self._connect() as con:
            row = con.execute("SELECT created, payload FROM cost_analyses WHERE id = ?", (analysis_id,)).fetchone()
        if row is None:
            raise WorkflowError("AnalysisNotFound", "Analysis not found. Run POST /cost-analysis first.", 404)
        if now - row[0] >= self.ttl:
            raise WorkflowError("AnalysisExpired", "This analysis has expired. Run POST /cost-analysis again.", 410)
        payload = json.loads(row[1])
        if payload["fingerprint"] != fingerprint:
            raise WorkflowError("AnalysisScopeChanged", "The configured accounts changed. Create a new cost analysis.", 409)
        return payload["report"]


def cost_amount(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise WorkflowError("InvalidCostData", "Cost Explorer returned an invalid amount; no complete analysis was saved.", 502) from None


class CostAnalysisWorkflow:
    def __init__(self, recommendation_service, store, *, cost_source=None, now_fn=None):
        self.engine, self.store = recommendation_service, store
        self.catalog, self.registry = self.engine.catalog, self.engine.registry
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.cost_source = cost_source

    def _fingerprint(self):
        accounts = sorted((a.account_id, a.account_type, a.region, a.role_arn,
                           getattr(a, "use_current_credentials", False)) for a in self.registry.entries.values())
        return hashlib.sha256(json.dumps(accounts).encode()).hexdigest()

    def analyze(self, start_date=None, end_date=None, top_n=5):
        if (start_date is None) != (end_date is None):
            raise ValueError("Provide both dates or neither.")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 50:
            raise ValueError("top_n must be a whole number from 1 to 50.")
        today = self.now_fn().date()
        start_date = start_date or today.replace(day=1).isoformat()
        end_date = end_date or today.isoformat()
        problem = HighCostServiceIdentifier._validate_dates(start_date, end_date)
        if problem:
            raise ValueError(problem["message"])
        if datetime.fromisoformat(end_date).date() > today:
            raise ValueError("end_date cannot be in the future; this endpoint reads actual costs, not a forecast.")
        source = self.cost_source or HighCostServiceIdentifier(ReadOnlySessionManager(self.engine.sm), self.registry, self.catalog)
        result = source.service_costs(start_date, end_date)
        if result.get("error"):
            code = result.get("code") or result.get("exception_type") or "CostReadFailed"
            raise WorkflowError("CostReadFailed", f"Cost Explorer could not be read ({code}). Check AWS authentication and read permissions.", 502)
        if result.get("warnings"):
            raise WorkflowError("IncompleteCostData", "Cost collection was incomplete: " + "; ".join(result["warnings"]), 502)
        totals = {}
        for ce_name, raw in result.get("costs", {}).items():
            if result.get("units", {}).get(ce_name) != ["USD"]:
                raise WorkflowError("UnsupportedCurrency", "All cost amounts must be USD; no currency conversion or mixed-unit sum is performed.", 502)
            amount = cost_amount(raw)
            key = self.catalog.resolve_service(ce_name)
            group = key or "ce_service:" + ce_name
            if group not in totals:
                totals[group] = {"service_key": key, "service": self.catalog.services[key].display_name if key else ce_name,
                                 "amount": Decimal(0), "cost_explorer_services": []}
            row = totals[group]
            row["amount"] += amount
            row["cost_explorer_services"].append({"service": ce_name, "cost_usd": float(amount), "cost_usd_exact": str(amount)})
        ranked = sorted(totals.values(), key=lambda r: (-r["amount"], r["service"]))
        services = []
        for rank, row in enumerate(ranked, 1):
            candidates = [e.id for e in self.catalog.approved_entries() if row["service_key"] in e.source_services]
            services.append({"rank": rank, "service_key": row["service_key"], "service": row["service"],
                             "cost_usd": float(row["amount"]), "cost_usd_exact": str(row["amount"]),
                             "alternative_available": bool(candidates), "alternative_ids": candidates,
                             "cost_explorer_services": row["cost_explorer_services"]})
        total = sum((r["amount"] for r in ranked), Decimal(0))
        payer = self.registry.get_payer_account()
        report = {"analysis_id": "analysis-" + uuid.uuid4().hex, "status": "evaluated" if services else "no_cost_data",
                  "period": {"start_date": start_date, "end_date_exclusive": end_date},
                  "created_at": self.now_fn().isoformat(), "currency": "USD", "metric": result.get("metric", "AmortizedCost"),
                  "total_cost_usd": float(total), "total_cost_usd_exact": str(total),
                  "service_count": len(services), "top_n": top_n, "highest_cost_services": services[:top_n], "services": services,
                  "billing_scope": {"payer_account_id": payer.account_id, "regions": "all billed regions",
                                    "account_scope": "payer-level Cost Explorer totals; may include linked accounts"},
                  "warnings": ["End date is exclusive. Cost Explorer data can lag; these are observed costs, not forecasts.",
                               "EC2 includes Compute and EC2 - Other; its total is not an individual instance cost.",
                               "A catalog mapping is a candidate, not proof of compatibility or savings.",
                               "Credits and negative amounts are preserved in net spending totals."],
                  "aws_resources_changed": False}
        self.store.save(report, self._fingerprint(), self.now_fn().timestamp())
        return report

    def recommend(self, analysis_id, service=None, account_id=None, regions=None, workload_inputs=None, resource_id=None):
        report = self.store.load(analysis_id, self._fingerprint(), self.now_fn().timestamp())
        if not report["services"]:
            return {"analysis_id": analysis_id, "status": "no_cost_data", "candidates": [], "recommendation": None}
        if service is None:
            selected = report["highest_cost_services"][0]
        else:
            normalized = service.strip().casefold()
            selected = next((r for r in report["services"] if normalized in
                {r["service"].casefold(), (r["service_key"] or "").casefold(),
                 r["service"].split(" (")[0].casefold(),
                 *(v["service"].casefold() for v in r["cost_explorer_services"])}), None)
            if selected is None:
                raise ValueError("Service was not found in this saved cost analysis. Use a returned service or service_key.")
        self.engine._scope(account_id, regions)
        header = {"analysis_id": analysis_id, "service": selected["service"], "service_key": selected["service_key"],
                  "period": report["period"], "selected_period_cost_usd": selected["cost_usd"],
                  "selected_period_cost_usd_exact": selected["cost_usd_exact"], "billing_scope": report["billing_scope"],
                  "aws_resources_changed": False,
                  "warnings": ["Selected-period service spending is not used as an individual resource's monthly baseline.",
                               "Fresh workload evidence uses its own metric window, which can differ from the billing period.",
                               "Do not add savings for competing replacements of the same workload."]}
        if not selected["alternative_ids"]:
            return {**header, "status": "no_supported_alternative", "candidates": [], "recommendation": None,
                    "reason": "No approved source-service mapping exists in the current catalog."}
        candidates, qualifying = [], []
        for eid in selected["alternative_ids"]:
            outcome = self.engine.evaluate(eid, account_id, regions, workload_inputs, resource_id)
            entry = next(e for e in self.catalog.approved_entries() if e.id == eid)
            reports = outcome.get("reports", [])
            missing = []
            for rr in reports:
                missing.extend(r["message"] for r in rr["decision"].get("reasons", []))
                if rr.get("cost_comparison", {}).get("status") == "insufficient_data":
                    missing.append(rr["cost_comparison"].get("reason", "Pricing information is missing."))
            statuses = {r["status"] for r in reports}
            status = ("recommended" if "recommended" in statuses else "needs_information" if
                      "needs_information" in statuses or outcome.get("errors") else "no_resources_found" if not reports else
                      "no_cost_advantage" if "no_cost_advantage" in statuses else
                      "incompatible" if "incompatible" in statuses else "already_in_use")
            candidate = {"alternative_id": eid, "alternative": entry.alternative_name, "status": status,
                         "missing_information": list(dict.fromkeys(missing)), "details": outcome}
            candidates.append(candidate)
            for rr in reports:
                if rr["status"] == "recommended":
                    d, cost = rr["decision"], rr["cost_comparison"]
                    qualifying.append({"alternative_id": eid, "alternative": entry.alternative_name,
                        **{k: d.get(k) for k in ("account_id", "region", "resource_id", "subject")},
                        **{k: cost[k] for k in ("current_monthly_cost_usd", "alternative_monthly_cost_usd", "monthly_savings_usd", "savings_pct")},
                        "reason": rr["explanation"], "_exact": cost["exact"]["alternative"]})
        # Choose independently for each resource/subject, not across unrelated workloads.
        best = {}
        for item in qualifying:
            key = tuple(item[k] for k in ("account_id", "region", "resource_id", "subject"))
            if key not in best or Decimal(item["_exact"]) < Decimal(best[key]["_exact"]):
                best[key] = item
        preferred = [{k: v for k, v in item.items() if k != "_exact"} for item in best.values()]
        statuses = {c["status"] for c in candidates}
        status = "recommended" if preferred else "needs_information" if "needs_information" in statuses else "no_recommendation"
        return {**header, "status": status, "candidates": candidates, "preferred_recommendations": preferred,
                "recommendation": preferred[0] if len(preferred) == 1 else None}
