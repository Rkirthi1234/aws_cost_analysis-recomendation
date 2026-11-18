"""Concise manager-facing views. Full evaluated evidence remains available on demand."""


def cost_view(report, include_details=False):
    rows = report.get("highest_cost_services", [])[:report.get("top_n", 5)]
    result = {k: report[k] for k in ("analysis_id", "status", "period", "currency", "service_count", "top_n")}
    result.update(
        ranking_basis=report.get("ranking_basis", "net_cost_legacy"),
        total_cost_before_credits_usd=report.get("total_cost_before_credits_usd"),
        total_credits_applied_usd=report.get("total_credits_applied_usd"),
        total_net_cost_usd=report.get("total_net_cost_usd", report["total_cost_usd"]),
        services_shown=len(rows), top_services=[{"service": r["service"],
            "cost_before_credits_usd": r.get("cost_before_credits_usd"),
            "credits_applied_usd": r.get("credits_applied_usd"),
            "net_cost_usd": r.get("net_cost_usd", r["cost_usd"])} for r in rows],
                  limitations=["Actual spending for the selected dates; end date is exclusive and recent billing data may lag.",
                               "Before-credit cost retains other adjustments; EC2 includes Compute and EC2 - Other."])
    if not report.get("cost_breakdown_available"):
        result["limitations"].append("This older snapshot has net costs only. Create a new cost analysis for before-credit ranking and credit amounts.")
    if include_details:
        result["details"] = {**{k: v for k, v in report.items() if k != "services"}, "services_shown": len(rows)}
    return result


def _human_requirement(reason):
    key = reason.get("evidence_key", "")
    message = reason.get("message", "")
    if "memory" in key:
        return "Measured peak memory usage over a representative period."
    if "vcpu" in key or "cpu_" in key:
        return "Measured CPU usage over a representative period."
    if key == "workload.event_driven":
        return "Whether the application runs continuously or responds to events."
    if "duration" in key:
        return "Maximum execution time per task."
    if "payload" in key:
        return "Maximum request and response sizes."
    if key == "workload.lambda_invocation_type":
        return "How the application would be triggered on Lambda."
    if key == "workload.containerizable":
        return "Whether the application can run in a container."
    if "workload_assessment" in message:
        return "Application dependencies, storage, networking, security and availability requirements."
    return message


def _priced(item):
    return {**{k: item[k] for k in ("alternative", "account_id", "region", "resource_id", "subject", "exact_costs") if item.get(k) is not None},
            "current_monthly_cost_usd": item["current_monthly_cost_usd"],
            "estimated_alternative_monthly_cost_usd": item["alternative_monthly_cost_usd"],
            "estimated_monthly_savings_usd": item["monthly_savings_usd"],
            "estimated_savings_percent": item["savings_pct"], "reason": item["reason"]}


def _brief_suggestion(item):
    """User-facing summary; the saved report retains every technical field."""
    fields = {q.get("field", "") for q in item.get("questions", [])}
    gaps = {g.get("field", "") for g in item.get("evidence_gaps", [])}
    pending = []
    if fields - {"workload.max_event_duration_seconds"}:
        pending.append("Application behavior and invocation design")
    if "workload.max_event_duration_seconds" in fields or any("memory" in k or "cpu" in k for k in gaps):
        pending.append("Complete task measurements and resource sizing")
    if "collection" in gaps:
        pending.append("Application requirements assessment")
    if "pricing" in gaps:
        pending.append("Comparable monthly costs")
    if gaps - {"collection", "pricing", "ec2.peak_memory_mib", "ec2.peak_vcpus_used"}:
        pending.append("Additional evidence checks; see details")
    result = {"service": item["service"],
              "reason": next(iter(item.get("supporting_facts", [])), item.get("reason", "Available evidence supports investigating this alternative.")),
              "validation": "provisional", "savings_status": "not_calculated",
              "estimated_monthly_savings_usd": None, "pending_checks": pending}
    for key in ("account_id", "region", "resource_id", "subject"):
        if item.get(key) is not None:
            result[key] = item[key]
    return result


def _provisional_view(report, include_details):
    result = {k: report[k] for k in ("recommendation_id", "analysis_id", "created_at", "service", "period",
              "selected_period_cost_before_credits_usd", "selected_period_credits_applied_usd", "selected_period_net_cost_usd") if k in report}
    if "workload_profile" in report:
        result["workload_profile"] = report["workload_profile"]
    result.update(status="provisional_suggestion", currency="USD", aws_resources_changed=False,
                  note="Potential alternative only; compatibility and cost savings are not fully verified.")
    if "threshold" in report:
        result["threshold"] = {k: report["threshold"][k] for k in ("status", "threshold_usd") if k in report["threshold"]}
    suggestions = [_brief_suggestion(item) for item in report.get("provisional_suggestions", [])]
    if len(suggestions) == 1:
        result["suggestion"] = suggestions[0]
    else:
        result["suggestions"] = suggestions
    result["next_action"] = "Review the pending checks before making a migration decision."
    if include_details:
        result["details"] = report
    return result


def _assessment_view(report, include_details):
    result = {k: report[k] for k in ("recommendation_id", "analysis_id", "created_at", "service", "period", "workload_profile", "selected_period_cost_before_credits_usd", "selected_period_credits_applied_usd", "selected_period_net_cost_usd") if k in report}
    result.update(status="candidates_for_assessment", currency="USD", aws_resources_changed=False,
                  note="Candidates found from AWS inventory and catalog mappings; suitability and savings are not established.")
    if "threshold" in report:
        result["threshold"] = {k: report["threshold"][k] for k in ("status", "threshold_usd") if k in report["threshold"]}
    result["candidates"] = [{k: c[k] for k in ("service", "account_id", "region", "resource_id", "subject", "validation", "automatic_facts", "questions") if k in c}
                            for c in report.get("assessment_candidates", [])]
    result["next_action"] = "Select a resource and submit only its missing owner answers in answers. Available measurements are collected automatically; unavailable measurements remain evidence gaps."
    if include_details:
        result["details"] = report
    return result


def recommendation_view(report, include_details=False):
    if report["status"] == "candidates_for_assessment":
        return _assessment_view(report, include_details)
    if report["status"] == "provisional_suggestion":
        return _provisional_view(report, include_details)
    status = report["status"]
    result = {k: report[k] for k in ("recommendation_id", "analysis_id", "created_at", "service", "period", "selected_period_cost_usd") if k in report}
    if "selected_period_cost_before_credits_usd" in report:
        result.pop("selected_period_cost_usd", None)
        result.update({k: report[k] for k in ("ranking_basis", "selected_period_cost_before_credits_usd",
                                             "selected_period_credits_applied_usd", "selected_period_net_cost_usd")})
    result.update(status=status, currency="USD", recommendation=None, aws_resources_changed=False)
    if "workload_profile" in report:
        result["workload_profile"] = report["workload_profile"]
    if "threshold" in report:
        result["threshold"] = report["threshold"]
    candidates = [c for c in report.get("candidates", []) if c["status"] != "no_resources_found"
                  or c.get("details", {}).get("errors") or c.get("details", {}).get("status") == "partial"]
    names, missing, limitations, assumptions, evaluated = [], [], [], [], []
    input_summaries = []
    for candidate in candidates:
        names.append(candidate["alternative"])
        details = candidate.get("details", {})
        for summary in details.get("input_summary", []):
            if summary not in input_summaries:
                input_summaries.append(summary)
                limitations.extend(summary.get("limitations", []))
        for err in details.get("errors", []):
            limitations.append(err.get("message", str(err)))
        limitations.extend(details.get("warnings", []))
        for rr in details.get("reports", []):
            missing.extend(_human_requirement(r) for r in rr.get("decision", {}).get("reasons", []))
            missing.extend(rr.get("decision", {}).get("configuration_issues", []))
            pricing = rr.get("cost_comparison", {})
            if pricing.get("status") == "insufficient_data":
                missing.append(pricing.get("reason", "Verified pricing and equivalent-workload inputs are needed."))
            if rr.get("status") == "recommended":
                assumptions.extend(pricing.get("assumptions", []))
            if rr.get("status") in ("incompatible", "already_in_use"):
                descriptions = [r["description"] for r in rr.get("decision", {}).get("matched_rules", [])]
                evaluated.append({"alternative": candidate["alternative"], "status": rr["status"],
                                  "resource_id": rr.get("decision", {}).get("resource_id"),
                                  "reason": "; ".join(descriptions) or "The alternative is already in use within the inspected scope."})
            elif rr.get("status") == "no_cost_advantage" and pricing.get("status") == "estimated":
                evaluated.append({"alternative": candidate["alternative"], "status": "no_cost_advantage",
                    "resource_id": rr.get("decision", {}).get("resource_id"),
                    "current_monthly_cost_usd": pricing["current_monthly_cost_usd"],
                    "estimated_alternative_monthly_cost_usd": pricing["alternative_monthly_cost_usd"],
                    "estimated_monthly_savings_usd": pricing["monthly_savings_usd"]})
        if not details.get("reports"):
            missing.extend(candidate.get("missing_information", []))
    if names:
        result["alternatives_considered"] = list(dict.fromkeys(names))
    preferred = [_priced(p) for p in report.get("preferred_recommendations", [])]
    if preferred:
        if len(preferred) == 1:
            result["recommendation"] = preferred[0]
        else:
            result["recommendations"] = preferred
        result.update(reason="Validated alternatives offer qualifying estimated savings.", next_action="Review the proposed migration for each assessed workload.")
    elif status in ("below_threshold", "thresholds_disabled", "threshold_not_configured", "invalid_configuration"):
        result.update(reason=report.get("reason"), next_action=(
            "Run cost analysis again later and request a new recommendation." if status == "below_threshold" else
            "Update the server cost_thresholds configuration, then request a new recommendation."))
    elif status == "needs_information":
        result.update(reason="Workload compatibility or pricing evidence is incomplete.",
                      required_information=list(dict.fromkeys(missing)),
                      next_action="Provide the missing workload measurements or assessments, then evaluate again.")
        if report.get("threshold", {}).get("status") == "threshold_reached":
            result["reason"] = "Threshold reached; workload compatibility or pricing information is still needed."
        elif report.get("threshold", {}).get("status") == "needs_information":
            result.update(reason=report["threshold"]["reason"], next_action="Run cost analysis again, then use the new analysis_id.")
        if not missing and limitations:
            result.update(reason="AWS evidence collection is incomplete.", next_action="Resolve the reported access or collection errors, then evaluate again.")
    elif status == "no_supported_alternative":
        result.update(reason=report.get("reason", "No supported mapping exists for this service."),
                      next_action="Review cost optimization within the existing service.")
    elif status == "no_cost_data":
        result.update(reason="No cost data was found for the selected period.", next_action="Check the billing dates and data availability.")
    else:
        if report.get("candidates") and not candidates:
            result.update(reason="No matching source resources were found in the inspected scope.", next_action="Check the selected account, region and resource.")
        else:
            result.update(reason="No validated lower-cost alternative was found for the inspected resources.",
                          next_action="Review the candidate outcomes before planning changes.")
    if input_summaries:
        result["workload_inputs_received"] = input_summaries
    if evaluated:
        result["alternative_outcomes"] = evaluated
    limitations = list(dict.fromkeys(limitations))
    if limitations:
        result["limitations"] = limitations
    if assumptions:
        result["assumptions"] = list(dict.fromkeys(assumptions))
    if "selected_period_cost_usd" in result or "selected_period_net_cost_usd" in result:
        result["cost_basis"] = "Selected-period service spending and estimated monthly resource costs cover different scopes; target usage is checked only in the inspected regions."
        if report.get("ranking_basis") == "cost_before_credits":
            result["cost_basis"] += " Before-credit ranking identifies workload cost, not immediate cash savings while credits cover charges."
    if include_details:
        result["details"] = report
    return result
