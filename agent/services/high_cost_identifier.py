"""High-cost service identification for the alternatives feature.

Read-only. Uses Cost Explorer through the payer account (via the existing
SessionManager) to find which services are expensive and which approved catalog
entries are cost-relevant. It does NOT evaluate compatibility, usage patterns or
savings; those belong to later phases.

Cost Explorer ``End`` dates are exclusive.
"""
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import ClientError

from agent.services.alternatives_catalog import AlternativeEntry, AlternativesCatalog

logger = logging.getLogger(__name__)

MAX_PAGES = 50
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Placeholder defaults pending approval; every call reports the rule it applied.
DEFAULT_TOP_N = 5
DEFAULT_MIN_COST_USD = 0.0
DEFAULT_METRIC = "AmortizedCost"  # matches the existing system prompt default
DEFAULT_EXCLUDED_SERVICES = ("Tax",)

_CENT = Decimal("0.01")


class HighCostServiceIdentifier:
    """Ranks services by cost and maps high-cost ones to approved catalog entries."""

    def __init__(
        self,
        session_manager: Any = None,
        account_registry: Any = None,
        catalog: Optional[AlternativesCatalog] = None,
        *,
        top_n: int = DEFAULT_TOP_N,
        min_cost_usd: float = DEFAULT_MIN_COST_USD,
        metric: str = DEFAULT_METRIC,
        excluded_services: Sequence[str] = DEFAULT_EXCLUDED_SERVICES,
        ce_client: Any = None,
    ):
        if catalog is None:
            raise ValueError("HighCostServiceIdentifier requires an AlternativesCatalog")
        if top_n < 1:
            raise ValueError("top_n must be >= 1")
        if min_cost_usd < 0:
            raise ValueError("min_cost_usd must be >= 0")

        self._catalog = catalog
        self._top_n = int(top_n)
        self._min_cost = Decimal(str(min_cost_usd))
        self._metric = metric
        self._excluded = {s.casefold() for s in excluded_services}
        self._excluded_display = tuple(excluded_services)
        self._ce = ce_client
        self._session_manager = session_manager
        self._payer_account_id: Optional[str] = None

        if ce_client is None:
            if session_manager is None or account_registry is None:
                raise ValueError("session_manager and account_registry are required without ce_client")
            payer = account_registry.get_payer_account()
            if payer is None:
                raise ValueError(
                    "HighCostServiceIdentifier requires a payer account. "
                    "Configure an account with account_type 'payer' in config.yaml."
                )
            self._payer_account_id = payer.account_id

    # ------------------------------------------------------------------ public
    def identify(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Identify high-cost services and cost-relevant catalog candidates.

        Args:
            start_date: Inclusive start, YYYY-MM-DD.
            end_date: Exclusive end, YYYY-MM-DD.

        Returns a JSON-serializable dict. On failure returns
        ``{"error": True, "code": ..., "message": ...}`` instead of raising.
        """
        problem = self._validate_dates(start_date, end_date)
        if problem:
            return problem

        warnings: List[str] = []
        try:
            rows = self._fetch(start_date, end_date, ["SERVICE", "USAGE_TYPE"], None, warnings)
        except ClientError as e:
            return self._client_error(e, "get_cost_and_usage")
        except Exception as e:  # unexpected: never let the agent tool crash
            logger.exception("High-cost identification failed")
            return {"error": True, "api": "get_cost_and_usage",
                    "exception_type": type(e).__name__, "message": str(e)}

        service_cost: Dict[str, Decimal] = {}
        usage_cost: Dict[Tuple[str, str], Decimal] = {}
        for keys, amount, unit in rows:
            if len(keys) != 2:
                continue
            service, usage_type = keys
            if unit != "USD":
                note = f"Non-USD unit '{unit}' returned for {service}; amounts are not converted"
                if note not in warnings:  # one warning per service/unit, not one per usage-type row
                    warnings.append(note)
            service_cost[service] = service_cost.get(service, Decimal(0)) + amount
            usage_cost[(service, usage_type)] = usage_cost.get((service, usage_type), Decimal(0)) + amount

        ranked = sorted(
            ((s, c) for s, c in service_cost.items() if c > 0 and s.casefold() not in self._excluded),
            key=lambda sc: (-sc[1], sc[0]),
        )
        total = sum((c for _, c in ranked), Decimal(0))

        high: List[Tuple[int, str, Decimal]] = [
            (rank, s, c)
            for rank, (s, c) in enumerate(ranked, start=1)
            if rank <= self._top_n and c >= self._min_cost
        ]

        high_services = []
        unmapped = []
        for rank, service, cost in high:
            key = self._catalog.resolve_service(service)
            if key is None:
                unmapped.append(service)
            high_services.append({
                "rank": rank,
                "ce_service": service,
                "service_key": key,
                "cost_usd": _money(cost),
                "share_of_total_pct": _pct(cost, total),
                "has_catalog_entry": self._catalog.has_catalog_entry(key),
            })

        candidates, not_applicable = self._build_candidates(
            high, usage_cost, start_date, end_date, warnings
        )

        high_names = {s for _, s, _ in high}
        other_cost = sum((c for s, c in ranked if s not in high_names), Decimal(0))

        return {
            "period": {"start": start_date, "end_exclusive": end_date},
            "metric": self._metric,
            "rule": (
                f"A service is high-cost when it ranks in the top {self._top_n} by {self._metric} "
                f"and costs at least ${self._min_cost:.2f} for the period. "
                f"Excluded from ranking: {', '.join(self._excluded_display) or 'none'}."
            ),
            "total_cost_usd": _money(total),
            "high_cost_services": high_services,
            "other_services": {"count": len(ranked) - len(high), "cost_usd": _money(other_cost)},
            "unmapped_high_cost_services": unmapped,
            "candidates": candidates,
            "not_applicable": not_applicable,
            "warnings": warnings,
            "note": (
                "Cost-based screening only. Compatibility, usage patterns and savings "
                "have not been evaluated."
            ),
        }

    def service_costs(self, start_date: str, end_date: str, *, include_credit_breakdown: bool = False) -> Dict[str, Any]:
        """Total cost per Cost Explorer SERVICE name for the period (internal building block).

        Uses the same Cost Explorer query path as ``identify`` (same payer account, MONTHLY
        granularity, metric and exclusive end date), grouped by SERVICE only. Amounts of several
        monthly buckets are summed per service. Nothing is filtered, ranked or excluded here.

        With include_credit_breakdown, group by SERVICE and RECORD_TYPE in one
        request. Return signed credits and before-credit costs alongside net totals.
        Before-credit costs retain other record types (including refunds/discounts);
        they are not list-price estimates or necessarily Usage-only charges.

        Returns ``{"metric", "costs", "units", "warnings"}`` where ``costs`` maps the Cost Explorer
        service name to a ``Decimal`` (NOT JSON-serializable: callers convert) and ``units`` maps
        it to the sorted unit strings seen. On failure returns the same
        ``{"error": True, "code": ..., "message": ...}`` shape as ``identify``.
        """
        problem = self._validate_dates(start_date, end_date)
        if problem:
            return problem

        warnings: List[str] = []
        try:
            groups = ["SERVICE", "RECORD_TYPE"] if include_credit_breakdown else ["SERVICE"]
            rows = (self._fetch(start_date, end_date, groups, None, warnings, strict=True)
                    if include_credit_breakdown else self._fetch(start_date, end_date, groups, None, warnings))
        except ClientError as e:
            return self._client_error(e, "get_cost_and_usage")
        except Exception as e:  # unexpected: never let the agent tool crash
            logger.exception("Service cost retrieval failed")
            return {"error": True, "api": "get_cost_and_usage",
                    "exception_type": type(e).__name__, "message": str(e)}

        costs: Dict[str, Decimal] = {}
        units: Dict[str, set] = {}
        credits: Dict[str, Decimal] = {}
        records: Dict[str, Dict[str, Decimal]] = {}
        for keys, amount, unit in rows:
            if len(keys) != (2 if include_credit_breakdown else 1):
                if include_credit_breakdown:
                    warnings.append("Incomplete service/record-type grouping; cost breakdown is unavailable.")
                continue
            costs[keys[0]] = costs.get(keys[0], Decimal(0)) + amount
            units.setdefault(keys[0], set()).add(unit)
            if include_credit_breakdown:
                record_type = keys[1]
                if not isinstance(record_type, str) or not record_type.strip():
                    warnings.append("A cost row has no record type; cost breakdown is incomplete.")
                    continue
                per_service = records.setdefault(keys[0], {})
                per_service[record_type] = per_service.get(record_type, Decimal(0)) + amount
                if record_type.strip().casefold() == "credit":
                    credits[keys[0]] = credits.get(keys[0], Decimal(0)) + amount
        result = {
            "metric": self._metric,
            "costs": costs,
            "units": {service: sorted(found) for service, found in units.items()},
            "warnings": warnings,
        }
        if include_credit_breakdown:
            result.update(
                cost_breakdown_available=True,
                credits={s: credits.get(s, Decimal(0)) for s in costs},
                costs_before_credits={s: amount - credits.get(s, Decimal(0)) for s, amount in costs.items()},
                record_type_costs=records,
            )
        return result

    # ---------------------------------------------------------------- internals
    def _build_candidates(
        self,
        high: List[Tuple[int, str, Decimal]],
        usage_cost: Dict[Tuple[str, str], Decimal],
        start: str,
        end: str,
        warnings: List[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        candidates: List[Dict[str, Any]] = []
        not_applicable: List[Dict[str, str]] = []

        for entry in self._catalog.approved_entries():
            matched = [
                (s, c) for _, s, c in high
                if self._catalog.resolve_service(s) in entry.source_services
            ]
            if not matched:
                continue
            matched_names = [s for s, _ in matched]
            scoped, contributors = self._scoped_cost(entry, matched, usage_cost, start, end, warnings)

            if scoped is not None and scoped <= 0:
                not_applicable.append({
                    "alternative_id": entry.id,
                    "reason": f"No {entry.scope.kind} cost matching {list(entry.scope.values) or 'service'} "
                              f"inside {matched_names}",
                })
                continue

            service_total = sum((c for s, c in matched if s in contributors), Decimal(0))
            candidates.append({
                "alternative_id": entry.id,
                "source": entry.source_name,
                "alternative": entry.alternative_name,
                "matched_ce_services": [s for s in matched_names if s in contributors],
                "scope": {"kind": entry.scope.kind, "values": list(entry.scope.values)},
                "scoped_cost_usd": _money(scoped) if scoped is not None else None,
                "share_of_matched_service_cost_pct": (
                    _pct(scoped, service_total) if scoped is not None else None
                ),
                "required_evidence_next": list(entry.required_evidence),
                "status": "cost_screen_passed_usage_analysis_required",
            })

        candidates.sort(key=lambda c: (c["scoped_cost_usd"] is None, -(c["scoped_cost_usd"] or 0)))
        return candidates, not_applicable

    def _scoped_cost(
        self,
        entry: AlternativeEntry,
        matched: List[Tuple[str, Decimal]],
        usage_cost: Dict[Tuple[str, str], Decimal],
        start: str,
        end: str,
        warnings: List[str],
    ) -> Tuple[Optional[Decimal], List[str]]:
        """Return (scoped cost, names of the services that contributed to it)."""
        kind = entry.scope.kind
        matched_names = {s for s, _ in matched}
        all_names = sorted(matched_names)

        if kind == "whole_service":
            return sum((c for _, c in matched), Decimal(0)), all_names

        if kind == "usage_type_contains":
            needles = [v.casefold() for v in entry.scope.values]
            per_service: Dict[str, Decimal] = {}
            for (svc, usage), c in usage_cost.items():
                if svc in matched_names and any(n in usage.casefold() for n in needles):
                    per_service[svc] = per_service.get(svc, Decimal(0)) + c
            return sum(per_service.values(), Decimal(0)), sorted(per_service)

        if kind == "database_engine":
            flt = {"Dimensions": {"Key": "SERVICE", "Values": sorted(matched_names)}}
            try:
                rows = self._fetch(start, end, ["DATABASE_ENGINE"], flt, warnings)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "Unknown")
                warnings.append(
                    f"Could not break down {sorted(matched_names)} by DATABASE_ENGINE for "
                    f"'{entry.id}' ({code}); scoped cost unavailable"
                )
                return None, all_names
            except Exception as e:
                warnings.append(f"DATABASE_ENGINE breakdown failed for '{entry.id}': {e}")
                return None, all_names
            wanted = {v.casefold() for v in entry.scope.values}
            scoped = sum((c for keys, c, _ in rows if keys and keys[0].casefold() in wanted), Decimal(0))
            return scoped, all_names

        warnings.append(f"Unsupported scope kind '{kind}' for '{entry.id}'")
        return None, all_names

    def _get_ce(self) -> Any:
        if self._ce is None:
            self._ce = self._session_manager.get_client(self._payer_account_id, "ce")
        return self._ce

    def _fetch(
        self,
        start: str,
        end: str,
        group_by: List[str],
        flt: Optional[dict],
        warnings: List[str],
        strict: bool = False,
    ) -> List[Tuple[Tuple[str, ...], Decimal, str]]:
        """Read-only Cost Explorer query with NextPageToken pagination."""
        params: Dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": [self._metric],
            "GroupBy": [{"Type": "DIMENSION", "Key": k} for k in group_by],
        }
        if flt:
            params["Filter"] = flt

        rows: List[Tuple[Tuple[str, ...], Decimal, str]] = []
        ce = self._get_ce()
        token = None
        for _ in range(MAX_PAGES):
            if token:
                params["NextPageToken"] = token
            resp = ce.get_cost_and_usage(**params)
            for period in resp.get("ResultsByTime", []):
                for group in period.get("Groups", []):
                    metric = group.get("Metrics", {}).get(self._metric, {})
                    if strict and ("Amount" not in metric or "Unit" not in metric):
                        warnings.append("Missing amount or unit in Cost Explorer result; cost data is incomplete.")
                        continue
                    try:
                        amount = Decimal(str(metric.get("Amount", "0")))
                    except InvalidOperation:
                        warnings.append(f"Skipped unparseable amount for {group.get('Keys')}")
                        continue
                    if strict and not amount.is_finite():
                        warnings.append("Non-finite amount in Cost Explorer result; cost data is incomplete.")
                        continue
                    rows.append((tuple(group.get("Keys", [])), amount, metric.get("Unit", "USD")))
            token = resp.get("NextPageToken")
            if not token:
                return rows
        warnings.append(f"Cost Explorer pagination stopped after {MAX_PAGES} pages; results may be incomplete")
        return rows

    @staticmethod
    def _validate_dates(start: str, end: str) -> Optional[Dict[str, Any]]:
        for label, value in (("start_date", start), ("end_date", end)):
            if not isinstance(value, str) or not _DATE_RE.match(value):
                return {"error": True, "code": "InvalidDate",
                        "message": f"{label} must be YYYY-MM-DD, got {value!r}"}
            try:
                date.fromisoformat(value)
            except ValueError:
                return {"error": True, "code": "InvalidDate",
                        "message": f"{label} is not a real calendar date: {value!r}"}
        if start >= end:
            return {"error": True, "code": "InvalidDateRange",
                    "message": "start_date must be before end_date (end is exclusive)"}
        return None

    @staticmethod
    def _client_error(e: ClientError, api: str) -> Dict[str, Any]:
        err = e.response.get("Error", {})
        code = err.get("Code", "Unknown")
        result = {"error": True, "api": api, "code": code, "message": err.get("Message", str(e))}
        if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
            result["hint"] = "The payer role needs ce:GetCostAndUsage (see docs/iam-permissions.md, CostExplorer)."
        return result


def _money(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value.quantize(_CENT))


def _pct(part: Optional[Decimal], whole: Decimal) -> Optional[float]:
    if part is None or whole <= 0:
        return None
    return float((part / whole * 100).quantize(Decimal("0.1")))
