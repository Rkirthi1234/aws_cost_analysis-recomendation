"""Settings loader for the alternative-service decision engine (Step 38.2).

Reads the optional top-level ``alternative_decisions`` section of an already-loaded configuration. It is
SEPARATE from the ``usage_patterns`` loader (which is not changed) and, like the other loaders, is pure: it
takes a mapping or an object with a ``config`` mapping (``ConfigService``), never reads files and never calls AWS.
``agent/config.yaml`` and ``agent/config.yaml.example`` are not modified; an absent section simply gives the
documented defaults below.

DOCUMENTED SETTINGS (value in parentheses is the default; ``null`` means intentionally unset)::

    alternative_decisions:
      min_coverage_pct: 80                 # (0, 100]; minimum per-series raw datapoint coverage
      ec2_to_fargate:
        headroom_pct: 20                   # >= 0
        sizing_statistic: max              # only "max" is approved
      lambda_to_fargate:
        min_monthly_invocations: null      # unset thresholds stay unset; no default is invented
        min_average_concurrency: null
        min_active_duty_cycle_pct: null    # (0, 100]
      attestation:
        max_age_days: null                 # whole days >= 1, or unset
      pricing:
        min_monthly_savings_usd: null
        min_savings_pct: null              # (0, 100]
        hours_per_month: 730               # whole hours, 1..744
        currency: USD                      # only USD is supported
      cache:
        ttl_seconds: 86400                 # whole seconds >= 1

The metrics window is REUSED from ``usage_patterns.window`` (``minimum_days`` / ``preferred_days``); it is read
through the existing loader and exposed here as ``usage_patterns.window.minimum_days`` and
``usage_patterns.window.preferred_days``.

FAIL-CLOSED CONTRACT
    * Nothing raises.
    * A value that is invalid (wrong type, out of range, NaN, a boolean, an unsupported value, ``null`` where a
      value is required) is NEVER replaced by its default: the setting becomes ``None`` (unusable) and a
      ``SettingIssue`` records it.
    * Unknown keys are issues too, because a misspelled safety-critical key would otherwise leave the default
      in force without anyone noticing. They block every decision.
    * Decisions that depend on an invalid setting must not be made: ``blocked_decision`` returns the
      ``insufficient_data`` / ``configuration_invalid`` decision that exposes ``configuration_issues``.
    * Which decisions "depend on" a setting is derived from the catalog: the parameter references of its rules,
      its metric-coverage requirements and its attestation-expiry requirements (see
      ``AlternativeEntry.decision_setting_paths``). Settings that no entry references (pricing, cache) block
      nothing in the compatibility decisions.
    * ``null`` (or an absent key) for a nullable threshold is VALID and stays unset. What an unset threshold
      means for a verdict is defined by the catalog rules, not here (the approved rules make it ``insufficient_data``
      with the ``threshold_unset`` reason wherever a rule needs the threshold).

INTEGRATION POINT
    ``ConfigService`` exposes the parsed YAML as ``.config`` and a dotted-path ``get(key, default)`` that returns
    the default for a missing key AND for an explicit ``null``, so it cannot tell "intentionally unset" from
    "absent". The loader therefore reads the mapping itself (``ConfigService`` or a plain mapping are both
    accepted, exactly like ``load_usage_pattern_settings``). ``ConfigService`` is not changed.
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from agent.services.alternative_decision_models import AlternativeDecision, configuration_invalid
from agent.services.usage_pattern_config import load_usage_pattern_settings
from agent.services.usage_patterns import MetricsWindow

logger = logging.getLogger(__name__)

SECTION = "alternative_decisions"
USAGE_SECTION = "usage_patterns"
WINDOW_MINIMUM_PATH = "usage_patterns.window.minimum_days"
WINDOW_PREFERRED_PATH = "usage_patterns.window.preferred_days"
SUPPORTED_CURRENCY = "USD"
APPROVED_SIZING_STATISTICS = ("max",)


# ----------------------------------------------------------------------------- validators
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _pct_gt0_le100(value: Any) -> float:
    if not _is_number(value) or not 0 < value <= 100:
        raise ValueError(f"must be a number greater than 0 and at most 100, got {_short(value)}")
    return float(value)


def _non_negative(value: Any) -> float:
    if not _is_number(value) or value < 0:
        raise ValueError(f"must be a number >= 0, got {_short(value)}")
    return float(value)


def _positive(value: Any) -> float:
    if not _is_number(value) or value <= 0:
        raise ValueError(f"must be a number greater than 0, got {_short(value)}")
    return float(value)


def _whole_days(value: Any) -> int:
    if not _is_whole(value) or value < 1:
        raise ValueError(f"must be a whole number of days >= 1, got {_short(value)}")
    return value


def _whole_seconds(value: Any) -> int:
    if not _is_whole(value) or value < 1:
        raise ValueError(f"must be a whole number of seconds >= 1, got {_short(value)}")
    return value


def _hours_per_month(value: Any) -> int:
    if not _is_whole(value) or not 1 <= value <= 744:
        raise ValueError(f"must be a whole number of hours between 1 and 744, got {_short(value)}")
    return value


def _sizing_statistic(value: Any) -> str:
    if not isinstance(value, str) or value not in APPROVED_SIZING_STATISTICS:
        raise ValueError(f"must be one of {list(APPROVED_SIZING_STATISTICS)}, got {_short(value)}")
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str) or value.strip().upper() != SUPPORTED_CURRENCY:
        raise ValueError(f"only {SUPPORTED_CURRENCY} is supported (no conversion is performed), got {_short(value)}")
    return SUPPORTED_CURRENCY


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "..."


@dataclass(frozen=True)
class Spec:
    default: Any
    nullable: bool
    validate: Callable[[Any], Any]


# group -> {key: Spec}. Group "" holds the top-level settings.
SPECS: Dict[str, Dict[str, Spec]] = {
    "": {"min_coverage_pct": Spec(80.0, False, _pct_gt0_le100)},
    "ec2_to_fargate": {
        "headroom_pct": Spec(20.0, False, _non_negative),
        "sizing_statistic": Spec("max", False, _sizing_statistic),
    },
    "lambda_to_fargate": {
        "min_monthly_invocations": Spec(None, True, _positive),
        "min_average_concurrency": Spec(None, True, _positive),
        "min_active_duty_cycle_pct": Spec(None, True, _pct_gt0_le100),
    },
    "attestation": {"max_age_days": Spec(None, True, _whole_days)},
    "pricing": {
        "min_monthly_savings_usd": Spec(None, True, _positive),
        "min_savings_pct": Spec(None, True, _pct_gt0_le100),
        "hours_per_month": Spec(730, False, _hours_per_month),
        "currency": Spec(SUPPORTED_CURRENCY, False, _currency),
    },
    "cache": {"ttl_seconds": Spec(86400, False, _whole_seconds)},
}


def _full(group: str, key: str) -> str:
    return f"{group}.{key}" if group else key


# Every settings path of the alternative_decisions section (flat dotted form).
DECISION_SETTING_PATHS = frozenset(_full(g, k) for g, keys in SPECS.items() for k in keys)
# Settings reused from the usage_patterns loader.
USAGE_PATTERN_SETTING_PATHS = frozenset({"window.minimum_days", "window.preferred_days"})


def setting_path(source: str, ref: str) -> str:
    """Flat value path for a catalog parameter reference (``decision_setting`` / ``usage_pattern_setting``)."""
    return ref if source == "decision_setting" else f"{USAGE_SECTION}.{ref}"


# ----------------------------------------------------------------------------- results
@dataclass(frozen=True)
class SettingIssue:
    path: str                  # flat dotted path; "" for a problem with the whole section
    message: str
    blocks_all: bool = False   # structural problems / unknown keys block every decision

    def text(self) -> str:
        return self.message


@dataclass(frozen=True)
class DecisionSettings:
    """Validated settings. ``values`` is flat (dotted paths); an invalid setting is ``None`` and has an issue."""

    values: Mapping[str, Any]
    source: str = "defaults"                         # "defaults" (no section) or "config"
    issues: Tuple[SettingIssue, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def configuration_issues(self) -> Tuple[str, ...]:
        """Every problem as text, for ``configuration_issues`` in a blocked decision."""
        return tuple(i.message for i in self.issues)

    # convenience accessors (None when invalid or intentionally unset)
    @property
    def min_coverage_pct(self) -> Optional[float]:
        return self.values.get("min_coverage_pct")

    @property
    def window_minimum_days(self) -> Optional[int]:
        return self.values.get(WINDOW_MINIMUM_PATH)

    @property
    def window_preferred_days(self) -> Optional[int]:
        return self.values.get(WINDOW_PREFERRED_PATH)

    def value_of(self, path: str) -> Any:
        """Value for a flat path. Unknown paths raise ``KeyError`` (a rule must not reference one)."""
        if path not in self.values:
            raise KeyError(path)
        return self.values[path]

    def issues_affecting(self, paths: Iterable[str]) -> Tuple[SettingIssue, ...]:
        """Issues that make a decision depending on ``paths`` unsafe.

        An issue affects a path when it blocks everything, is on that exact path, or is on an ancestor of it
        (for example a non-mapping ``lambda_to_fargate`` affects ``lambda_to_fargate.min_monthly_invocations``).
        """
        wanted = list(paths)
        out: List[SettingIssue] = []
        for issue in self.issues:
            if issue.blocks_all or any(
                issue.path == p or (issue.path and p.startswith(issue.path + ".")) for p in wanted
            ):
                out.append(issue)
        return tuple(out)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "valid": self.valid,
            "values": dict(self.values),
            "configuration_issues": list(self.configuration_issues),
            "warnings": list(self.warnings),
        }


def blocked_decision(
    settings: DecisionSettings,
    alternative_id: str,
    setting_paths: Iterable[str],
    **scope: Optional[str],
) -> Optional[AlternativeDecision]:
    """The blocking decision for an alternative whose rules depend on ``setting_paths``, or None if safe.

    The result has verdict ``insufficient_data``, reason ``configuration_invalid`` and exposes the problems in
    ``configuration_issues``. Invalid values are never replaced by defaults.
    """
    affecting = settings.issues_affecting(setting_paths)
    if not affecting:
        return None
    return configuration_invalid(alternative_id, tuple(i.message for i in affecting), **scope)


# ----------------------------------------------------------------------------- loader
def load_alternative_decision_settings(config: Any = None) -> DecisionSettings:
    """Build settings from a loaded config. Never raises."""
    raw = getattr(config, "config", config)
    root = raw if isinstance(raw, Mapping) else {}
    section = root.get(SECTION)
    issues: List[SettingIssue] = []

    values: Dict[str, Any] = {_full(g, k): spec.default for g, keys in SPECS.items() for k, spec in keys.items()}

    if section is None:
        source = "defaults"
    elif not isinstance(section, Mapping):
        source = "config"
        issues.append(SettingIssue(
            "", f"'{SECTION}' must be a mapping, got {type(section).__name__}", blocks_all=True))
        values = {path: None for path in values}
    else:
        source = "config"
        _parse_section(section, values, issues)

    _read_window(root, values, issues)
    for issue in issues:
        logger.warning("alternative_decisions config: %s", issue.message)
    return DecisionSettings(values=values, source=source, issues=tuple(issues))


def _parse_section(section: Mapping, values: Dict[str, Any], issues: List[SettingIssue]) -> None:
    allowed = set(SPECS[""]) | {g for g in SPECS if g}
    for key in section:
        if key not in allowed:
            issues.append(SettingIssue(
                _short(key), f"Unknown key '{SECTION}.{_short(key)}'; check the spelling "
                             "(a misspelled setting would otherwise leave its default in force)", blocks_all=True))

    for key, spec in SPECS[""].items():
        _read(section, key, key, spec, values, issues)

    for group, specs in SPECS.items():
        if not group or group not in section:
            continue
        body = section[group]
        if body is None or not isinstance(body, Mapping):
            issues.append(SettingIssue(
                group, f"'{SECTION}.{group}' must be a mapping, got {type(body).__name__}"))
            for key in specs:
                values[_full(group, key)] = None
            continue
        for key in body:
            if key not in specs:
                issues.append(SettingIssue(
                    f"{group}.{_short(key)}", f"Unknown key '{SECTION}.{group}.{_short(key)}'", blocks_all=True))
        for key, spec in specs.items():
            _read(body, key, _full(group, key), spec, values, issues)


def _read(mapping: Mapping, key: str, path: str, spec: Spec, values: Dict[str, Any], issues: List[SettingIssue]) -> None:
    if key not in mapping:
        return                                    # absent: the documented default stays in force
    value = mapping[key]
    if value is None:
        if spec.nullable:
            values[path] = None                   # intentionally unset: stays unset
        else:
            values[path] = None
            issues.append(SettingIssue(path, f"'{SECTION}.{path}' must not be null"))
        return
    try:
        values[path] = spec.validate(value)
    except ValueError as e:
        values[path] = None                       # never replaced by the default
        issues.append(SettingIssue(path, f"'{SECTION}.{path}' {e}"))


def _read_window(root: Mapping, values: Dict[str, Any], issues: List[SettingIssue]) -> None:
    """Reuse ``usage_patterns.window``. The existing loader falls back to defaults with only a warning, so the
    raw values are checked here too: an invalid window blocks instead of being silently defaulted."""
    values[WINDOW_MINIMUM_PATH] = values[WINDOW_PREFERRED_PATH] = None
    usage = root.get(USAGE_SECTION)
    defaults = MetricsWindow()
    problems: List[str] = []

    if usage is not None and not isinstance(usage, Mapping):
        problems.append(f"'{USAGE_SECTION}' must be a mapping, got {type(usage).__name__}")
    else:
        window = usage.get("window") if isinstance(usage, Mapping) else None
        if window is not None and not isinstance(window, Mapping):
            problems.append(f"'{USAGE_SECTION}.window' must be a mapping, got {type(window).__name__}")
        elif isinstance(window, Mapping):
            for name in ("preferred_days", "minimum_days"):
                if name in window and not (_is_whole(window[name]) and window[name] >= 1):
                    problems.append(
                        f"'{USAGE_SECTION}.window.{name}' must be a whole number of days >= 1, got {_short(window[name])}")
            if not problems:
                preferred = window.get("preferred_days", defaults.preferred_days)
                minimum = window.get("minimum_days", defaults.minimum_days)
                if minimum > preferred:
                    problems.append(
                        f"'{USAGE_SECTION}.window' minimum_days ({minimum}) must not exceed preferred_days ({preferred})")

    if problems:
        for message in problems:
            issues.append(SettingIssue(f"{USAGE_SECTION}.window", message))
        return
    window_settings = load_usage_pattern_settings(root).window
    values[WINDOW_MINIMUM_PATH] = window_settings.minimum_days
    values[WINDOW_PREFERRED_PATH] = window_settings.preferred_days
