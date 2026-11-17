"""Offline, explicitly hypothetical EC2-to-Lambda demonstration. No AWS imports.

Example rates, rules and baseline are fixtures, never current AWS prices or
verified compatibility. Real billing snapshots are context only.
"""
import re
from decimal import Decimal

QUESTIONS = {
    "Does the application run continuously or respond to events?": "workload_type",
    "What is the maximum execution time per task?": "duration",
    "What would trigger the application on Lambda?": "trigger",
    "What are the maximum request and response sizes?": "payload",
    "What is the peak memory usage?": "memory",
    "What are the application dependencies and requirements?": "dependencies",
    "Can the application run in a container?": "container",
    "What is the CPU usage?": "cpu",
    "How many tasks run per month?": "invocations",
}


def simulation_profile(profiles):
    """Reject unsupported Q&A on the live path instead of silently ignoring it."""
    profiles = profiles or []
    for p in profiles:
        if not isinstance(p, dict):
            raise ValueError("Each workload profile must be an object.")
        if "simulated" in p and type(p["simulated"]) is not bool:
            raise ValueError("simulated must be a JSON boolean: true or false.")
        if "answers" in p and p.get("simulated") is not True:
            raise ValueError("Question-and-answer input is supported only with simulated: true. Live mode requires verified evidence records.")
    simulations = [p for p in profiles if p.get("simulated") is True]
    if not simulations:
        return None
    if len(profiles) != 1:
        raise ValueError("Use exactly one simulated profile; do not mix simulated and live workloads.")
    p = simulations[0]
    if set(p) - {"account_id", "region", "resource_id", "simulated", "answers"}:
        raise ValueError("A simulated profile accepts only account_id, region, resource_id, simulated and answers.")
    if not re.fullmatch(r"[0-9]{12}", str(p.get("account_id", ""))) or not all(
            isinstance(p.get(k), str) and p[k].strip() for k in ("region", "resource_id")):
        raise ValueError("Each simulated profile needs account_id, region and resource_id.")
    return p


def _number(text, pattern, label):
    match = re.fullmatch(pattern, text, flags=re.I)
    if not match:
        raise ValueError(f"Invalid simulated answer for {label}; use the documented question-and-answer example and units.")
    values = [Decimal(v) for v in match.groups()]
    if any(not v.is_finite() or v < 0 for v in values):
        raise ValueError(f"Invalid simulated numeric value for {label}.")
    return values


def simulate(profile):
    answers = profile.get("answers", [])
    if not isinstance(answers, list) or len(answers) > len(QUESTIONS):
        raise ValueError("answers must be a list of up to nine documented question/answer objects.")
    fields = {}
    for row in answers:
        if not isinstance(row, dict) or set(row) != {"question", "answer"}:
            raise ValueError("Each answer needs exactly question and answer.")
        key = QUESTIONS.get(row["question"]) if isinstance(row["question"], str) else None
        if not key or key in fields or not isinstance(row["answer"], str):
            raise ValueError("Unknown or duplicate question, or non-text answer. Use the documented questions.")
        fields[key] = row["answer"].strip()
    missing = [q for q, key in QUESTIONS.items() if not fields.get(key) or fields[key].lower().startswith(("unknown", "not measured", "not defined"))]
    base = {
        "simulated": True, "mode": "simulation", "label": "Simulated test — not a live AWS recommendation",
        "aws_access_checked": False, "aws_resources_changed": False,
        "scenario": "ec2-event-image-processing-to-lambda-v1",
        "recommendation": None,
        "assumptions": [
            "Answers are hypothetical user inputs, not verified measurements.",
            "Example compatibility rules assume a supported Python image-processing runtime, S3 storage, no persistent local state, and no unmet networking, security or availability needs.",
            "Example current baseline: USD 10/month; not extrapolated from the saved AWS bill.",
            "Example rates: USD 0.000020 per GB-second and USD 0.000001 per invocation; not live AWS prices.",
            "Example excludes free tier, credits, transfer, storage, logging, retries, concurrency and migration overheads.",
        ],
        "next_action": "Use verified workload evidence and current equivalent-workload pricing for a real recommendation.",
    }
    if missing:
        return {**base, "status": "simulated_needs_information", "required_information": missing,
                "reason": "The simulated scenario still needs these answers."}
    duration, = _number(fields["duration"], r"(\d+(?:\.\d+)?)\s*seconds?", "task duration")
    memory, = _number(fields["memory"], r"(\d+(?:\.\d+)?)\s*MiB(?:;.*)?", "peak memory")
    request, response = _number(fields["payload"], r"Request:\s*(\d+(?:\.\d+)?)\s*MB;\s*response:\s*(\d+(?:\.\d+)?)\s*MB(?:.*)", "payload sizes")
    invocations, = _number(fields["invocations"], r"(\d+)", "monthly tasks")
    average, peak = _number(fields["cpu"], r"Average:\s*(\d+(?:\.\d+)?)%;\s*peak:\s*(\d+(?:\.\d+)?)%(?:;.*)?", "CPU")
    if memory <= 0 or memory > 1048576 or duration <= 0 or duration > 31536000 or invocations > 10**12 or not 0 <= average <= peak <= 100:
        raise ValueError("Simulated duration, memory, invocation count or CPU range is invalid.")
    # These are named fixture rules, not a substitute for the live evidence evaluator.
    compatible = (fields["workload_type"].lower() == "responds to events" and
                  "s3" in fields["trigger"].lower() and "asynchronous" in fields["trigger"].lower() and
                  "python" in fields["dependencies"].lower() and "s3" in fields["dependencies"].lower() and
                  "no persistent local state" in fields["dependencies"].lower() and
                  duration <= 900 and memory <= 10240 and request <= 1 and response == 0)
    base["compatibility"] = {"basis": "example scenario rules and stated assumptions only",
                              "status": "compatible_in_simulation" if compatible else "not_matched_in_simulation"}
    if not compatible:
        return {**base, "status": "simulated_no_recommendation",
                "reason": "Answers do not match the short, asynchronous S3 image-processing example; no alternative was selected."}
    allocated_memory = max(Decimal(128), memory.to_integral_value(rounding="ROUND_CEILING"))
    target = (invocations * duration * allocated_memory / 1024 * Decimal("0.000020") +
              invocations * Decimal("0.000001"))
    current = Decimal(10)
    savings = current - target
    costs = {"current_monthly_cost_usd": float(current), "estimated_alternative_monthly_cost_usd": float(target),
             "estimated_monthly_savings_usd": float(savings), "estimated_savings_pct": float(savings / current * 100),
             "basis": "hypothetical monthly baseline and example rates; not AWS pricing",
             "monthly_invocations": int(invocations), "lambda_memory_mb": int(allocated_memory),
             "duration_seconds": float(duration)}
    if savings <= 0:
        return {**base, "status": "simulated_no_recommendation", "cost_comparison": costs,
                "reason": "The example target cost does not offer savings against the example baseline."}
    return {**base, "status": "simulated_recommendation", "recommendation": {
        "alternative": "AWS Lambda", **costs,
        "reason": "Under the example assumptions, short event-driven tasks fit the scenario and cost less than the hypothetical EC2 monthly baseline."}}
