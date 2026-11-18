"""Bounded local workload profiles, scoped to configuration and AWS resource.

Only supported live inputs are saved after that resource was found by inventory.
Read reuse preserves every original evidence timestamp and does not extend TTL.
"""
import json
import sqlite3
from copy import deepcopy

from agent.services.live_workload_inputs import APPLICATION_FIELDS, normalize_live_profiles
from agent.services.compatibility_evaluator import timestamp, valid_value


class WorkloadProfiles:
    def __init__(self, path, ttl=86400, maximum=1024):
        self.path, self.ttl, self.maximum = path, ttl, maximum

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("CREATE TABLE IF NOT EXISTS workload_profiles (fingerprint TEXT, service TEXT, account_id TEXT, region TEXT, resource_id TEXT, updated REAL, payload TEXT, PRIMARY KEY(fingerprint,service,account_id,region,resource_id))")
        return connection

    def load(self, fingerprint, service, scope, resource_id, now):
        con = self._connect()
        try:
            with con:
                con.execute("DELETE FROM workload_profiles WHERE updated <= ?", (now-self.ttl,))
                rows = con.execute("SELECT account_id,region,resource_id,payload FROM workload_profiles WHERE fingerprint=? AND service=? ORDER BY updated DESC", (fingerprint, service)).fetchall()
            saved = [json.loads(payload) for aid, region, rid, payload in rows
                     if aid in scope and region in scope[aid] and (resource_id is None or rid == resource_id)]
            if saved:
                return saved
            # Upgrade path: recover only owner context/local reports from recent
            # same-scope saved results. Never recover derived approvals or prices.
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendations'").fetchone()
            if not exists:
                return []
            snapshots = con.execute("SELECT payload FROM recommendations WHERE created > ? ORDER BY created DESC LIMIT ?", (now-self.ttl, self.maximum)).fetchall()
            recovered = {}
            for (encoded,) in snapshots:
                snapshot = json.loads(encoded)
                report = snapshot.get("report", {})
                if snapshot.get("fingerprint") != fingerprint or report.get("service_key") != service or report.get("simulated") is True:
                    continue
                for candidate in report.get("candidates", []):
                    for item in candidate.get("details", {}).get("input_summary", []):
                        aid, region, rid = (item.get(k) for k in ("account_id", "region", "resource_id"))
                        if aid not in scope or region not in scope[aid] or (resource_id and rid != resource_id):
                            continue
                        key = (aid, region, rid)
                        if key in recovered:
                            continue
                        p = {"account_id": aid, "region": region, "resource_id": rid}
                        app, measured = item.get("application"), item.get("measurement_report")
                        dates = []
                        if app:
                            p["application"] = app
                            dates.append(timestamp(app.get("observed_at")))
                        if measured:
                            p["measurement_report"] = {**measured, "simulated": False, "resource_id": rid}
                            dates.append(timestamp(measured.get("started_at")))
                        if not dates or any(t is None or not 0 <= now-t.timestamp() < self.ttl for t in dates):
                            continue
                        try:
                            from datetime import datetime, timezone
                            normalize_live_profiles([p], datetime.fromtimestamp(now, timezone.utc))
                        except ValueError:
                            continue
                        recovered[key] = p
            return list(recovered.values())
        finally:
            con.close()

    def save(self, fingerprint, service, profile, now):
        from agent.tools.alternatives_tools import _dump
        safe = json.loads(_dump(profile))
        con = self._connect()
        try:
            with con:
                con.execute("DELETE FROM workload_profiles WHERE updated <= ?", (now-self.ttl,))
                con.execute("INSERT INTO workload_profiles VALUES (?,?,?,?,?,?,?) ON CONFLICT(fingerprint,service,account_id,region,resource_id) DO UPDATE SET updated=excluded.updated,payload=excluded.payload",
                    (fingerprint, service, profile['account_id'], profile['region'], profile['resource_id'], now, json.dumps(safe)))
                con.execute("DELETE FROM workload_profiles WHERE rowid NOT IN (SELECT rowid FROM workload_profiles ORDER BY updated DESC,rowid DESC LIMIT ?)", (self.maximum,))
        finally:
            con.close()


def profile_key(profile):
    return tuple(profile[k] for k in ("account_id", "region", "resource_id"))


def merge_profiles(saved, supplied):
    """Explicit supplied sections win; evidence records are replaced atomically."""
    result = {profile_key(p): deepcopy(p) for p in saved}
    for p in supplied:
        key = profile_key(p)
        if key not in result:
            result[key] = deepcopy(p)
            continue
        merged = result[key]
        if isinstance(p.get("application"), dict):
            # Preserve old fact dates before applying a new section-level date.
            old_app = merged.get("application", {})
            for field, (evidence_key, _) in APPLICATION_FIELDS.items():
                if field not in p["application"] and old_app.get(field) is not None:
                    merged.setdefault("evidence", {})[evidence_key] = {
                        **{name: merged[name] for name in ("account_id", "region", "resource_id")},
                        "observed_at": old_app.get("observed_at"), "source": old_app.get("source"),
                        "value": old_app.pop(field)}
        for section, value in p.items():
            if section in ("application", "evidence") and isinstance(value, dict):
                merged[section] = {**merged.get(section, {}), **deepcopy(value)}
            else:
                merged[section] = deepcopy(value)
        # A new simple/typed fact must supersede its older alternative encoding.
        for field, (evidence_key, _) in APPLICATION_FIELDS.items():
            if field in p.get("application", {}):
                merged.get("evidence", {}).pop(evidence_key, None)
            if evidence_key in p.get("evidence", {}):
                merged.get("application", {}).pop(field, None)
    return list(result.values())


def simple_answers(answers, scope, catalog, service, now):
    if not isinstance(answers, dict) or len(answers) > 50:
        raise ValueError("answers must be an object with at most 50 typed fields.")
    application = {**{"observed_at": now.isoformat(), "source": "User answers submitted to POST /recommendations"}}
    result = {**scope, "application": application}
    requirements = {req.key: req for e in catalog.approved_entries() if service in e.source_services
                    for req in e.evidence_requirements if req.origin == "attestation"}
    for key, value in answers.items():
        if key in APPLICATION_FIELDS or key in ("trigger", "input_file_bytes", "monthly_runs_min", "monthly_runs_max"):
            application[key] = value
        elif key == "purpose":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("answers.purpose must be nonempty text.")
            application["requirements"] = value
        elif key == "runs_per_month":
            if not isinstance(value, dict) or set(value) != {"min", "max"}:
                raise ValueError("runs_per_month needs min and max.")
            application.update(monthly_runs_min=value["min"], monthly_runs_max=value["max"])
        elif key == "requirements_assessed":
            if type(value) is not bool:
                raise ValueError("requirements_assessed must be true or false after a real requirements assessment.")
            result["workload_assessment"] = {**scope, "observed_at": now.isoformat(), "source": application["source"], "value": value}
        elif key in requirements and not key.startswith("nat."):
            req = requirements[key]
            if value is not None and not valid_value(req, value):
                raise ValueError(f"Invalid answer type/value for {key}.")
            result.setdefault("evidence", {})[key] = {**scope, "observed_at": now.isoformat(), "source": application["source"], "value": value}
        else:
            raise ValueError(f"Unsupported answer {key}; measurements, rates and approvals cannot be guessed or supplied as simple owner answers.")
    for field, (evidence_key, _) in APPLICATION_FIELDS.items():
        if field in application and evidence_key in result.get("evidence", {}):
            raise ValueError(f"Do not answer both {field} and {evidence_key}.")
    normalize_live_profiles([result], now)
    return result
