"""Connect scoped owner statements and local test reports to live evaluation.

No sample rates, benchmarks, coverage or approvals are manufactured. Local test
observations are context only; the existing compatibility gates remain intact.
"""
import math
from copy import deepcopy

from agent.services.compatibility_evaluator import timestamp

APPLICATION_FIELDS = {
    "event_driven": ("workload.event_driven", bool),
    "lambda_invocation_type": ("workload.lambda_invocation_type", str),
    "max_event_duration_seconds": ("workload.max_event_duration_seconds", float),
    "max_request_payload_mb": ("workload.max_request_payload_mb", float),
    "max_response_payload_mb": ("workload.max_response_payload_mb", float),
    "containerizable": ("workload.containerizable", bool),
}
CONTEXT_FIELDS = {"trigger", "input_file_bytes", "monthly_runs_min", "monthly_runs_max", "requirements"}


def normalize_live_profiles(profiles, now):
    if profiles is None:
        return [], []
    if not isinstance(profiles, list) or len(profiles) > 200:
        raise ValueError("workload_inputs must be a list of at most 200 scoped profiles.")
    normalized, summaries = [], []
    for original in profiles:
        if not isinstance(original, dict):
            raise ValueError("Each workload profile must be an object.")
        if "simulated" in original and original["simulated"] is not False:
            raise ValueError("This branch is live-only. Use simulation-demotest for simulated workloads.")
        if "answers" in original:
            raise ValueError("Live mode accepts typed application fields or verified evidence records, not free-text answers. See examples/live-word-count-request.json.")
        allowed = {"account_id", "region", "resource_id", "simulated", "application", "measurement_report", "evidence", "pricing", "workload_assessment"}
        if set(original) - allowed:
            raise ValueError("Unsupported live profile fields. Use application, measurement_report, evidence, pricing or workload_assessment.")
        p = deepcopy(original)
        for key in ("account_id", "region", "resource_id"):
            if not isinstance(p.get(key), str) or not p[key].strip():
                raise ValueError("Each profile needs account_id, region and resource_id.")
        scope = {k: p[k] for k in ("account_id", "region", "resource_id")}
        summary = {**scope, "mode": "live", "limitations": []}
        application = p.pop("application", None)
        if application is not None:
            if not isinstance(application, dict) or set(application) - set(APPLICATION_FIELDS) - CONTEXT_FIELDS - {"source", "observed_at"}:
                raise ValueError("Unknown live application field. Use the documented typed application schema.")
            when = timestamp(application.get("observed_at"))
            source = application.get("source")
            if when is None or when > now or not isinstance(source, str) or not source.strip():
                raise ValueError("Live application statements need a source and non-future timezone-aware observed_at.")
            if application.get("trigger") is not None and application["trigger"] not in ("manual", "event", "schedule", "continuous"):
                raise ValueError("application.trigger must be manual, event, schedule or continuous.")
            evidence = p.setdefault("evidence", {})
            if not isinstance(evidence, dict):
                raise ValueError("evidence must be an object.")
            for field, (key, kind) in APPLICATION_FIELDS.items():
                value = application.get(field)
                if value is None:
                    continue
                valid = type(value) is kind if kind is bool else isinstance(value, str) if kind is str else (
                    isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0)
                if not valid or (field == "lambda_invocation_type" and value not in ("synchronous", "asynchronous", "event_source_mapping")):
                    raise ValueError(f"Invalid application.{field}.")
                if key in evidence:
                    raise ValueError(f"Do not supply {field} and evidence.{key} together.")
                evidence[key] = {**scope, "observed_at": application["observed_at"], "source": source, "value": value}
            for field in ("input_file_bytes", "monthly_runs_min", "monthly_runs_max"):
                value = application.get(field)
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError(f"application.{field} must be a nonnegative integer or null.")
            low, high = application.get("monthly_runs_min"), application.get("monthly_runs_max")
            if low is not None and high is not None and low > high:
                raise ValueError("monthly_runs_min cannot exceed monthly_runs_max.")
            summary["application"] = {k: v for k, v in application.items() if v is not None}
            if low is not None or high is not None:
                summary["limitations"].append("Monthly runs are a user-stated plan/range, not a measured invocation count; no single count or continuous-running EC2 cost is assumed.")
            if application.get("trigger") == "manual":
                summary["limitations"].append("Manual execution does not specify the target invocation type or request payload; confirm those separately.")
        measurement = p.pop("measurement_report", None)
        if measurement is not None:
            if not isinstance(measurement, dict) or measurement.get("simulated") is not False or measurement.get("resource_id") != scope["resource_id"]:
                raise ValueError("measurement_report must be non-simulated and match the resource_id.")
            when = timestamp(measurement.get("started_at"))
            if when is None or when > now or not isinstance(measurement.get("measurement_scope"), str) or not measurement["measurement_scope"].strip():
                raise ValueError("measurement_report needs a non-future started_at and explicit measurement_scope.")
            names = ("input_file_bytes", "word_count", "processing_duration_seconds", "processing_cpu_seconds", "python_process_peak_memory_mib")
            for key in names:
                v = measurement.get(key)
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0:
                    raise ValueError(f"measurement_report.{key} must be a finite nonnegative number.")
            summary["measurement_report"] = {k: measurement[k] for k in ("started_at", "measurement_scope", *names)}
            summary["limitations"].append("A local test's Python RSS and processing time do not establish instance-wide memory, maximum event duration, S3 transfer overheads, Lambda memory allocation or billed duration.")
        if application is not None or measurement is not None:
            summaries.append(summary)
        normalized.append(p)
    return normalized, summaries
