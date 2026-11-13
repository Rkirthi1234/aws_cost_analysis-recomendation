"""Cost threshold detection (read-only, recommendation-only).

Reads the optional ``cost_thresholds`` section of ``config.yaml``::

    cost_thresholds:
      enabled: true
      currency: USD
      services:
        ec2: 0.01
        s3: 0.01
      dedupe:                 # optional
        state_file: ./.cost_threshold_alerts.json   # local file only; omit for in-memory dedupe

and compares each configured service's cost with its threshold.

WHAT IS COMPARED (the period semantics are part of the contract)
    * Cost source: the existing Cost Explorer integration (``HighCostServiceIdentifier.service_costs``):
      payer account, ``MONTHLY`` granularity, ``AmortizedCost`` (the identifier's metric), USD.
    * Period: the caller passes ``start_date`` (inclusive) and ``end_date`` (EXCLUSIVE), like
      ``identify_high_cost_services``. When both are omitted the period is MONTH-TO-DATE: the first
      day of the current UTC month up to, but not including, today. On the 1st of a month there is no
      complete day yet, so explicit dates are required.
    * The compared figure is the TOTAL cost of the service over the whole period (several monthly
      buckets are summed). It is a monthly figure only when the period lies inside one calendar
      month; a warning is returned when it does not.
    * Rule: ``cost >= threshold`` crosses (equal counts as crossed). The comparison uses the exact
      Cost Explorer amount, never a rounded one; displayed amounts have 6 decimals.
    * Cost Explorer data can lag by up to 24 hours, so the newest day may be incomplete.

SERVICES AND COST EXPLORER NAMES
    A configured key (for example ``ec2``) is a service key of the alternatives catalog. Its cost is
    the SUM of every Cost Explorer service name the catalog maps to it, and each name is always
    reported separately. ``ec2`` maps to ``Amazon Elastic Compute Cloud - Compute`` and
    ``EC2 - Other``; the latter can include EBS, NAT Gateway and data-transfer charges, so a crossing
    must not be attributed entirely to EC2 instances.

SAFETY
    Only ``ce:GetCostAndUsage`` is used (through the caller's read-only session). Nothing is created,
    modified, started, stopped or deleted. Configuration is validated without raising: any
    configuration error produces ``status: invalid_configuration`` and NO Cost Explorer call.
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agent.services.alternatives_catalog import AlternativesCatalog, normalize_service_name

logger = logging.getLogger(__name__)

SECTION = "cost_thresholds"
SUPPORTED_CURRENCY = "USD"
_KNOWN_KEYS = ("enabled", "currency", "services", "dedupe")
_DEDUPE_KEYS = ("state_file",)
_MICRO = Decimal("0.000001")

STATUS_EVALUATED = "evaluated"
STATUS_DISABLED = "disabled"
STATUS_INVALID = "invalid_configuration"

SERVICE_CROSSED = "threshold_crossed"
SERVICE_BELOW = "below_threshold"
SERVICE_NO_DATA = "no_cost_data"
SERVICE_UNSUPPORTED_CURRENCY = "unsupported_currency"

PERIOD_SEMANTICS = (
    "Total cost over the whole period (start inclusive, end exclusive), summed across monthly buckets. "
    "With no dates given the period is month-to-date: the first day of the current UTC month up to, "
    "but not including, today. Cost Explorer data can lag by up to 24 hours."
)
EC2_OTHER_NOTE = (
    "Cost Explorer reports 'EC2 - Other' separately from 'Amazon Elastic Compute Cloud - Compute'. "
    "'EC2 - Other' can include EBS volumes and snapshots, NAT Gateway and data-transfer charges, so the "
    "combined total must not be attributed entirely to EC2 instances."
)
NOTE = (
    "Cost threshold detection only. It reports which services crossed their configured thresholds; it "
    "does not evaluate compatibility, estimate savings or change any AWS resource."
)


def _money(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value.quantize(_MICRO))


# ---------------------------------------------------------------------- settings
@dataclass(frozen=True)
class CostThresholdSettings:
    enabled: bool = False
    currency: str = SUPPORTED_CURRENCY
    thresholds: Dict[str, Decimal] = field(default_factory=dict)  # canonical service key -> threshold
    state_file: Optional[str] = None                               # optional local dedupe file
    source: str = "defaults"                                       # "defaults" (no section) or "config"
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "enabled": self.enabled,
            "currency": self.currency,
            "thresholds": {k: _money(v) for k, v in self.thresholds.items()},
            "persistent_dedupe": bool(self.state_file),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def load_cost_threshold_settings(config: Any, catalog: AlternativesCatalog) -> CostThresholdSettings:
    """Read and validate ``cost_thresholds`` from a loaded config. Never raises.

    ``config`` is a mapping or an object with a ``config`` mapping (``ConfigService``). An absent
    section gives a disabled, valid result. When ``enabled`` is false nothing else is validated.
    """
    raw = getattr(config, "config", config)
    section = raw.get(SECTION) if isinstance(raw, Mapping) else None
    if section is None:
        return CostThresholdSettings()

    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(section, Mapping):
        errors.append(f"'{SECTION}' must be a mapping, got {type(section).__name__}")
        return _finish(CostThresholdSettings(source="config"), errors, warnings)

    for key in section:
        if key not in _KNOWN_KEYS:
            warnings.append(f"Unknown key '{SECTION}.{_short(key)}' ignored")

    enabled_raw = section.get("enabled")
    if "enabled" not in section or enabled_raw is None:
        warnings.append(f"'{SECTION}.enabled' is not set; cost threshold detection is treated as disabled")
        return _finish(CostThresholdSettings(source="config"), errors, warnings)
    if not isinstance(enabled_raw, bool):
        errors.append(f"'{SECTION}.enabled' must be true or false, got {_short(enabled_raw)}")
        return _finish(CostThresholdSettings(source="config"), errors, warnings)
    if enabled_raw is False:
        return _finish(CostThresholdSettings(source="config"), errors, warnings)

    currency = _parse_currency(section.get("currency", SUPPORTED_CURRENCY), errors)
    thresholds = _parse_services(section.get("services"), catalog, errors)
    state_file = _parse_dedupe(section.get("dedupe"), errors)

    settings = CostThresholdSettings(
        enabled=True,
        currency=currency,
        thresholds=thresholds,
        state_file=state_file,
        source="config",
    )
    return _finish(settings, errors, warnings)


def _finish(settings: CostThresholdSettings, errors: List[str], warnings: List[str]) -> CostThresholdSettings:
    for message in errors:
        logger.warning("cost_thresholds config error: %s", message)
    for message in warnings:
        logger.warning("cost_thresholds config: %s", message)
    return CostThresholdSettings(
        enabled=settings.enabled if not errors else False,
        currency=settings.currency,
        thresholds=dict(settings.thresholds),
        state_file=settings.state_file,
        source=settings.source,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _parse_currency(raw: Any, errors: List[str]) -> str:
    if not isinstance(raw, str) or raw.strip().upper() != SUPPORTED_CURRENCY:
        errors.append(
            f"'{SECTION}.currency' {_short(raw)} is not supported: only {SUPPORTED_CURRENCY} is supported "
            "(no currency conversion is performed)"
        )
        return SUPPORTED_CURRENCY
    return SUPPORTED_CURRENCY


def _parse_services(raw: Any, catalog: AlternativesCatalog, errors: List[str]) -> Dict[str, Decimal]:
    where = f"{SECTION}.services"
    if not isinstance(raw, Mapping) or not raw:
        errors.append(f"'{where}' must be a non-empty mapping of service key to threshold")
        return {}

    thresholds: Dict[str, Decimal] = {}
    for name, value in raw.items():
        key = catalog.resolve_service(name) if isinstance(name, str) else None
        if key is None:
            errors.append(
                f"'{where}.{_short(name)}' is not a known service (known keys: {sorted(catalog.services)})"
            )
            continue
        if key in thresholds:
            errors.append(f"'{where}.{_short(name)}' duplicates the service '{key}'")
            continue
        threshold = _parse_threshold(value, f"{where}.{key}", errors)
        if threshold is not None:
            thresholds[key] = threshold
    return thresholds


def _parse_threshold(value: Any, where: str, errors: List[str]) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"'{where}' must be a number, got {_short(value)}")
        return None
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f"'{where}' must be a finite number, got {value!r}")
        return None
    threshold = Decimal(str(value))
    if threshold <= 0:
        errors.append(f"'{where}' must be greater than 0 (a zero threshold would flag every service), got {value!r}")
        return None
    return threshold


def _parse_dedupe(raw: Any, errors: List[str]) -> Optional[str]:
    if raw is None:
        return None
    where = f"{SECTION}.dedupe"
    if not isinstance(raw, Mapping):
        errors.append(f"'{where}' must be a mapping, got {type(raw).__name__}")
        return None
    for key in raw:
        if key not in _DEDUPE_KEYS:
            errors.append(f"Unknown key '{where}.{_short(key)}'")
    state_file = raw.get("state_file")
    if state_file is None:
        return None
    if not isinstance(state_file, str) or not state_file.strip():
        errors.append(f"'{where}.state_file' must be a non-empty local file path, got {_short(state_file)}")
        return None
    return state_file.strip()


# ---------------------------------------------------------------------- period
def month_to_date(today: Optional[date] = None) -> Optional[Tuple[str, str]]:
    """(start, end_exclusive) of the current UTC month up to, not including, today.

    Returns None on the 1st of a month: there is no complete day yet.
    """
    today = today or datetime.now(timezone.utc).date()
    if today.day == 1:
        return None
    return today.replace(day=1).isoformat(), today.isoformat()


# ---------------------------------------------------------------------- detector
class CostThresholdDetector:
    """Compares configured service costs with their thresholds. Read-only; never raises for AWS errors."""

    def __init__(self, settings: CostThresholdSettings, catalog: AlternativesCatalog, cost_source: Any = None):
        """``cost_source`` needs ``service_costs(start, end)`` (``HighCostServiceIdentifier``).

        It is only used when the feature is enabled and the configuration is valid, so it may be
        None for a disabled or invalid configuration.
        """
        self._settings = settings
        self._catalog = catalog
        self._source = cost_source

    def detect(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        *,
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Evaluate every configured service. Returns a JSON-serializable dict.

        Args:
            start_date: Inclusive start, YYYY-MM-DD. Give both dates or neither.
            end_date: EXCLUSIVE end, YYYY-MM-DD.
            today: Override of "today" (UTC) for the month-to-date default; used by tests.
        """
        settings = self._settings
        if not settings.valid:
            return {
                "status": STATUS_INVALID,
                "enabled": False,
                "errors": list(settings.errors),
                "warnings": list(settings.warnings),
                "note": "The cost_thresholds configuration is invalid; no Cost Explorer call was made.",
            }
        if not settings.enabled:
            return {
                "status": STATUS_DISABLED,
                "enabled": False,
                "warnings": list(settings.warnings),
                "note": "Cost threshold detection is disabled (cost_thresholds.enabled is not true); "
                        "no Cost Explorer call was made.",
            }

        if (start_date is None) != (end_date is None):
            return _error("InvalidDate", "Provide both start_date and end_date, or neither (month-to-date).")
        kind = "custom"
        if start_date is None:
            period = month_to_date(today)
            if period is None:
                return _error(
                    "PeriodEmpty",
                    "Month-to-date has no complete day on the 1st of a month; pass explicit start_date and end_date.",
                )
            start_date, end_date = period
            kind = "month_to_date"
        if self._source is None:
            return _error("NoCostSource", "No cost source is configured for threshold detection.")

        fetched = self._source.service_costs(start_date, end_date)
        if not isinstance(fetched, dict):
            return _error("UnexpectedResult", "The cost source returned no result.")
        if fetched.get("error"):
            return fetched

        warnings: List[str] = list(settings.warnings) + list(fetched.get("warnings", []))
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        last_day = end - timedelta(days=1)
        if (start.year, start.month) != (last_day.year, last_day.month):
            warnings.append(
                "The period spans several calendar months: thresholds are compared with the SUM over the whole "
                "period, not with a monthly figure. Use a single calendar month for monthly semantics."
            )

        costs: Dict[str, Decimal] = fetched.get("costs", {})
        units: Dict[str, List[str]] = fetched.get("units", {})
        by_norm = {normalize_service_name(name): name for name in costs}

        services = [
            self._evaluate_service(key, threshold, costs, units, by_norm, start_date, end_date)
            for key, threshold in settings.thresholds.items()
        ]
        crossed = [s["service_key"] for s in services if s["crossed"]]
        return {
            "status": STATUS_EVALUATED,
            "enabled": True,
            "period": {
                "start": start_date,
                "end_exclusive": end_date,
                "kind": kind,
                "granularity": "MONTHLY",
                "semantics": PERIOD_SEMANTICS,
            },
            "metric": fetched.get("metric"),
            "currency": settings.currency,
            "comparison_rule": "cost >= threshold crosses (equal counts); exact Cost Explorer amounts are compared, "
                               "displayed with 6 decimals",
            "services": services,
            "crossed_service_keys": crossed,
            "summary": {
                "configured": len(services),
                "crossed": len(crossed),
                "below_threshold": sum(1 for s in services if s["status"] == SERVICE_BELOW),
                "no_cost_data": sum(1 for s in services if s["status"] == SERVICE_NO_DATA),
                "unsupported_currency": sum(1 for s in services if s["status"] == SERVICE_UNSUPPORTED_CURRENCY),
            },
            "warnings": warnings,
            "note": NOTE,
        }

    def _evaluate_service(
        self,
        key: str,
        threshold: Decimal,
        costs: Dict[str, Decimal],
        units: Dict[str, List[str]],
        by_norm: Dict[str, str],
        start: str,
        end: str,
    ) -> Dict[str, Any]:
        info = self._catalog.services[key]
        breakdown: List[Dict[str, Any]] = []
        total = Decimal(0)
        any_present = False
        bad_units: List[str] = []
        notes: List[str] = []

        for ce_name in self._catalog.ce_names_for(key):
            actual = by_norm.get(normalize_service_name(ce_name))
            present = actual is not None
            amount = costs[actual] if present else Decimal(0)
            seen_units = list(units.get(actual, [])) if present else []
            bad_units.extend(u for u in seen_units if str(u).upper() != SUPPORTED_CURRENCY)
            any_present = any_present or present
            total += amount
            breakdown.append({
                "ce_service": ce_name,
                "present_in_cost_explorer": present,
                "cost_usd": _money(amount),
                "cost_usd_exact": format(amount, "f"),
            })
            if normalize_service_name(ce_name) == normalize_service_name("EC2 - Other"):
                notes.append(EC2_OTHER_NOTE)

        entry: Dict[str, Any] = {
            "service_key": key,
            "display_name": info.display_name,
            "threshold_usd": _money(threshold),
            "threshold_usd_exact": format(threshold, "f"),
            "cost_explorer_services": breakdown,
            "notes": notes,
        }
        period = f"{start} to {end} (end exclusive)"

        if bad_units:
            entry.update({
                "status": SERVICE_UNSUPPORTED_CURRENCY,
                "crossed": False,
                "cost_data": "reported",
                "cost_usd": None,
                "cost_usd_exact": None,
                "reason": f"Cost Explorer returned the unit(s) {sorted(set(bad_units))}; only {SUPPORTED_CURRENCY} "
                          "can be compared (no conversion is performed), so the threshold was not evaluated.",
            })
            return entry

        cost_data = "absent" if not any_present else ("zero" if total == 0 else "reported")
        entry.update({"cost_usd": _money(total), "cost_usd_exact": format(total, "f"), "cost_data": cost_data})

        if not any_present:
            entry.update({
                "status": SERVICE_NO_DATA,
                "crossed": False,
                "reason": f"Cost Explorer reported no cost for this service in {period}; it is treated as "
                          f"{SUPPORTED_CURRENCY} 0 and is below the threshold {entry['threshold_usd']}.",
            })
        elif total >= threshold:
            relation = "equals" if total == threshold else "is greater than"
            entry.update({
                "status": SERVICE_CROSSED,
                "crossed": True,
                "reason": f"Cost {entry['cost_usd']} {SUPPORTED_CURRENCY} {relation} the configured threshold "
                          f"{entry['threshold_usd']} {SUPPORTED_CURRENCY} for {period}; a cost equal to the "
                          "threshold counts as crossed.",
            })
        else:
            entry.update({
                "status": SERVICE_BELOW,
                "crossed": False,
                "reason": f"Cost {entry['cost_usd']} {SUPPORTED_CURRENCY} is below the configured threshold "
                          f"{entry['threshold_usd']} {SUPPORTED_CURRENCY} for {period}.",
            })
        return entry


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"error": True, "code": code, "message": message}
