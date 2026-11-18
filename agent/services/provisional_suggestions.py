"""Evidence-backed candidates, separate from validated migration recommendations.

No prices, savings, compatibility approvals or target deployments are invented.
A catalog mapping alone is never sufficient positive evidence.
"""
import math

from agent.services import aws_limits


def _value(evidence, key):
    item = evidence.get(key, {})
    return item.get("value") if item.get("status") == "available" else None


def _positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _support(entry, decision, summaries):
    evidence = decision.get("evidence", {})
    if decision.get("verdict") == "compatible":
        return ["The existing compatibility rules passed; equivalent-workload pricing is still incomplete."]
    eid = entry.id
    if eid == "ec2-short-event-driven-to-lambda":
        event_driven = _value(evidence, "workload.event_driven")
        duration = _value(evidence, "workload.max_event_duration_seconds")
        memory = _value(evidence, "ec2.peak_memory_mib")
        if (event_driven is False or
                (_positive_number(duration) and duration > aws_limits.LAMBDA_TIMEOUT_MAX_SECONDS.value) or
                (_positive_number(memory) and memory > aws_limits.LAMBDA_MEMORY_MAX_MB.value)):
            # This provisional heuristic covers the standard Lambda candidate,
            # never assumes special extended-timeout configurations.
            return []
        if event_driven is True:
            return ["The workload owner identifies discrete event-driven work; Lambda is a candidate for further assessment."]
        for summary in summaries:
            if any(summary.get(k) != decision.get(k) for k in ("account_id", "region", "resource_id")):
                continue
            application, measured = summary.get("application", {}), summary.get("measurement_report", {})
            if (application.get("trigger") in ("manual", "event", "schedule") and
                    _positive_number(application.get("monthly_runs_max"))):
                facts = [f"The owner plans {application['trigger']} discrete jobs, between {application.get('monthly_runs_min', 'an unspecified minimum')} and {application['monthly_runs_max']} runs per month."]
                if _positive_number(measured.get("input_file_bytes")) and _positive_number(measured.get("processing_duration_seconds")):
                    facts.append("An actual local processing test exists. Its scope excludes full-runtime overheads, so it supports investigating Lambda, not verified compatibility or a cost estimate.")
                else:
                    facts.append("This stated job pattern supports investigating Lambda; complete-runtime measurements, compatibility and cost advantage remain unverified.")
                return facts
    elif eid == "ec2-containerized-to-fargate":
        if _value(evidence, "workload.containerizable") is True:
            return ["The owner confirms the EC2 application can run in a container; managed Fargate tasks are a candidate pending sizing, requirements and pricing."]
    elif eid == "lambda-steady-high-volume-to-fargate":
        if (_value(evidence, "workload.containerizable") is True and
                _value(evidence, "workload.irreplaceable_lambda_behavior") is not True and
                _positive_number(_value(evidence, "lambda.monthly_invocations"))):
            return ["Container compatibility and measured Lambda invocation activity support investigating Fargate; volume thresholds, trigger replacement and cost advantage still need validation."]
    elif eid == "rds-mysql-postgres-to-aurora-serverless-v2":
        mapping = _value(evidence, "rds.aurora_version_mapping")
        if isinstance(mapping, dict) and mapping.get("migration_assessed") is True and _value(evidence, "rds.engine") in ("mysql", "postgres"):
            return ["An assessed database engine/version mapping supports investigating Aurora Serverless v2; capacity, availability and equivalent-workload costs remain unverified."]
    elif eid == "nat-gateway-to-gateway-endpoints":
        subject = decision.get("subject")
        if subject in ("s3", "dynamodb") and _positive_number(_value(evidence, f"nat.{subject}.same_region_bytes")):
            return [f"Attributed same-region {subject} traffic supports investigating a gateway endpoint; routing coverage and avoidable processing costs still need validation."]
    return []


def build_provisional_suggestions(candidates, catalog):
    entries = {e.id: e for e in catalog.approved_entries()}
    suggestions = []
    for candidate in candidates:
        entry = entries.get(candidate.get("alternative_id"))
        details = candidate.get("details", {})
        # Unknown scope from a failed/truncated collector is not a valid basis.
        if entry is None or details.get("errors") or details.get("status") == "partial":
            continue
        for report in details.get("reports", []):
            decision = report.get("decision", {})
            if (report.get("status") != "needs_information" or
                    decision.get("verdict") not in ("insufficient_data", "compatible") or
                    decision.get("configuration_issues") or
                    any(rule.get("verdict") in ("incompatible", "already_in_use") for rule in decision.get("matched_rules", []))):
                continue
            reasons = decision.get("reasons", [])
            if any(r.get("code") in ("configuration_invalid", "attestation_expired") for r in reasons):
                continue
            support = _support(entry, decision, details.get("input_summary", []))
            if not support:
                continue
            questions, gaps = [], []
            for reason in reasons:
                key = reason.get("evidence_key")
                req = entry.evidence_requirement(key) if isinstance(key, str) else None
                if req and req.origin == "attestation":
                    question = {"field": key, "type": req.type, "question": req.description,
                                "source": "user_or_application_owner",
                                "input_path": f'workload_inputs[].evidence["{key}"].value'}
                    if req.values:
                        question["allowed_values"] = list(req.values)
                    if req.unit:
                        question["unit"] = req.unit
                    if question not in questions:
                        questions.append(question)
                else:
                    gap = {"field": key, "message": reason.get("message", "Evidence is incomplete."),
                           "source": "requirements_assessment" if key == "collection" else "measurement_or_collection"}
                    if gap not in gaps:
                        gaps.append(gap)
            pricing = report.get("cost_comparison", {})
            gaps.append({"field": "pricing", "message": pricing.get("reason") or "Equivalent-workload pricing is incomplete.",
                         "source": "resource_cost_and_aws_pricing"})
            suggestion = {
                "alternative_id": entry.id, "service": entry.alternative_name,
                **{k: decision[k] for k in ("account_id", "region", "resource_id", "subject") if decision.get(k) is not None},
                "label": "Potential alternative — not a validated migration recommendation",
                "reason": " ".join(support), "supporting_facts": support,
                "compatibility": "passed_existing_rules" if decision["verdict"] == "compatible" else "not_fully_verified",
                "estimated_monthly_cost_usd": None, "estimated_monthly_savings_usd": None,
                "estimated_savings_pct": None, "cost_advantage": "not_established",
                "questions": questions, "evidence_gaps": gaps,
                "next_action": "Answer only the candidate-specific questions and resolve the listed evidence gaps before any migration or savings claim.",
            }
            if suggestion not in suggestions:
                suggestions.append(suggestion)
    return suggestions
