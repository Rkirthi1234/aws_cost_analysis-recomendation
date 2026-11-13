"""Load usage-pattern settings (metrics window + pattern thresholds) from config.yaml.

Reads the optional ``usage_patterns`` section::

    usage_patterns:
      window:
        preferred_days: 30
        minimum_days: 14
      thresholds:
        steady_max_cv: 0.5
        idle_ec2_cpu_pct: 5.0
        idle_cpu_peak_pct: 30.0
        # ... any field of PatternThresholds

This module is pure: it takes an already-loaded config (a mapping, or an object with a
``config`` mapping such as ``ConfigService``) and never reads files or calls AWS.

Safe defaults: an absent or empty section gives the built-in defaults. A value that is
invalid (wrong type, out of range, NaN, a boolean ...) or an unknown key never raises and is
never silently used: the built-in default is kept for that setting and a warning is
returned in ``UsagePatternSettings.warnings`` and logged. The thresholds actually applied
are always the ones echoed in ``thresholds_used`` in the analysis output.
"""
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agent.services.usage_patterns import (
    THRESHOLD_LIMITS,
    MetricsWindow,
    PatternThresholds,
    validate_threshold,
)

logger = logging.getLogger(__name__)

SECTION = "usage_patterns"
WINDOW_KEYS = ("preferred_days", "minimum_days")


@dataclass(frozen=True)
class UsagePatternSettings:
    window: MetricsWindow
    thresholds: PatternThresholds
    source: str = "defaults"          # "defaults" (no section) or "config"
    warnings: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "window": self.window.as_dict(),
            "thresholds": self.thresholds.as_dict(),
            "warnings": list(self.warnings),
        }


def load_usage_pattern_settings(config: Any = None) -> UsagePatternSettings:
    """Build settings from a loaded config. Never raises."""
    warnings: List[str] = []
    raw = getattr(config, "config", config)
    section = raw.get(SECTION) if isinstance(raw, Mapping) else None

    if section is None:
        return UsagePatternSettings(MetricsWindow(), PatternThresholds(), "defaults", ())
    if not isinstance(section, Mapping):
        warnings.append(f"'{SECTION}' must be a mapping, got {type(section).__name__}; using defaults")
        return _finish(MetricsWindow(), PatternThresholds(), "config", warnings)

    for key in section:
        if key not in ("window", "thresholds"):
            warnings.append(f"Unknown key '{SECTION}.{key}' ignored")

    window = _parse_window(section.get("window"), warnings)
    thresholds = _parse_thresholds(section.get("thresholds"), warnings)
    return _finish(window, thresholds, "config", warnings)


def _finish(window: MetricsWindow, thresholds: PatternThresholds, source: str,
            warnings: List[str]) -> UsagePatternSettings:
    for w in warnings:
        logger.warning("usage_patterns config: %s", w)
    return UsagePatternSettings(window, thresholds, source, tuple(warnings))


def _parse_thresholds(raw: Any, warnings: List[str]) -> PatternThresholds:
    if raw is None:
        return PatternThresholds()
    if not isinstance(raw, Mapping):
        warnings.append(f"'{SECTION}.thresholds' must be a mapping, got {type(raw).__name__}; using defaults")
        return PatternThresholds()

    valid: Dict[str, float] = {}
    for name, value in raw.items():
        if name not in THRESHOLD_LIMITS:
            warnings.append(f"Unknown threshold '{name}' ignored")
            continue
        try:
            valid[name] = validate_threshold(name, value)
        except ValueError as e:
            warnings.append(f"Invalid threshold: {e}; using the default {getattr(PatternThresholds(), name)}")
    return PatternThresholds(**valid)


def _parse_window(raw: Any, warnings: List[str]) -> MetricsWindow:
    defaults = MetricsWindow()
    if raw is None:
        return defaults
    if not isinstance(raw, Mapping):
        warnings.append(f"'{SECTION}.window' must be a mapping, got {type(raw).__name__}; using defaults")
        return defaults

    values: Dict[str, int] = {}
    for name, value in raw.items():
        if name not in WINDOW_KEYS:
            warnings.append(f"Unknown window setting '{name}' ignored")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
            warnings.append(
                f"Invalid window setting {name}={value!r} (must be a whole number of days >= 1); "
                f"using the default {getattr(defaults, name)}"
            )
        else:
            values[name] = value

    preferred = values.get("preferred_days", defaults.preferred_days)
    minimum = values.get("minimum_days", defaults.minimum_days)
    try:
        return MetricsWindow(preferred_days=preferred, minimum_days=minimum)
    except ValueError as e:
        warnings.append(
            f"Invalid window (preferred_days={preferred}, minimum_days={minimum}): {e}; using the default window"
        )
        return defaults
