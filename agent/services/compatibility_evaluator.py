"""Execute the catalog's typed rules. Missing observations are never truthy defaults.

Inputs are normalized by recommendation_evidence; no AWS calls occur here.
"""
import math
import operator
from datetime import datetime, timezone

from agent.services import aws_limits
from agent.services.alternative_decision_models import (
    AlternativeDecision, DecisionReason, ReasonCode, Verdict, insufficient_data,
)
from agent.services.alternative_decision_settings import blocked_decision


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return result.astimezone(timezone.utc) if isinstance(result, datetime) and result.tzinfo else None
    except (ValueError, TypeError):
        return None


def fargate_size(cpu, memory, os_family, headroom=0):
    if not all(number(v) and v >= 0 for v in (cpu, memory, headroom)):
        return None
    multiplier = 1 + headroom / 100
    for size in aws_limits.FARGATE_TASK_SIZES:
        if os_family in size.operating_systems and size.vcpus >= cpu * multiplier:
            for mib in size.memory_mib:
                if mib >= memory * multiplier:
                    return {"vcpu": size.vcpus, "memory_mib": mib,
                            "requires_linux_platform_1_4": size.requires_linux_platform_1_4}
    return None


def valid_value(req, value):
    if req.type == "boolean":
        return isinstance(value, bool)
    if req.type == "number":
        return number(value) and value >= 0 and (not req.unit or
               req.unit.lower() not in ("percent", "pct") or value <= 100)
    if req.type in ("string", "enum"):
        return isinstance(value, str) and bool(value.strip()) and (req.type != "enum" or value in req.values)
    if req.type == "list":
        return isinstance(value, list) and (not req.key.startswith("nat.") or all(isinstance(v, str) and v for v in value))
    return isinstance(value, dict) and bool(value)


class CompatibilityEvaluator:
    def __init__(self, settings, now_fn=None):
        self.settings = settings
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def evaluate(self, entry, observations, *, account_id, region, resource_id,
                 subject=None, target_in_use=None, collection_issues=()):
        scope = dict(account_id=account_id, region=region, resource_id=resource_id)
        blocked = blocked_decision(self.settings, entry.id, entry.decision_setting_paths(), **scope)
        if blocked:
            return {**blocked.as_dict(), "subject": subject, "matched_rules": []}
        reasons = {}

        def missing(key, code, message):
            reasons[(key, code)] = DecisionReason(code, message, evidence_key=key)

        def value(key):
            req = entry.evidence_requirement(key)
            obs = observations.get(key)
            if not req or not isinstance(obs, dict) or obs.get("status", "available") != "available":
                code = (ReasonCode.EVIDENCE_COLLECTION_FAILED if obs and obs.get("status") == "failed"
                        else ReasonCode.ATTESTATION_EXPIRED if obs and obs.get("status") == "expired"
                        else ReasonCode.EVIDENCE_MISSING)
                missing(key, code, (obs or {}).get("message", f"Required evidence {key} is unavailable."))
                return None
            result = obs.get("value")
            if not valid_value(req, result):
                missing(key, ReasonCode.EVIDENCE_MISSING, f"{key} has an invalid type or value.")
                return None
            if req.attestation_expiry:
                when = timestamp(obs.get("observed_at"))
                now = self.now_fn()
                if when is None or when > now:
                    missing(key, ReasonCode.EVIDENCE_MISSING, f"{key} needs a timezone-aware, non-future attestation date.")
                    return None
                age = self.settings.value_of("attestation.max_age_days")
                if age is not None and (now - when).total_seconds() > age * 86400:
                    missing(key, ReasonCode.ATTESTATION_EXPIRED, f"The attestation for {key} has expired.")
                    return None
            if req.requires_metric_coverage:
                cov = obs.get("coverage") or {}
                pct, days = cov.get("coverage_pct"), cov.get("window_days")
                if (not number(pct) or not 0 <= pct <= 100 or not number(days)
                        or days < self.settings.window_minimum_days
                        or pct < self.settings.min_coverage_pct):
                    missing(key, ReasonCode.COVERAGE_BELOW_MINIMUM, f"{key} has insufficient raw metric coverage or history.")
                    return None
                if obs.get("collection_complete") is not True:
                    missing(key, ReasonCode.EVIDENCE_COLLECTION_FAILED, f"{key} metric collection was incomplete.")
                    return None
            return result

        def parameter(ref):
            if ref.source == "aws_limit":
                return {"lambda.memory_min_mb": aws_limits.LAMBDA_MEMORY_MIN_MB.value,
                        "lambda.memory_max_mb": aws_limits.LAMBDA_MEMORY_MAX_MB.value}.get(ref.ref)
            result = self.settings.value_of(ref.setting_path)
            if result is None:
                missing(ref.setting_path, ReasonCode.THRESHOLD_UNSET, f"Set {ref.setting_path} before evaluating this rule.")
            return result

        sizes = {}

        def condition(cond):
            observed = value(cond.evidence)
            related = {role: value(key) for role, key in cond.related_evidence.items()}
            if observed is None or any(v is None for v in related.values()):
                return None
            if cond.op in ("within_applicable_lambda_limit", "exceeds_applicable_lambda_limit"):
                try:
                    limit = aws_limits.applicable_lambda_limit(cond.parameter.ref.split(".")[-1], related["invocation_type"])
                except ValueError:
                    return None
                if limit.status == aws_limits.LIMIT_NOT_DETERMINABLE:
                    missing(cond.evidence, ReasonCode.EVIDENCE_MISSING, limit.note)
                    return None
                within = limit.status == aws_limits.LIMIT_NOT_APPLICABLE or observed <= limit.value
                return within if cond.op == "within_applicable_lambda_limit" else not within
            if cond.op in ("fargate_size_fits", "fargate_size_exceeds"):
                headroom = parameter(cond.headroom)
                if headroom is None:
                    return None
                size = fargate_size(observed, related["memory"], related["os_family"], headroom)
                if size:
                    sizes.update(size)
                return bool(size) if cond.op == "fargate_size_fits" else not bool(size)
            if cond.op == "is_present":
                return True
            if cond.op == "is_true":
                return observed is True
            if cond.op == "is_false":
                return observed is False
            if cond.op in ("in", "not_in"):
                return (observed in cond.values) if cond.op == "in" else (observed not in cond.values)
            operand = parameter(cond.parameter) if cond.parameter else cond.value
            if operand is None:
                return None
            return {"equals": operator.eq, "gt": operator.gt, "gte": operator.ge,
                    "lt": operator.lt, "lte": operator.le}[cond.op](observed, operand)

        rules = [r for r in entry.decision_rules if r.subject == subject]
        matches = []
        verdict = None
        # Target presence is produced from fresh AWS inventory, never from a workload input.
        if target_in_use is True:
            verdict = Verdict.ALREADY_IN_USE
        else:
            for wanted in ("already_in_use", "incompatible", "compatible"):
                for rule in rules:
                    states = [condition(c) for c in rule.conditions]
                    if not all(s is True for s in states):
                        continue
                    if wanted != rule.verdict:
                        continue
                    if wanted == "compatible":
                        required = [e for e in entry.evidence_requirements
                                    if e.role == "required" and e.subject in (None, subject)]
                        known = [value(e.key) is not None for e in required]
                        if not all(known) or collection_issues or target_in_use is None:
                            continue
                    matches.append({"id": rule.id, "description": rule.description,
                                    "references": list(rule.references),
                                    "identified_evidence": {k: (observations.get(k) or {}).get("value")
                                                            for k in rule.identifies}})
                if matches:
                    verdict = Verdict(wanted)
                    break
        if verdict is None:
            if target_in_use is None:
                missing("target.presence", ReasonCode.EVIDENCE_MISSING, "Target usage could not be fully checked in the inspected scope.")
            for issue in collection_issues:
                missing("collection", ReasonCode.EVIDENCE_COLLECTION_FAILED, str(issue))
            if not reasons:
                missing("rules", ReasonCode.EVIDENCE_MISSING, "No catalog compatibility rule was satisfied by the observed workload.")
            result = insufficient_data(entry.id, tuple(reasons.values()), **scope)
        else:
            result = AlternativeDecision(entry.id, verdict, **scope)
        return {**result.as_dict(), "subject": subject, "matched_rules": matches,
                "fargate_size": sizes or None,
                "evidence": {k: v for k, v in observations.items()
                             if (entry.evidence_requirement(k) and entry.evidence_requirement(k).subject in (None, subject))},
                "sources": [{"id": r.id, "url": r.url, "verified_on": r.verified_on} for r in entry.references]}
