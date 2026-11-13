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
    return {**{k: item[k] for k in ("alternative", "account_id", "region", "resource_id", "subject") if item.get(k) is not None},
            "current_monthly_cost_usd": item["current_monthly_cost_usd"],
            "estimated_alternative_monthly_cost_usd": item["alternative_monthly_cost_usd"],
            "estimated_monthly_savings_usd": item["monthly_savings_usd"],
            "estimated_savings_percent": item["savings_pct"], "reason": item["reason"]}


def recommendation_view(report, include_details=False):
    status = report["status"]
    result = {k: report[k] for k in ("recommendation_id", "analysis_id", "created_at", "service", "period", "selected_period_cost_usd") if k in report}
    if "selected_period_cost_before_credits_usd" in report:
        result.pop("selected_period_cost_usd", None)
        result.update({k: report[k] for k in ("ranking_basis", "selected_period_cost_before_credits_usd",
                                             "selected_period_credits_applied_usd", "selected_period_net_cost_usd")})
    result.update(status=status, currency="USD", recommendation=None, aws_resources_changed=False)
    candidates = [c for c in report.get("candidates", []) if c["status"] != "no_resources_found"
                  or c.get("details", {}).get("errors") or c.get("details", {}).get("status") == "partial"]
    names, missing, limitations, assumptions, evaluated = [], [], [], [], []
    for candidate in candidates:
        names.append(candidate["alternative"])
        details = candidate.get("details", {})
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
    elif status == "needs_information":
        result.update(reason="Workload compatibility or pricing evidence is incomplete.",
                      required_information=list(dict.fromkeys(missing)),
                      next_action="Provide the missing workload measurements or assessments, then evaluate again.")
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
