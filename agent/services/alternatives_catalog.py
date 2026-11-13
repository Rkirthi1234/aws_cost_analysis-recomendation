"""Alternatives catalog: service-name mapping, catalog loading and validation.

Pure data/validation module. It performs no AWS calls and has no dependency on
the agent runtime, so it can be unit tested in isolation.

The catalog data lives in ``agent/service_alternatives.yaml``.

TYPED DECISION DEFINITIONS (Step 38.3)
    Besides the descriptive ``required_evidence`` strings (unchanged: they are echoed in the usage evidence
    output) each approved entry carries machine-readable definitions. They are DATA ONLY: nothing here
    evaluates a rule; compatibility_evaluator executes the validated definitions.

    ``references``            source references: id, https URL, verification date
    ``evidence_requirements`` typed evidence keys (type, origin, source path or derivation, role)
    ``compatibility_rules``   typed rules: verdict, conditions over declared evidence keys, parameter
                              references (settings or AWS limits), basis and source reference ids

    Evidence ORIGIN says honestly where a value comes from:
        collected   produced by an existing collector (``source_path`` locates it in the usage evidence output)
        attestation a statement only the user can make; subject to ``attestation.max_age_days`` when it is set
        derived     computed from other declared evidence keys by a named derivation (implemented later)
        planned     no collector exists yet; the rule needs it, and without it the verdict is insufficient_data
    Evidence ROLE is ``required`` (a rule uses it) or ``informational`` (shown to the user, never used by a rule).

    VERDICT RULES (fail-closed, three-valued conditions)
        A condition is TRUE, FALSE or UNKNOWN. It is UNKNOWN when its evidence is missing, its attestation is
        expired, an unset (null) threshold is needed, a metric series is below the minimum coverage, or a limit is
        not determinable. A rule fires only when ALL its conditions are TRUE; UNKNOWN never counts as true.
        Rules are applied in ``VERDICT_PRECEDENCE`` order: already_in_use, incompatible, compatible. When none
        fires the verdict is ``insufficient_data`` (never a rule of its own), and the unknown conditions explain why.
        An entry may contain per-subject rules (``repeat_for``), for example S3 and DynamoDB, evaluated separately.

    OPERATORS: is_true, is_false, is_present (known and available), equals, in, not_in, gt, gte, lt, lte (with a
    literal ``value`` or a ``parameter``), within_applicable_lambda_limit / exceeds_applicable_lambda_limit (limit
    chosen by the invocation type: not_applicable is within and never exceeds, not_determinable is UNKNOWN) and
    fargate_size_fits / fargate_size_exceeds (a valid Fargate size exists / does not exist for the evidence with
    headroom).
"""
import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

import yaml

from agent.services import aws_limits
from agent.services.alternative_decision_settings import (
    DECISION_SETTING_PATHS,
    USAGE_PATTERN_SETTING_PATHS,
    WINDOW_MINIMUM_PATH,
    WINDOW_PREFERRED_PATH,
    setting_path,
)

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "service_alternatives.yaml"

SUPPORTED_SCHEMA_VERSION = 1
SCOPE_KINDS = ("whole_service", "usage_type_contains", "database_engine")
ALTERNATIVE_KINDS = ("service", "service_feature")
STATUS_APPROVED = "approved"
STATUS_PENDING = "pending_approval"
STATUSES = (STATUS_APPROVED, STATUS_PENDING)

_SERVICE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTRY_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_EVIDENCE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_REF_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# ---- typed decision definitions (Step 38.3) ----
EVIDENCE_TYPES = ("boolean", "number", "string", "enum", "list", "object")
ORIGIN_COLLECTED, ORIGIN_ATTESTATION, ORIGIN_DERIVED, ORIGIN_PLANNED = "collected", "attestation", "derived", "planned"
EVIDENCE_ORIGINS = (ORIGIN_COLLECTED, ORIGIN_ATTESTATION, ORIGIN_DERIVED, ORIGIN_PLANNED)
ROLE_REQUIRED, ROLE_INFORMATIONAL = "required", "informational"
EVIDENCE_ROLES = (ROLE_REQUIRED, ROLE_INFORMATIONAL)

# insufficient_data is never a rule: it is the fail-closed outcome when no rule fires.
RULE_VERDICTS = ("already_in_use", "incompatible", "compatible")
VERDICT_PRECEDENCE = ("already_in_use", "incompatible", "compatible", "insufficient_data")
BASIS_DOCUMENTATION, BASIS_DESIGN, BASIS_BOTH = "aws_documentation", "approved_design", "both"
RULE_BASES = (BASIS_DOCUMENTATION, BASIS_DESIGN, BASIS_BOTH)

PARAMETER_SOURCES = ("decision_setting", "usage_pattern_setting", "aws_limit")
AWS_LIMIT_REFS = frozenset(
    {"lambda.memory_min_mb", "lambda.memory_max_mb", "fargate.task_sizes"}
    | {f"lambda.applicable.{kind}" for kind in aws_limits.LAMBDA_LIMIT_KINDS}
)

_COMPARISON_OPS = ("gt", "gte", "lt", "lte")
_NO_OPERAND_OPS = ("is_true", "is_false", "is_present")
_LAMBDA_LIMIT_OPS = ("within_applicable_lambda_limit", "exceeds_applicable_lambda_limit")
_FARGATE_OPS = ("fargate_size_fits", "fargate_size_exceeds")
OPERATORS = (
    *_NO_OPERAND_OPS, "equals", "in", "not_in", *_COMPARISON_OPS, *_LAMBDA_LIMIT_OPS, *_FARGATE_OPS,
)
_CONDITION_KEYS = {
    "evidence", "op", "value", "values", "parameter", "headroom",
    "invocation_type_evidence", "memory_evidence", "os_family_evidence",
}


def normalize_service_name(name: str) -> str:
    """Normalize a service name for case/whitespace-insensitive matching."""
    return re.sub(r"\s+", " ", str(name).strip()).casefold()


class CatalogValidationError(ValueError):
    """Raised when the catalog is malformed. Carries every problem found."""

    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__(
            "Invalid service alternatives catalog:\n - " + "\n - ".join(self.errors)
        )


@dataclass(frozen=True)
class ServiceInfo:
    key: str
    display_name: str
    ce_names: Tuple[str, ...]
    pricing_service_code: str
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceScope:
    kind: str
    values: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CompatibilityNote:
    id: str
    text: str
    reference: str


@dataclass(frozen=True)
class SourceReference:
    """A documentation source with the date it was last verified."""

    id: str
    url: str
    verified_on: str
    title: str = ""


@dataclass(frozen=True)
class ParameterRef:
    """Reference to a configurable setting or an AWS limit used by a rule (never a literal copy of its value)."""

    source: str          # decision_setting | usage_pattern_setting | aws_limit
    ref: str

    @property
    def setting_path(self) -> Optional[str]:
        """Flat settings path for a setting reference; None for an AWS limit."""
        return None if self.source == "aws_limit" else setting_path(self.source, self.ref)


@dataclass(frozen=True)
class EvidenceRequirement:
    """One typed, machine-readable evidence key of an alternative."""

    key: str
    type: str
    origin: str
    description: str
    role: str = ROLE_REQUIRED
    subject: Optional[str] = None                      # e.g. "s3" for a per-service key
    unit: Optional[str] = None
    values: Tuple[str, ...] = ()                       # allowed values of an enum
    source_path: Optional[str] = None                  # collected: where it is in the usage evidence output
    derived_from: Tuple[str, ...] = ()                 # derived: evidence keys it is computed from
    derivation: Optional[str] = None                   # derived: name of the (later) derivation
    attestation_expiry: bool = False                   # attestation: subject to attestation.max_age_days
    requires_metric_coverage: bool = False             # metric series: needs min_coverage_pct and the window
    coverage_series: Optional[str] = None
    parameters: Tuple[ParameterRef, ...] = ()          # settings the value depends on (statistic, scaling)
    references: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleCondition:
    evidence: str
    op: str
    value: Any = None
    values: Tuple[Any, ...] = ()
    parameter: Optional[ParameterRef] = None
    headroom: Optional[ParameterRef] = None
    related_evidence: Mapping[str, str] = field(default_factory=dict)   # role -> evidence key

    def evidence_keys(self) -> Tuple[str, ...]:
        return (self.evidence, *self.related_evidence.values())

    def parameters(self) -> Tuple[ParameterRef, ...]:
        return tuple(p for p in (self.parameter, self.headroom) if p is not None)


@dataclass(frozen=True)
class DecisionRule:
    """A typed rule: it yields ``verdict`` when ALL conditions are true (see the module docstring)."""

    id: str
    verdict: str
    description: str
    conditions: Tuple[RuleCondition, ...]
    basis: str
    references: Tuple[str, ...] = ()
    subject: Optional[str] = None
    identifies: Tuple[str, ...] = ()          # evidence keys to report when the rule fires (uncovered route tables)


@dataclass(frozen=True)
class AlternativeEntry:
    id: str
    status: str
    description: str
    source_name: str
    source_services: Tuple[str, ...]
    scope: SourceScope
    alternative_name: str
    alternative_kind: str
    alternative_services: Tuple[str, ...]
    required_evidence: Tuple[str, ...] = ()
    notes: Tuple[CompatibilityNote, ...] = ()
    rules: Tuple[Dict[str, Any], ...] = ()          # raw compatibility_rules as written in the YAML
    pricing_note: Optional[str] = None
    references: Tuple[SourceReference, ...] = ()
    evidence_requirements: Tuple[EvidenceRequirement, ...] = ()
    decision_rules: Tuple[DecisionRule, ...] = ()   # the typed, expanded form of ``rules``

    @property
    def approved(self) -> bool:
        return self.status == STATUS_APPROVED

    def evidence_requirement(self, key: str) -> Optional[EvidenceRequirement]:
        return next((e for e in self.evidence_requirements if e.key == key), None)

    def decision_setting_paths(self) -> FrozenSet[str]:
        """Flat settings paths this entry's decisions depend on (an invalid one must block them).

        Collected from rule parameters, evidence parameters, metric-coverage requirements (minimum coverage
        and the usage_patterns window) and attestation-expiry requirements. Informational evidence adds none.
        """
        paths = set()
        for rule in self.decision_rules:
            for cond in rule.conditions:
                paths.update(p.setting_path for p in cond.parameters() if p.setting_path)
        for ev in self.evidence_requirements:
            if ev.role != ROLE_REQUIRED:
                continue
            paths.update(p.setting_path for p in ev.parameters if p.setting_path)
            if ev.requires_metric_coverage:
                paths.update({"min_coverage_pct", WINDOW_MINIMUM_PATH, WINDOW_PREFERRED_PATH})
            if ev.attestation_expiry:
                paths.add("attestation.max_age_days")
        return frozenset(paths)


@dataclass
class AlternativesCatalog:
    """Validated, read-only view of the alternatives catalog."""

    services: Dict[str, ServiceInfo]
    entries: List[AlternativeEntry]
    warnings: List[str] = field(default_factory=list)
    _name_index: Dict[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AlternativesCatalog":
        """Load and validate the catalog from YAML. Raises on any problem."""
        catalog_path = Path(path) if path else DEFAULT_CATALOG_PATH
        if not catalog_path.exists():
            raise FileNotFoundError(f"Alternatives catalog not found: {catalog_path}")
        try:
            data = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise CatalogValidationError([f"YAML parse error in {catalog_path.name}: {e}"]) from e
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "AlternativesCatalog":
        errors: List[str] = []
        warnings: List[str] = []

        if not isinstance(data, dict):
            raise CatalogValidationError(["catalog root must be a mapping"])

        if data.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {SUPPORTED_SCHEMA_VERSION}, got {data.get('schema_version')!r}"
            )

        services, name_index = cls._parse_services(data.get("services"), errors)
        entries = cls._parse_entries(data.get("alternatives"), services, errors, warnings)

        if errors:
            raise CatalogValidationError(errors)
        for w in warnings:
            logger.warning("Alternatives catalog: %s", w)
        return cls(services=services, entries=entries, warnings=warnings, _name_index=name_index)

    # --------------------------------------------------------------- validation
    @staticmethod
    def _parse_services(raw: Any, errors: List[str]) -> Tuple[Dict[str, ServiceInfo], Dict[str, str]]:
        services: Dict[str, ServiceInfo] = {}
        name_index: Dict[str, str] = {}

        if not isinstance(raw, dict) or not raw:
            errors.append("'services' must be a non-empty mapping")
            return services, name_index

        for key, body in raw.items():
            where = f"services.{key}"
            if not isinstance(key, str) or not _SERVICE_KEY_RE.match(key):
                errors.append(f"{where}: key must match {_SERVICE_KEY_RE.pattern}")
                continue
            if not isinstance(body, dict):
                errors.append(f"{where}: must be a mapping")
                continue

            display = body.get("display_name")
            code = body.get("pricing_service_code")
            ce_names = body.get("ce_names")
            aliases = body.get("aliases") or []

            if not isinstance(display, str) or not display.strip():
                errors.append(f"{where}: display_name is required")
            if not isinstance(code, str) or not code.strip():
                errors.append(f"{where}: pricing_service_code is required")
            if not _is_str_list(ce_names) or not ce_names:
                errors.append(f"{where}: ce_names must be a non-empty list of strings")
                continue
            if not _is_str_list(aliases):
                errors.append(f"{where}: aliases must be a list of strings")
                continue

            for name in [key, *ce_names, *aliases]:
                norm = normalize_service_name(name)
                owner = name_index.get(norm)
                if owner is not None and owner != key:
                    errors.append(
                        f"{where}: name {name!r} is already mapped to service '{owner}'"
                    )
                else:
                    name_index[norm] = key

            services[key] = ServiceInfo(
                key=key,
                display_name=str(display),
                ce_names=tuple(ce_names),
                pricing_service_code=str(code),
                aliases=tuple(aliases),
            )
        return services, name_index

    @staticmethod
    def _parse_entries(
        raw: Any,
        services: Dict[str, ServiceInfo],
        errors: List[str],
        warnings: List[str],
    ) -> List[AlternativeEntry]:
        entries: List[AlternativeEntry] = []
        if not isinstance(raw, list) or not raw:
            errors.append("'alternatives' must be a non-empty list")
            return entries

        seen_ids = set()
        for idx, body in enumerate(raw):
            where = f"alternatives[{idx}]"
            if not isinstance(body, dict):
                errors.append(f"{where}: must be a mapping")
                continue

            entry_id = body.get("id")
            if not isinstance(entry_id, str) or not _ENTRY_ID_RE.match(entry_id):
                errors.append(f"{where}: id must match {_ENTRY_ID_RE.pattern}")
                entry_id = f"#{idx}"
            elif entry_id in seen_ids:
                errors.append(f"{where}: duplicate id '{entry_id}'")
            seen_ids.add(entry_id)
            where = f"alternatives[{entry_id}]"

            status = body.get("status")
            if status not in STATUSES:
                errors.append(f"{where}: status must be one of {list(STATUSES)}, got {status!r}")

            description = body.get("description")
            if not isinstance(description, str) or not description.strip():
                errors.append(f"{where}: description is required")

            source = body.get("source")
            alternative = body.get("alternative")
            if not isinstance(source, dict):
                errors.append(f"{where}: 'source' must be a mapping")
                source = {}
            if not isinstance(alternative, dict):
                errors.append(f"{where}: 'alternative' must be a mapping")
                alternative = {}

            source_name = source.get("name")
            if not isinstance(source_name, str) or not source_name.strip():
                errors.append(f"{where}.source: name is required")
            src_services = _check_service_refs(source.get("services"), services, f"{where}.source", errors)
            scope = _parse_scope(source.get("scope"), f"{where}.source.scope", errors)

            alt_name = alternative.get("name")
            if not isinstance(alt_name, str) or not alt_name.strip():
                errors.append(f"{where}.alternative: name is required")
            elif isinstance(source_name, str) and normalize_service_name(alt_name) == normalize_service_name(source_name):
                errors.append(f"{where}: alternative must differ from the source ('{alt_name}')")
            alt_kind = alternative.get("kind")
            if alt_kind not in ALTERNATIVE_KINDS:
                errors.append(f"{where}.alternative: kind must be one of {list(ALTERNATIVE_KINDS)}, got {alt_kind!r}")
            alt_services = _check_service_refs(alternative.get("services"), services, f"{where}.alternative", errors)

            evidence = body.get("required_evidence", [])
            if not _is_str_list(evidence):
                errors.append(f"{where}: required_evidence must be a list of strings")
                evidence = []

            notes = _parse_notes(body.get("compatibility_notes", []), where, errors)

            rules = body.get("compatibility_rules", [])
            if not isinstance(rules, list):
                errors.append(f"{where}: compatibility_rules must be a list")
                rules = []
            elif not rules and status == STATUS_APPROVED:
                warnings.append(
                    f"{entry_id}: compatibility_rules is empty; compatibility cannot be "
                    "evaluated until rules from the approved design are added (Phase 3)"
                )

            pricing_note = alternative.get("pricing_note")
            if pricing_note is not None and not isinstance(pricing_note, str):
                errors.append(f"{where}.alternative: pricing_note must be a string")
                pricing_note = None

            references = _parse_references(body.get("references"), where, errors)
            evidence_reqs, decision_rules = _parse_decision_definitions(
                body.get("evidence_requirements"), rules if isinstance(rules, list) else [],
                references, where, errors,
            )

            entries.append(
                AlternativeEntry(
                    id=str(entry_id),
                    status=str(status),
                    description=str(description or "").strip(),
                    source_name=str(source_name or ""),
                    source_services=src_services,
                    scope=scope,
                    alternative_name=str(alt_name or ""),
                    alternative_kind=str(alt_kind or ""),
                    alternative_services=alt_services,
                    required_evidence=tuple(evidence),
                    notes=notes,
                    rules=tuple(r for r in rules if isinstance(r, dict)),
                    pricing_note=pricing_note.strip() if pricing_note else None,
                    references=references,
                    evidence_requirements=evidence_reqs,
                    decision_rules=decision_rules,
                )
            )
        return entries

    # ------------------------------------------------------------------ queries
    def resolve_service(self, name: str) -> Optional[str]:
        """Map a Cost Explorer service name (or alias/key) to its canonical key."""
        if not isinstance(name, str):
            return None
        return self._name_index.get(normalize_service_name(name))

    def ce_names_for(self, service_key: str) -> Tuple[str, ...]:
        info = self.services.get(service_key)
        return info.ce_names if info else ()

    def pricing_code_for(self, service_key: str) -> Optional[str]:
        info = self.services.get(service_key)
        return info.pricing_service_code if info else None

    def approved_entries(self) -> List[AlternativeEntry]:
        """Entries eligible for recommendations. Pending entries are never returned."""
        return [e for e in self.entries if e.approved]

    def get_alternatives_for(self, service_key: str) -> List[AlternativeEntry]:
        """Approved entries whose source involves the given canonical service key."""
        return [e for e in self.approved_entries() if service_key in e.source_services]

    def has_catalog_entry(self, service_key: Optional[str]) -> bool:
        return bool(service_key) and bool(self.get_alternatives_for(service_key))


# ----------------------------------------------------------- typed decision definitions
def _substitute(value: Any, placeholder: str, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace(placeholder, replacement)
    if isinstance(value, list):
        return [_substitute(v, placeholder, replacement) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, placeholder, replacement) for k, v in value.items()}
    return value


def _expand_repeats(items: List[Any], where: str, errors: List[str]) -> List[Dict[str, Any]]:
    """Expand ``repeat_for: {name: service, values: [s3, dynamodb]}`` items into one concrete item per value."""
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{where}[{i}]: must be a mapping")
            continue
        repeat = item.get("repeat_for")
        if repeat is None:
            out.append(item)
            continue
        if not (isinstance(repeat, dict) and isinstance(repeat.get("name"), str) and repeat["name"].strip()
                and _is_str_list(repeat.get("values")) and repeat["values"]):
            errors.append(f"{where}[{i}]: repeat_for needs a name and a non-empty list of values")
            continue
        base = {k: v for k, v in item.items() if k != "repeat_for"}
        for value in repeat["values"]:
            expanded = _substitute(copy.deepcopy(base), "{" + repeat["name"] + "}", value)
            expanded["subject"] = value
            out.append(expanded)
    return out


def _parse_references(raw: Any, where: str, errors: List[str]) -> Tuple[SourceReference, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append(f"{where}: references must be a list")
        return ()
    out: List[SourceReference] = []
    seen = set()
    for i, item in enumerate(raw):
        w = f"{where}.references[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{w}: must be a mapping")
            continue
        rid, url, verified, title = item.get("id"), item.get("url"), item.get("verified_on"), item.get("title", "")
        if not isinstance(rid, str) or not _REF_ID_RE.match(rid):
            errors.append(f"{w}: id must match {_REF_ID_RE.pattern}")
            continue
        if rid in seen:
            errors.append(f"{w}: duplicate reference id '{rid}'")
        seen.add(rid)
        if not isinstance(url, str) or not url.startswith("https://"):
            errors.append(f"{w}: url must be an https URL")
        if isinstance(verified, date):
            verified = verified.isoformat()
        try:
            date.fromisoformat(verified)
        except (TypeError, ValueError):
            errors.append(f"{w}: verified_on must be an ISO date (YYYY-MM-DD)")
        if not isinstance(title, str):
            errors.append(f"{w}: title must be a string")
            title = ""
        if isinstance(url, str) and isinstance(verified, str):
            out.append(SourceReference(id=rid, url=url, verified_on=verified, title=title))
    return tuple(out)


def _parse_param(raw: Any, where: str, errors: List[str]) -> Optional[ParameterRef]:
    if not isinstance(raw, dict) or set(raw) - {"source", "ref"}:
        errors.append(f"{where}: a parameter reference must be a mapping with only 'source' and 'ref'")
        return None
    source, ref = raw.get("source"), raw.get("ref")
    if source not in PARAMETER_SOURCES:
        errors.append(f"{where}: parameter source must be one of {list(PARAMETER_SOURCES)}, got {source!r}")
        return None
    valid = {
        "decision_setting": DECISION_SETTING_PATHS,
        "usage_pattern_setting": USAGE_PATTERN_SETTING_PATHS,
        "aws_limit": AWS_LIMIT_REFS,
    }[source]
    if not isinstance(ref, str) or ref not in valid:
        errors.append(f"{where}: unknown {source} reference {ref!r}")
        return None
    return ParameterRef(source=source, ref=ref)


def _parse_evidence(
    raw: Any, ref_ids: set, where: str, errors: List[str]
) -> Dict[str, EvidenceRequirement]:
    out: Dict[str, EvidenceRequirement] = {}
    if raw is None:
        return out
    if not isinstance(raw, list):
        errors.append(f"{where}: evidence_requirements must be a list")
        return out
    for i, item in enumerate(_expand_repeats(raw, f"{where}.evidence_requirements", errors)):
        key = item.get("key")
        w = f"{where}.evidence_requirements[{key if isinstance(key, str) else i}]"
        if not isinstance(key, str) or not _EVIDENCE_KEY_RE.match(key):
            errors.append(f"{w}: key must match {_EVIDENCE_KEY_RE.pattern}")
            continue
        if key in out:
            errors.append(f"{w}: duplicate evidence key")
            continue
        etype, origin = item.get("type"), item.get("origin")
        role = item.get("role", ROLE_REQUIRED)
        if etype not in EVIDENCE_TYPES:
            errors.append(f"{w}: type must be one of {list(EVIDENCE_TYPES)}, got {etype!r}")
        if origin not in EVIDENCE_ORIGINS:
            errors.append(f"{w}: origin must be one of {list(EVIDENCE_ORIGINS)}, got {origin!r}")
        if role not in EVIDENCE_ROLES:
            errors.append(f"{w}: role must be one of {list(EVIDENCE_ROLES)}, got {role!r}")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{w}: description is required")
            description = ""
        values = item.get("values") or []
        if not _is_str_list(values):
            errors.append(f"{w}: values must be a list of strings")
            values = []
        if etype == "enum" and not values:
            errors.append(f"{w}: an enum needs a non-empty list of values")
        if etype != "enum" and values:
            errors.append(f"{w}: only an enum may define values")
        source_path, derived_from = item.get("source_path"), item.get("derived_from") or []
        derivation = item.get("derivation")
        if origin == ORIGIN_COLLECTED and not (isinstance(source_path, str) and source_path.strip()):
            errors.append(f"{w}: a collected key needs a source_path")
        if origin != ORIGIN_COLLECTED and source_path is not None:
            errors.append(f"{w}: only a collected key may define a source_path")
        if origin == ORIGIN_DERIVED:
            if not _is_str_list(derived_from) or not derived_from:
                errors.append(f"{w}: a derived key needs derived_from (a list of evidence keys)")
                derived_from = []
            if not isinstance(derivation, str) or not derivation.strip():
                errors.append(f"{w}: a derived key needs a derivation name")
        elif derived_from or derivation is not None:
            errors.append(f"{w}: only a derived key may define derived_from or derivation")
            derived_from = []
        expiry = item.get("attestation_expiry", origin == ORIGIN_ATTESTATION)
        if not isinstance(expiry, bool) or (expiry and origin != ORIGIN_ATTESTATION):
            errors.append(f"{w}: attestation_expiry must be a boolean and is only allowed for an attestation")
            expiry = False
        coverage = item.get("requires_metric_coverage", False)
        series = item.get("coverage_series")
        if not isinstance(coverage, bool):
            errors.append(f"{w}: requires_metric_coverage must be a boolean")
            coverage = False
        if coverage and not (isinstance(series, str) and series.strip()):
            errors.append(f"{w}: requires_metric_coverage needs a coverage_series")
        if series is not None and not coverage:
            errors.append(f"{w}: coverage_series is only allowed with requires_metric_coverage")
        params = []
        raw_params = item.get("parameters") or []
        if not isinstance(raw_params, list):
            errors.append(f"{w}: parameters must be a list")
            raw_params = []
        for j, p in enumerate(raw_params):
            parsed = _parse_param(p, f"{w}.parameters[{j}]", errors)
            if parsed is not None:
                if parsed.source == "aws_limit":
                    errors.append(f"{w}.parameters[{j}]: an evidence key may depend on settings only")
                else:
                    params.append(parsed)
        refs = item.get("references") or []
        if not _is_str_list(refs) or any(r not in ref_ids for r in refs):
            errors.append(f"{w}: references must list declared reference ids")
            refs = []
        unit = item.get("unit")
        if unit is not None and not isinstance(unit, str):
            errors.append(f"{w}: unit must be a string")
            unit = None
        out[key] = EvidenceRequirement(
            key=key, type=str(etype), origin=str(origin), description=description.strip(), role=str(role),
            subject=item.get("subject"), unit=unit, values=tuple(values),
            source_path=source_path if isinstance(source_path, str) else None,
            derived_from=tuple(derived_from), derivation=derivation if isinstance(derivation, str) else None,
            attestation_expiry=expiry, requires_metric_coverage=coverage,
            coverage_series=series if isinstance(series, str) else None,
            parameters=tuple(params), references=tuple(refs),
        )
    for ev in out.values():
        for dep in ev.derived_from:
            if dep not in out or dep == ev.key:
                errors.append(f"{where}.evidence_requirements[{ev.key}]: derived_from key '{dep}' is not another declared key")
    return out


def _parse_condition(
    raw: Any, evidence: Dict[str, EvidenceRequirement], where: str, errors: List[str]
) -> Optional[RuleCondition]:
    if not isinstance(raw, dict):
        errors.append(f"{where}: a condition must be a mapping")
        return None
    unknown = set(raw) - _CONDITION_KEYS
    if unknown:
        errors.append(f"{where}: unknown condition keys {sorted(unknown)}")
        return None
    key, op = raw.get("evidence"), raw.get("op")
    ev = evidence.get(key) if isinstance(key, str) else None
    if ev is None:
        errors.append(f"{where}: evidence '{key}' is not a declared evidence key")
        return None
    if ev.role != ROLE_REQUIRED:
        errors.append(f"{where}: informational evidence '{key}' cannot be used by a rule")
        return None
    if op not in OPERATORS:
        errors.append(f"{where}: op must be one of {list(OPERATORS)}, got {op!r}")
        return None

    before = len(errors)
    value, values = raw.get("value"), raw.get("values")
    parameter = _parse_param(raw["parameter"], f"{where}.parameter", errors) if "parameter" in raw else None
    headroom = _parse_param(raw["headroom"], f"{where}.headroom", errors) if "headroom" in raw else None
    related: Dict[str, str] = {}
    for field_name, role in (("invocation_type_evidence", "invocation_type"), ("memory_evidence", "memory"),
                             ("os_family_evidence", "os_family")):
        if field_name in raw:
            target = raw[field_name]
            target_ev = evidence.get(target) if isinstance(target, str) else None
            if target_ev is None or target_ev.role != ROLE_REQUIRED:
                errors.append(f"{where}: {field_name} '{target}' is not a declared required evidence key")
            else:
                related[role] = target

    def need(ok: bool, message: str) -> None:
        if not ok:
            errors.append(f"{where}: {message}")

    def only(*allowed: str) -> None:
        given = {n for n, present in (("value", "value" in raw), ("values", "values" in raw),
                                      ("parameter", "parameter" in raw), ("headroom", "headroom" in raw),
                                      ("invocation_type_evidence", "invocation_type_evidence" in raw),
                                      ("memory_evidence", "memory_evidence" in raw),
                                      ("os_family_evidence", "os_family_evidence" in raw)) if present}
        extra = given - set(allowed)
        need(not extra, f"op '{op}' does not accept {sorted(extra)}")

    def literals_allowed(literals: List[Any]) -> None:
        if ev.type == "enum":
            bad = [v for v in literals if v not in ev.values]
            need(not bad, f"{bad} not in the allowed values {list(ev.values)} of '{key}'")

    if op in ("is_true", "is_false"):
        only()
        need(ev.type == "boolean", f"op '{op}' needs a boolean evidence key, '{key}' is {ev.type}")
    elif op == "is_present":
        only()
    elif op == "equals":
        only("value")
        need(isinstance(value, (str, int, float, bool)) and value is not None, "equals needs a literal value")
        if isinstance(value, str):
            literals_allowed([value])
    elif op in ("in", "not_in"):
        only("values")
        need(isinstance(values, list) and bool(values), f"{op} needs a non-empty list of values")
        if isinstance(values, list):
            literals_allowed(values)
    elif op in _COMPARISON_OPS:
        only("value", "parameter")
        need(ev.type == "number", f"op '{op}' needs a number evidence key, '{key}' is {ev.type}")
        need(("value" in raw) != ("parameter" in raw), f"{op} needs exactly one of value or parameter")
        if "value" in raw:
            need(isinstance(value, (int, float)) and not isinstance(value, bool), "value must be a number")
        if parameter is not None:
            need(parameter.source != "aws_limit" or (parameter.ref != "fargate.task_sizes"
                                                      and not parameter.ref.startswith("lambda.applicable.")),
                 f"'{parameter.ref}' is not a single number; use the dedicated operator")
    elif op in _LAMBDA_LIMIT_OPS:
        only("parameter", "invocation_type_evidence")
        need(ev.type == "number", f"op '{op}' needs a number evidence key, '{key}' is {ev.type}")
        need(parameter is not None and parameter.source == "aws_limit" and parameter.ref.startswith("lambda.applicable."),
             "parameter must be an aws_limit lambda.applicable.* reference")
        need("invocation_type" in related, "invocation_type_evidence is required (the limit depends on the invocation type)")
        itype = evidence.get(raw.get("invocation_type_evidence")) if isinstance(raw.get("invocation_type_evidence"), str) else None
        if itype is not None:
            need(itype.type == "enum" and set(itype.values) == set(aws_limits.LAMBDA_INVOCATION_TYPES),
                 f"invocation type evidence must be an enum of {list(aws_limits.LAMBDA_INVOCATION_TYPES)}")
    else:  # fargate ops
        only("parameter", "headroom", "memory_evidence", "os_family_evidence")
        need(ev.type == "number", f"op '{op}' needs a number evidence key (CPU), '{key}' is {ev.type}")
        need(parameter is not None and parameter.source == "aws_limit" and parameter.ref == "fargate.task_sizes",
             "parameter must be the aws_limit fargate.task_sizes reference")
        need(headroom is not None and headroom.source == "decision_setting", "headroom must be a decision_setting reference")
        need({"memory", "os_family"} <= set(related), "memory_evidence and os_family_evidence are required")

    if len(errors) > before:
        return None
    return RuleCondition(
        evidence=key, op=op, value=value, values=tuple(values) if isinstance(values, list) else (),
        parameter=parameter, headroom=headroom, related_evidence=related,
    )


def _parse_rules(
    raw_rules: List[Any], evidence: Dict[str, EvidenceRequirement], ref_ids: set, where: str, errors: List[str]
) -> Tuple[DecisionRule, ...]:
    out: List[DecisionRule] = []
    seen = set()
    for i, item in enumerate(_expand_repeats(raw_rules, f"{where}.compatibility_rules", errors)):
        rid = item.get("id")
        w = f"{where}.compatibility_rules[{rid if isinstance(rid, str) else i}]"
        if not isinstance(rid, str) or not _RULE_ID_RE.match(rid):
            errors.append(f"{w}: id must match {_RULE_ID_RE.pattern}")
            continue
        if rid in seen:
            errors.append(f"{w}: duplicate rule id")
        seen.add(rid)
        verdict = item.get("verdict")
        if verdict == "insufficient_data":
            errors.append(f"{w}: insufficient_data is never a rule; it is the outcome when no rule fires")
        elif verdict not in RULE_VERDICTS:
            errors.append(f"{w}: verdict must be one of {list(RULE_VERDICTS)}, got {verdict!r}")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{w}: description is required")
            description = ""
        basis = item.get("basis")
        if basis not in RULE_BASES:
            errors.append(f"{w}: basis must be one of {list(RULE_BASES)}, got {basis!r}")
        refs = item.get("references") or []
        if not _is_str_list(refs) or any(r not in ref_ids for r in refs):
            errors.append(f"{w}: references must list declared reference ids")
            refs = []
        elif basis in (BASIS_DOCUMENTATION, BASIS_BOTH) and not refs:
            errors.append(f"{w}: a rule based on AWS documentation needs at least one reference")
        raw_when = item.get("when")
        conditions: List[RuleCondition] = []
        if not isinstance(raw_when, list) or not raw_when:
            errors.append(f"{w}: 'when' must be a non-empty list of conditions")
        else:
            for j, cond in enumerate(raw_when):
                parsed = _parse_condition(cond, evidence, f"{w}.when[{j}]", errors)
                if parsed is not None:
                    conditions.append(parsed)
        identifies = item.get("identifies") or []
        if not _is_str_list(identifies) or any(k not in evidence for k in identifies):
            errors.append(f"{w}: identifies must list declared evidence keys")
            identifies = []
        if isinstance(verdict, str) and isinstance(basis, str):
            out.append(DecisionRule(
                id=rid, verdict=verdict, description=description.strip(), conditions=tuple(conditions), basis=basis,
                references=tuple(refs), subject=item.get("subject"), identifies=tuple(identifies),
            ))
    return tuple(out)


def _parse_decision_definitions(
    raw_evidence: Any,
    raw_rules: List[Any],
    references: Tuple[SourceReference, ...],
    where: str,
    errors: List[str],
) -> Tuple[Tuple[EvidenceRequirement, ...], Tuple[DecisionRule, ...]]:
    if raw_evidence is None and not raw_rules:
        return (), ()
    ref_ids = {r.id for r in references}
    evidence = _parse_evidence(raw_evidence, ref_ids, where, errors)
    if raw_rules and not evidence:
        errors.append(f"{where}: compatibility_rules need evidence_requirements")
    rules = _parse_rules(raw_rules, evidence, ref_ids, where, errors) if evidence else ()
    if evidence and not any(r.verdict == "compatible" for r in rules):
        errors.append(f"{where}: at least one compatible rule is required")

    used = set()
    for rule in rules:
        used.update(rule.identifies)
        for cond in rule.conditions:
            used.update(cond.evidence_keys())
    for ev in evidence.values():       # keys a used derived key is computed from count as used
        if ev.key in used:
            used.update(ev.derived_from)
    for _ in evidence:                 # propagate through chains of derivations
        for ev in evidence.values():
            if ev.key in used:
                used.update(ev.derived_from)
    for ev in evidence.values():
        if ev.role == ROLE_REQUIRED and ev.key not in used:
            errors.append(f"{where}.evidence_requirements[{ev.key}]: required evidence is not used by any rule "
                          "(mark it informational or remove it)")
        if ev.role == ROLE_INFORMATIONAL and ev.key in used:
            errors.append(f"{where}.evidence_requirements[{ev.key}]: informational evidence must not be used by a rule")
    return tuple(evidence.values()), rules


# ---------------------------------------------------------------------- helpers
def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _check_service_refs(
    refs: Any, services: Dict[str, ServiceInfo], where: str, errors: List[str]
) -> Tuple[str, ...]:
    if not _is_str_list(refs) or not refs:
        errors.append(f"{where}: services must be a non-empty list of service keys")
        return ()
    for ref in refs:
        if ref not in services:
            errors.append(f"{where}: unknown service key '{ref}'")
    return tuple(refs)


def _parse_scope(raw: Any, where: str, errors: List[str]) -> SourceScope:
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping")
        return SourceScope(kind="")
    kind = raw.get("kind")
    if kind not in SCOPE_KINDS:
        errors.append(f"{where}: kind must be one of {list(SCOPE_KINDS)}, got {kind!r}")
        return SourceScope(kind=str(kind))
    values = raw.get("values") or []
    if kind == "whole_service":
        if values:
            errors.append(f"{where}: whole_service must not define values")
        return SourceScope(kind=kind)
    if not _is_str_list(values) or not values:
        errors.append(f"{where}: {kind} requires a non-empty list of values")
        return SourceScope(kind=kind)
    return SourceScope(kind=kind, values=tuple(values))


def _parse_notes(raw: Any, where: str, errors: List[str]) -> Tuple[CompatibilityNote, ...]:
    if not isinstance(raw, list):
        errors.append(f"{where}: compatibility_notes must be a list")
        return ()
    notes: List[CompatibilityNote] = []
    seen = set()
    for i, n in enumerate(raw):
        nw = f"{where}.compatibility_notes[{i}]"
        if not isinstance(n, dict):
            errors.append(f"{nw}: must be a mapping")
            continue
        nid, text, ref = n.get("id"), n.get("text"), n.get("reference")
        if not isinstance(nid, str) or not nid.strip():
            errors.append(f"{nw}: id is required")
        elif nid in seen:
            errors.append(f"{nw}: duplicate note id '{nid}'")
        seen.add(nid)
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{nw}: text is required")
        if not isinstance(ref, str) or not ref.startswith("https://"):
            errors.append(f"{nw}: reference must be an https URL (every note needs a source)")
        if isinstance(nid, str) and isinstance(text, str) and isinstance(ref, str):
            notes.append(CompatibilityNote(id=nid, text=" ".join(text.split()), reference=ref))
    return tuple(notes)
