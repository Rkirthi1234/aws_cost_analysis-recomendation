"""Alternatives evidence tools (read-only, opt-in).

Exposes the Phase 1/2 modules to the Strands agent as three tools:

* ``identify_high_cost_services``  - Cost Explorer screening via ``HighCostServiceIdentifier``
* ``collect_usage_evidence``       - inventory + CloudWatch metrics + usage patterns via
                                     ``UsageEvidenceCollector``
* ``collect_resource_inventory``   - compact resource-level inventory via ``InventoryCollector``

The tools are EVIDENCE GATHERING only. They never recommend an alternative, estimate savings or
evaluate compatibility; that is Phase 3 work.

Safety properties:

* They are registered only when ``alternatives.enabled`` is true (default: false), see
  ``get_alternatives_tools``.
* All AWS access (inventory, metrics and the Cost Explorer screen) goes through
  ``ReadOnlySessionManager`` (allowlisted List/Describe/Get calls). Only accounts present in the
  account registry can be targeted.
* Collectors are built per call so assumed-role credentials are refreshed by ``SessionManager``
  and no AWS client is cached here.
* Detailed resources are capped (default 25 per response, shared across accounts/regions and
  covering resources and RDS clusters; nested lists such as event source mappings and ECS
  container-instance ids are capped too) while aggregate counts, warnings, errors, missing
  permissions and evidence gaps are always preserved.
* AWS-controlled strings (names, tag values) are sanitized and truncated. Role/caller ARNs,
  external IDs, access keys and the stream marker ``[BLOCK_END]`` are redacted from every string
  that reaches the model, including error messages.
"""
import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import ClientError
from strands import tool

from .base_tool import BaseTool
from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.high_cost_identifier import HighCostServiceIdentifier
from agent.services.inventory_collector import DEFAULT_MAX_RESOURCES, InventoryCollector
from agent.services.readonly_guard import ReadOnlySessionManager, ReadOnlyViolation
from agent.services.usage_evidence import EVIDENCE_PLANS, UsageEvidenceCollector
from agent.services.usage_pattern_config import UsagePatternSettings, load_usage_pattern_settings

logger = logging.getLogger("AlternativesTools")

CONFIG_FLAG = "alternatives.enabled"

MAX_DETAIL_RESOURCES = 25   # approved: detailed resources per response
MAX_REGIONS = 3             # safe region limit per call
MAX_ACCOUNT_WORKERS = 4

RESOURCE_TYPES = ("ec2", "lambda", "ecs", "rds", "nat_gateway")

NAME_MAX_CHARS = 64         # AWS names / tag values
ID_MAX_CHARS = 128          # identifiers
TEXT_MAX_CHARS = 500        # any other string

# inventory envelope resource_type -> field holding the resource identifier
_ID_FIELD = {
    "ec2_instance": "instance_id",
    "lambda_function": "function_name",
    "rds_instance": "db_instance_id",
    "nat_gateway": "nat_gateway_id",
    "ecs_cluster": "cluster_name",
}
_NAME_FIELDS = frozenset({"name", "autoscaling_group"})
_IDENTIFIER_FIELDS = frozenset(_ID_FIELD.values()) | {"cluster_arn"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

# Sensitive text that must never reach the model, even inside AWS/botocore error messages.
_BLOCK_END_MARKER = "[BLOCK_END]"
_REDACTIONS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    # IAM / STS ARNs (role ARNs, caller identities such as assumed-role sessions)
    (re.compile(r"arn:aws[a-z-]*:(?:iam|sts)::\d{12}:[^\s'\",;)\]]+"), "[redacted-arn]"),
    # botocore ParamValidationError echoes the offending value: "parameter ExternalId, value: xyz"
    (re.compile(r"(?i)(parameter\s+\w+\s*,\s*value:\s*)[^,\n]+"), r"\1[redacted]"),
    # key=value / key: value forms for secrets
    (re.compile(
        r"(?i)\b(externalid|secretaccesskey|sessiontoken|aws_secret_access_key|aws_session_token)\b"
        r"(\s*[=:]\s*)[^\s,;)]+"
    ), r"\1\2[redacted]"),
    # access key ids
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[redacted-key]"),
)

# Per-resource usage fields kept in compact output.
_USAGE_STAT_KEYS = (
    "count", "mean", "p50", "p95", "max", "cv", "active_hour_fraction", "peak_to_mean",
    "hour_of_day_eta_squared", "coverage_pct", "peak_cpu_pct", "top_hours_utc",
)
_METRIC_SUMMARY_KEYS = ("count", "mean", "p95", "max")

_STATUS_PRECEDENCE = (
    "read_only_violation",
    "partial_missing_permissions",
    "partial_errors",
    "failed",
    "evidence_collected",
    "no_resources_found",
)


# ------------------------------------------------------------------ sanitizing
def _clean(text: Any, limit: int) -> str:
    """Redact secrets, strip control characters, remove the stream marker, collapse whitespace, truncate."""
    raw = str(text)
    for pattern, replacement in _REDACTIONS:
        raw = pattern.sub(replacement, raw)
    cleaned = " ".join(_CONTROL_RE.sub(" ", raw).split())
    while _BLOCK_END_MARKER in cleaned:  # loop: removal must not re-assemble a marker
        cleaned = cleaned.replace(_BLOCK_END_MARKER, "")
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _sanitize(value: Any, key: Optional[str] = None) -> Any:
    """Make a structure safe and JSON-clean for the model.

    Strings are stripped of control characters and truncated (tighter for names and tag values);
    non-finite floats become None; unknown types become strings.
    """
    if isinstance(value, dict):
        return {_clean(k, ID_MAX_CHARS): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_sanitize(v, key) for v in items]
    if isinstance(value, str):
        if key in _NAME_FIELDS:
            return _clean(value, NAME_MAX_CHARS)
        if key in _IDENTIFIER_FIELDS:
            return _clean(value, ID_MAX_CHARS)
        return _clean(value, TEXT_MAX_CHARS)
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return _clean(value, TEXT_MAX_CHARS)


def _dump(result: Dict[str, Any]) -> str:
    return json.dumps(_sanitize(result), default=str)


def _error(api: str, code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """Error shape used by BillingTools._call_api: error / api / code / message."""
    return {"error": True, "api": api, "code": code, "message": _clean(message, TEXT_MAX_CHARS), **extra}


def _exception_error(api: str, exc: Exception) -> Dict[str, Any]:
    """BillingTools._call_api convention: ClientError -> code/message, anything else -> exception_type/message."""
    if isinstance(exc, ClientError):
        response = getattr(exc, "response", None)
        err = response.get("Error", {}) if isinstance(response, dict) else {}
        return {
            "error": True,
            "api": api,
            "code": _clean(err.get("Code") or "ClientError", ID_MAX_CHARS),
            "message": _clean(err.get("Message") or exc, TEXT_MAX_CHARS),
        }
    return {
        "error": True,
        "api": api,
        "exception_type": type(exc).__name__,
        "message": _clean(exc, TEXT_MAX_CHARS),
    }


# ----------------------------------------------------------------- validation
def _validate_dates(start_date: Any, end_date: Any, api: str) -> Optional[Dict[str, Any]]:
    parsed: Dict[str, date] = {}
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if not isinstance(value, str) or not _DATE_RE.match(value):
            return _error(api, "InvalidDate", f"{label} must be YYYY-MM-DD, got {_clean(value, 40)!r}")
        try:
            parsed[label] = date.fromisoformat(value)
        except ValueError:
            return _error(api, "InvalidDate", f"{label} is not a real calendar date: {_clean(value, 40)!r}")
    if parsed["start_date"] >= parsed["end_date"]:
        return _error(
            api, "InvalidDateRange",
            "start_date must be before end_date (end_date is exclusive: use the first day of the "
            "following period, e.g. 2026-11-01 for all of October 2026)",
        )
    return None


def _inclusive_end_warning(end_date: str) -> Optional[str]:
    """Warn when end_date looks like an inclusive last day of a month (it would drop that day)."""
    end = date.fromisoformat(end_date)
    if end.day != 1 and (end + timedelta(days=1)).day == 1:
        return (
            f"end_date {end_date} is the last day of a month. Cost Explorer end dates are EXCLUSIVE, so "
            f"{end_date} itself is not included; use {(end + timedelta(days=1)).isoformat()} to cover the whole month."
        )
    return None


def _allocate(budget: int, counts: Sequence[int]) -> List[int]:
    """Share ``budget`` detail slots fairly across sections with the given resource counts."""
    alloc = [0] * len(counts)
    remaining = budget
    active = [i for i, c in enumerate(counts) if c > 0]
    while active and remaining > 0:
        share = remaining // len(active)
        if share == 0:
            for i in active[:remaining]:
                alloc[i] += 1
            break
        done = [i for i in active if counts[i] - alloc[i] <= share]
        if done:
            for i in done:
                remaining -= counts[i] - alloc[i]
                alloc[i] = counts[i]
            active = [i for i in active if i not in done]
        else:
            for i in active:
                alloc[i] += share
            remaining -= share * len(active)
            for i in active[:remaining]:
                alloc[i] += 1
            break
    return alloc


def _regions_of(result: Any) -> List[Dict[str, Any]]:
    """Region sections of an evidence result; anything that is not a dict is ignored."""
    regions = result.get("regions") if isinstance(result, dict) else None
    return [rr for rr in regions if isinstance(rr, dict)] if isinstance(regions, list) else []


def _detail_count(inv: Any) -> int:
    """Detailed items in an inventory that compete for the response-wide detail budget:
    resources plus RDS clusters."""
    if not isinstance(inv, dict):
        return 0
    resources, clusters = inv.get("resources"), inv.get("clusters")
    return (len(resources) if isinstance(resources, list) else 0) + (len(clusters) if isinstance(clusters, list) else 0)


def _dedupe_errors(errors: Any) -> Any:
    """Drop exact duplicate error entries (first occurrence kept, in order); count repeats."""
    if not isinstance(errors, list):
        return errors
    unique: Dict[str, Dict[str, Any]] = {}
    for err in errors:
        if not isinstance(err, dict):
            unique.setdefault(repr(err), err)
            continue
        key = json.dumps(err, sort_keys=True, default=str)
        if key in unique:
            unique[key]["occurrences"] = unique[key].get("occurrences", 1) + 1
        else:
            unique[key] = dict(err)
    return list(unique.values())


# ------------------------------------------------------------------ the tools
class AlternativesTools(BaseTool):
    """Evidence-gathering tools for the alternative-service workflow (read-only)."""

    def __init__(
        self,
        session_manager: Any,
        account_registry: Any,
        catalog: AlternativesCatalog,
        usage_settings: Optional[UsagePatternSettings] = None,
        *,
        max_detail_resources: int = MAX_DETAIL_RESOURCES,
        max_regions: int = MAX_REGIONS,
        max_inventory_resources: int = DEFAULT_MAX_RESOURCES,
        max_account_workers: int = MAX_ACCOUNT_WORKERS,
        identifier_factory: Optional[Callable[[], Any]] = None,
        evidence_factory: Optional[Callable[[], Any]] = None,
        inventory_factory: Optional[Callable[[], Any]] = None,
    ):
        if max_detail_resources < 1 or max_regions < 1 or max_inventory_resources < 1 or max_account_workers < 1:
            raise ValueError("limits must be >= 1")
        self._sm = session_manager
        self._registry = account_registry
        self._catalog = catalog
        self._settings = usage_settings or load_usage_pattern_settings(None)
        self._max_detail = int(max_detail_resources)
        self._max_regions = int(max_regions)
        self._max_inventory = int(max_inventory_resources)
        self._max_workers = int(max_account_workers)
        self._identifier_factory = identifier_factory
        self._evidence_factory = evidence_factory
        self._inventory_factory = inventory_factory

    def get_tools(self) -> List[Callable]:
        return [
            self.identify_high_cost_services,
            self.collect_usage_evidence,
            self.collect_resource_inventory,
        ]

    # ------------------------------------------------- tool 1: high-cost services
    @tool
    def identify_high_cost_services(self, start_date: str, end_date: str) -> str:
        """Identify high-cost AWS services and which approved alternative-service candidates apply.

        FIRST step of the alternatives evidence workflow. Cost-based screening only: it does NOT
        evaluate usage, compatibility or savings, and a high cost alone is never a reason to recommend
        an alternative. Use the returned alternative_id values with collect_usage_evidence.

        Args:
            start_date: Inclusive start date, YYYY-MM-DD (e.g. 2026-10-01).
            end_date: EXCLUSIVE end date, YYYY-MM-DD. For a full month use the first day of the next
                month (e.g. 2026-11-01 for October 2026), NOT the last day of the month.

        Returns:
            JSON with high_cost_services, candidates (alternative_id, scoped cost, required evidence),
            not_applicable, warnings and configured_accounts (account id, type, region).
        """
        api = "identify_high_cost_services"
        problem = _validate_dates(start_date, end_date, api)
        if problem:
            return _dump(problem)
        try:
            result = self._make_identifier().identify(start_date, end_date)
        except ReadOnlyViolation as e:
            return _dump(_error(api, "ReadOnlyViolation", str(e)))
        except Exception as e:  # never let the agent tool crash
            logger.exception("identify_high_cost_services failed")
            return _dump(_exception_error(api, e))

        if not isinstance(result, dict):
            return _dump(_error(api, "UnexpectedResult", "High-cost identification returned no result"))
        if not result.get("error"):
            result = dict(result)
            warning = _inclusive_end_warning(end_date)
            if warning:
                result["warnings"] = list(result.get("warnings", [])) + [warning]
            result["configured_accounts"] = self._configured_accounts()
            result["next_step"] = (
                "For each candidate, call collect_usage_evidence with its alternative_id. "
                "No recommendation may be made until workload evidence has been collected and reviewed."
            )
        return _dump(result)

    # ------------------------------------------------- tool 2: usage evidence
    @tool
    def collect_usage_evidence(
        self,
        alternative_id: str,
        account_id: Optional[str] = None,
        regions: Optional[List[str]] = None,
    ) -> str:
        """Collect read-only workload evidence for one approved alternative candidate.

        SECOND step of the alternatives evidence workflow. Gathers the resource inventory, CloudWatch
        usage metrics and usage-pattern classification (idle / steady / diurnal / bursty / variable /
        insufficient_data) for the resources the alternative applies to, and lists evidence gaps. It
        does NOT evaluate compatibility, assign confidence, recommend anything or estimate savings.
        Detailed per-resource output is limited to 25 resources per response; aggregate counts cover
        all collected resources. Warnings, errors, missing permissions and evidence gaps are always kept.

        Args:
            alternative_id: An alternative_id from identify_high_cost_services candidates.
            account_id: Optional configured AWS account ID. When omitted, ALL configured accounts are analysed.
            regions: Optional list of up to 3 regions (e.g. ["us-east-1"]). When omitted, each account's
                configured region is used.

        Returns:
            JSON evidence per account and region: inventory, metrics summary, usage patterns,
            evidence_gaps, missing_permissions, errors and status.
        """
        api = "collect_usage_evidence"
        if not isinstance(alternative_id, str) or alternative_id not in EVIDENCE_PLANS:
            return _dump(_error(
                api, "UnknownAlternative",
                f"alternative_id {_clean(alternative_id, ID_MAX_CHARS)!r} has no evidence plan",
                supported_alternative_ids=sorted(EVIDENCE_PLANS),
            ))
        problem, account_ids = self._resolve_accounts(account_id, api)
        if problem:
            return _dump(problem)
        problem, region_list = self._validate_regions(regions, api)
        if problem:
            return _dump(problem)

        try:
            collector = self._make_evidence()
        except Exception as e:
            logger.exception("collect_usage_evidence setup failed")
            return _dump(_exception_error(api, e))

        outcomes = self._run_per_account(
            account_ids, api, lambda aid: collector.collect(alternative_id, aid, region_list)
        )

        # Detail budget is shared by every account/region section in this response.
        sections: List[Tuple[int, int]] = []
        counts: List[int] = []
        for ai, (_, result, err) in enumerate(outcomes):
            if err is None and not result.get("error"):
                for ri, rr in enumerate(_regions_of(result)):
                    sections.append((ai, ri))
                    counts.append(_detail_count(rr.get("inventory")))
        quotas = dict(zip(sections, _allocate(self._max_detail, counts)))

        accounts_out: List[Dict[str, Any]] = []
        statuses = set()
        missing: List[str] = []
        gaps: List[str] = []
        thresholds = None
        violation = False
        failed_count = 0
        for ai, (aid, result, err) in enumerate(outcomes):
            if err is None and isinstance(result, dict) and result.get("error"):
                err = _error(api, result.get("code", "CollectionFailed"), result.get("message", "Evidence collection failed"))
            if err is not None:
                if err.get("code") == "ReadOnlyViolation":
                    violation = True
                failed_count += 1
                accounts_out.append({"account_id": aid, "status": "failed", "error": err})
                continue
            compact = self._compact_evidence(result, ai, quotas)
            accounts_out.append(compact)
            statuses.add(result.get("status", "evidence_collected"))
            missing.extend(result.get("missing_permissions", []))
            gaps.extend(result.get("evidence_gaps", []))
            thresholds = thresholds or result.get("thresholds_used")

        if failed_count and failed_count == len(accounts_out):
            statuses = {"failed"}
        elif failed_count:
            statuses.add("partial_errors")  # some accounts failed, others returned evidence
        overall = next((s for s in _STATUS_PRECEDENCE if s in statuses), "evidence_collected")
        if violation:
            overall = "read_only_violation"
        return _dump({
            "alternative_id": alternative_id,
            "scope": "single_account" if account_id else "all_configured_accounts",
            "accounts_analyzed": len(accounts_out),
            "regions_requested": region_list or "each account's configured region",
            "status": overall,
            "accounts": accounts_out,
            "missing_permissions": sorted(set(missing)),
            "evidence_gaps": list(dict.fromkeys(gaps)),
            "thresholds_used": thresholds,
            "limits": self._limits(),
            "compaction": self._compaction_note(),
            "note": "Descriptive evidence only. No compatibility evaluation, confidence level, "
                    "recommendation or savings figure has been produced.",
        })

    # ------------------------------------------------- tool 3: resource inventory
    @tool
    def collect_resource_inventory(
        self,
        resource_type: str,
        account_id: Optional[str] = None,
        region: Optional[str] = None,
    ) -> str:
        """Collect a compact, read-only inventory of one resource type (resource-level inspection).

        Use to inspect what exists after collect_usage_evidence, or when asked which resources exist.
        Descriptive only (List/Describe calls); no metrics, recommendations or savings. At most 25
        resources are detailed per response; aggregate counts cover everything collected.

        Args:
            resource_type: One of ec2, lambda, ecs, rds, nat_gateway.
            account_id: Optional configured AWS account ID. When omitted, ALL configured accounts are inspected.
            region: Optional single region (e.g. "us-east-1"). When omitted, each account's configured region is used.

        Returns:
            JSON inventory per account with summary counts, errors, warnings and missing permissions.
        """
        api = "collect_resource_inventory"
        rtype = resource_type.strip().lower() if isinstance(resource_type, str) else None
        if rtype not in RESOURCE_TYPES:
            return _dump(_error(
                api, "UnsupportedResourceType",
                f"resource_type {_clean(resource_type, 40)!r} is not supported",
                supported_resource_types=list(RESOURCE_TYPES),
            ))
        problem, account_ids = self._resolve_accounts(account_id, api)
        if problem:
            return _dump(problem)
        if region is not None and not isinstance(region, str):
            return _dump(_error(api, "InvalidRegion", "region must be a single region string"))
        problem, region_list = self._validate_regions([region] if region else None, api)
        if problem:
            return _dump(problem)
        effective_region = region_list[0] if region_list else None

        try:
            collector = self._make_inventory()
        except Exception as e:
            logger.exception("collect_resource_inventory setup failed")
            return _dump(_exception_error(api, e))

        # Resolve each account's configured region here so the result states which region was inspected
        # (the session manager would otherwise apply the same default without saying so).
        outcomes = self._run_per_account(
            account_ids, api,
            lambda aid: self._collect_inventory(collector, rtype, aid, effective_region or self._account_region(aid)),
        )
        counts = [
            _detail_count(result) if err is None and isinstance(result, dict) else 0
            for _, result, err in outcomes
        ]
        quotas = _allocate(self._max_detail, counts)

        accounts_out: List[Dict[str, Any]] = []
        missing: List[str] = []
        total = 0
        for (aid, result, err), quota in zip(outcomes, quotas):
            if err is not None:
                accounts_out.append({"account_id": aid, "status": "failed", "error": err})
                continue
            inventory = self._compact_inventory(result, None, quota)
            total += inventory.get("count", 0) or 0
            # Include ecs_errors (ECS membership lookup for the ec2 inventory) so their missing
            # permissions are reported too.
            missing.extend(
                e["missing_permission"]
                for e in [*(result.get("errors") or []), *(result.get("ecs_errors") or [])]
                if isinstance(e, dict) and e.get("missing_permission")
            )
            accounts_out.append({"account_id": aid, "inventory": inventory})

        return _dump({
            "resource_type": rtype,
            "accounts_analyzed": len(accounts_out),
            "region_requested": effective_region or "each account's configured region",
            "total_resources_collected": total,
            "accounts": accounts_out,
            "missing_permissions": sorted(set(missing)),
            "limits": self._limits(),
            "note": "Descriptive inventory only. Names and tag values are sanitized and truncated; "
                    "no recommendation or savings figure has been produced.",
        })

    # ------------------------------------------------------------ collectors
    def _make_identifier(self) -> Any:
        if self._identifier_factory:
            return self._identifier_factory()
        return HighCostServiceIdentifier(ReadOnlySessionManager(self._sm), self._registry, self._catalog)

    def _make_evidence(self) -> Any:
        if self._evidence_factory:
            return self._evidence_factory()
        return UsageEvidenceCollector(
            self._sm, self._catalog, self._registry,
            window=self._settings.window, thresholds=self._settings.thresholds,
            max_resources=self._max_inventory,
        )

    def _make_inventory(self) -> Any:
        if self._inventory_factory:
            return self._inventory_factory()
        return InventoryCollector(ReadOnlySessionManager(self._sm), max_resources=self._max_inventory)

    @staticmethod
    def _collect_inventory(collector: Any, rtype: str, account_id: str, region: Optional[str]) -> Dict[str, Any]:
        if rtype == "ec2":
            ecs = collector.collect_ecs_usage(account_id, region, include_container_instances=True)
            env = collector.collect_ec2_instances(
                account_id, region, ecs_instance_ids=ecs.get("container_instance_ec2_ids", [])
            )
            if ecs.get("errors"):
                env["warnings"] = list(env.get("warnings", [])) + [
                    "ECS container-instance membership could not be fully determined (see ecs_errors); "
                    "is_ecs_container_instance may be false for ECS hosts."
                ]
                env["ecs_errors"] = ecs["errors"]
            return env
        if rtype == "lambda":
            return collector.collect_lambda_functions(account_id, region)
        if rtype == "ecs":
            return collector.collect_ecs_usage(account_id, region, include_container_instances=True)
        if rtype == "rds":
            return collector.collect_rds(account_id, region)
        return collector.collect_nat_gateways(account_id, region)

    # ------------------------------------------------------- accounts/regions
    def _configured_accounts(self) -> List[Dict[str, Any]]:
        """Account id, type and region only. Role ARNs, external IDs and Athena config are never exposed."""
        return [
            {"account_id": e.account_id, "account_type": e.account_type, "region": e.region}
            for e in self._registry.entries.values()
        ]

    def _account_region(self, account_id: str) -> Optional[str]:
        entry = self._registry.entries.get(account_id)
        return getattr(entry, "region", None)

    def _resolve_accounts(self, account_id: Any, api: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        configured = list(self._registry.entries)
        if account_id is None or (isinstance(account_id, str) and not account_id.strip()):
            return None, configured
        if not isinstance(account_id, str) or account_id.strip() not in self._registry.entries:
            return _error(
                api, "UnknownAccount",
                f"account_id {_clean(account_id, 40)!r} is not in the configured account registry",
                configured_account_ids=configured,
            ), []
        return None, [account_id.strip()]

    def _validate_regions(self, regions: Any, api: str) -> Tuple[Optional[Dict[str, Any]], Optional[List[str]]]:
        if regions is None:
            return None, None
        if isinstance(regions, str):
            regions = regions.replace(",", " ").split()
        if not isinstance(regions, (list, tuple)):
            return _error(api, "InvalidRegion", "regions must be a list of region names"), None
        cleaned: List[str] = []
        for r in regions:
            if not isinstance(r, str) or not _REGION_RE.match(r.strip().lower()):
                return _error(api, "InvalidRegion", f"{_clean(r, 40)!r} is not a valid AWS region name (e.g. us-east-1)"), None
            cleaned.append(r.strip().lower())
        cleaned = list(dict.fromkeys(cleaned))
        if not cleaned:
            return None, None
        if len(cleaned) > self._max_regions:
            return _error(
                api, "TooManyRegions",
                f"{len(cleaned)} regions requested; at most {self._max_regions} are allowed per call",
                max_regions=self._max_regions,
            ), None
        return None, cleaned

    def _run_per_account(
        self, account_ids: Sequence[str], api: str, fn: Callable[[str], Any]
    ) -> List[Tuple[str, Any, Optional[Dict[str, Any]]]]:
        """Run ``fn`` per account (bounded parallelism, input order kept). Never raises."""

        def run(aid: str) -> Tuple[str, Any, Optional[Dict[str, Any]]]:
            try:
                result = fn(aid)
            except ReadOnlyViolation as e:
                return aid, None, _error(api, "ReadOnlyViolation", str(e))
            except Exception as e:
                logger.warning("%s failed for one account: %s", api, type(e).__name__)
                return aid, None, _exception_error(api, e)
            if not isinstance(result, dict):
                # Never index into a collector result that is not a result object.
                return aid, None, _error(
                    api, "UnexpectedResult", f"collector returned {type(result).__name__} instead of a result object"
                )
            return aid, result, None

        workers = min(self._max_workers, len(account_ids))
        if workers <= 1:
            return [run(a) for a in account_ids]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(run, account_ids))

    # ------------------------------------------------------------ compaction
    def _limits(self) -> Dict[str, Any]:
        return {
            "max_detailed_resources_per_response": self._max_detail,
            "max_regions_per_call": self._max_regions,
            "max_resources_collected_per_resource_type": self._max_inventory,
        }

    @staticmethod
    def _compaction_note() -> str:
        return (
            "Per-resource detail is limited; inventory summaries and metrics summaries cover ALL collected "
            "resources. Per-resource 'rule' and 'thresholds_used' are omitted (thresholds are echoed once at the "
            "top level). Warnings, missing permissions and evidence gaps are never removed. Errors are reported "
            "once in each account's 'errors' list (tagged with region and section; repeats are counted in "
            "'occurrences'); sections only carry 'error_count'."
        )

    def _compact_evidence(self, evidence: Dict[str, Any], account_index: int, quotas: Dict[Tuple[int, int], int]) -> Dict[str, Any]:
        out = {k: v for k, v in evidence.items() if k != "regions"}
        if isinstance(out.get("errors"), list):
            out["errors"] = _dedupe_errors(out["errors"])
        out["regions"] = [
            self._compact_region(rr, quotas.get((account_index, ri), 0))
            for ri, rr in enumerate(_regions_of(evidence))
        ]
        return out

    def _compact_region(self, rr: Dict[str, Any], quota: int) -> Dict[str, Any]:
        out = {k: v for k, v in rr.items() if k not in ("inventory", "metrics")}
        ecs = out.get("ecs")
        if isinstance(ecs, dict) and isinstance(ecs.get("container_instance_ec2_ids"), list):
            ids = ecs["container_instance_ec2_ids"]
            ecs = dict(ecs)
            ecs["container_instance_ec2_ids_total"] = len(ids)
            ecs["container_instance_ec2_ids"] = ids[: self._max_detail]
            out["ecs"] = ecs
        metrics = rr.get("metrics")
        inventory = rr.get("inventory")
        out["inventory"] = self._compact_inventory(inventory if isinstance(inventory, dict) else {}, metrics, quota)
        if isinstance(metrics, dict):
            out["metrics"] = {k: v for k, v in metrics.items() if k not in ("resources", "thresholds_used")}
            out["metrics"]["resources_with_metrics"] = len(metrics.get("resources") or {})
        # Errors are already aggregated (tagged with region and section) in the account-level
        # 'errors' list; keep only a count in each section so they are not repeated.
        for name in ("ecs", "lambda_presence", "inventory", "metrics"):
            body = out.get(name)
            if isinstance(body, dict) and isinstance(body.get("errors"), list):
                body = dict(body)
                body["error_count"] = len(body.pop("errors"))
                out[name] = body
        return out

    def _compact_inventory(self, inv: Dict[str, Any], metrics: Optional[Dict[str, Any]], quota: int) -> Dict[str, Any]:
        resources = inv.get("resources") or []
        id_field = _ID_FIELD.get(inv.get("resource_type"))
        per_resource = (metrics or {}).get("resources") or {}
        budget = max(quota, 0)

        shown: List[Dict[str, Any]] = []
        mapping_room = self._max_detail  # event source mappings are capped per section
        for r in resources[:budget]:
            item = dict(r)
            if metrics is not None and id_field:
                rid = r.get(id_field)
                item["usage"] = self._compact_usage(per_resource[rid]) if rid in per_resource else {"metrics_collected": False}
            mappings = item.get("event_source_mappings")
            if isinstance(mappings, list):
                item["event_source_mapping_count"] = len(mappings)
                item["event_source_mappings"] = mappings[:mapping_room]
                mapping_room -= len(item["event_source_mappings"])
            shown.append(item)

        out = {k: v for k, v in inv.items() if k != "resources"}
        ids = out.get("container_instance_ec2_ids")
        if isinstance(ids, list):
            out["container_instance_ec2_ids_total"] = len(ids)
            out["container_instance_ec2_ids"] = ids[: self._max_detail]
        # RDS clusters share the same detail budget as the resources (resources first).
        clusters = out.get("clusters")
        clusters_total = clusters_shown = 0
        if isinstance(clusters, list):
            clusters_total = len(clusters)
            out["clusters"] = clusters[: max(budget - len(shown), 0)]
            clusters_shown = len(out["clusters"])
            out["clusters_total"] = clusters_total
            out["clusters_omitted"] = clusters_total - clusters_shown
        for key in ("errors", "ecs_errors"):
            if isinstance(out.get(key), list):
                out[key] = _dedupe_errors(out[key])
        out["resources_total"] = len(resources)
        out["resources_shown"] = len(shown)
        out["resources_omitted"] = len(resources) - len(shown)
        out["resources"] = shown
        if out["resources_omitted"] or clusters_total > clusters_shown:
            out["detail_note"] = (
                f"Detail shown for {len(shown) + clusters_shown} of {len(resources) + clusters_total} collected "
                f"resources (limit of {self._max_detail} per response, shared across accounts and regions); "
                f"summaries cover all of them."
            )
        return out

    @staticmethod
    def _compact_usage(entry: Dict[str, Any]) -> Dict[str, Any]:
        up = entry.get("usage_pattern") or {}
        stats = up.get("stats") or {}
        out: Dict[str, Any] = {
            "metrics_available": entry.get("metrics_available", False),
            "pattern": up.get("pattern"),
            "reasons": up.get("reasons", []),
            "flags": up.get("flags", []),
            "data_sufficiency": up.get("data_sufficiency"),
            "stats": {k: stats[k] for k in _USAGE_STAT_KEYS if k in stats},
        }
        if "resource_age_days" in entry:
            out["resource_age_days"] = entry["resource_age_days"]
        metrics = entry.get("metrics")
        if isinstance(metrics, dict):
            out["metric_summaries"] = {
                name: {k: s[k] for k in _METRIC_SUMMARY_KEYS if k in s}
                for name, s in metrics.items() if isinstance(s, dict)
            }
        return out


# ------------------------------------------------------------------ registration
def is_alternatives_enabled(config: Any) -> bool:
    """True only when ``alternatives.enabled`` is the boolean true. Absent/false/invalid -> False."""
    getter = getattr(config, "get", None)
    value = getter(CONFIG_FLAG, False) if callable(getter) else False
    if value is True:
        return True
    if value is not None and not isinstance(value, bool):
        logger.warning("%s must be true or false (got %r); alternatives tools stay disabled", CONFIG_FLAG, value)
    return False


def get_alternatives_tools(
    config: Any,
    session_manager: Any,
    account_registry: Any,
    *,
    catalog: Optional[AlternativesCatalog] = None,
) -> List[Callable]:
    """Return the alternatives tools, or [] when disabled or when initialization fails.

    Never raises: a missing/invalid catalog or any other problem is logged as a warning so the
    existing agent tools are unaffected.
    """
    if not is_alternatives_enabled(config):
        return []
    try:
        instance = AlternativesTools(
            session_manager,
            account_registry,
            catalog or AlternativesCatalog.load(),
            load_usage_pattern_settings(config),
        )
        tools = instance.get_tools()
        logger.info("AlternativesTools: registered %d tools", len(tools))
        return tools
    except Exception as e:
        logger.warning(
            "Alternatives tools could not be initialized (%s: %s); continuing without them",
            type(e).__name__, e,
        )
        return []
