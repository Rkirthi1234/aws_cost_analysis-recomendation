"""Cost threshold alert tool (read-only, opt-in).

Exposes the existing cost threshold detection to the Strands agent as one tool:

* ``detect_cost_threshold_alerts`` - compares the configured services' cost with their configured
  thresholds and returns structured alert information.

It is a thin integration layer. Nothing is re-implemented here:

* configuration  -> ``load_cost_threshold_settings`` (``cost_thresholds`` section of config.yaml)
* detection      -> ``CostThresholdDetector`` fed by ``HighCostServiceIdentifier.service_costs``
* de-duplication -> ``ThresholdAlertDeduplicator``

Safety properties:

* Registered only when ``threshold_detection.enabled`` is the boolean true (default: false), see
  ``get_threshold_tools``. This flag is independent of ``alternatives.enabled`` and of
  ``cost_thresholds.enabled`` (which decides whether the detector evaluates anything).
* Cost Explorer is reached only through ``ReadOnlySessionManager`` (``ce:GetCostAndUsage`` only) on the
  payer account. Nothing in AWS is created, modified, started, stopped or deleted.
* The tool NEVER marks an alert as notified: it calls ``annotate(..., mark=False)``. An alert may only
  be recorded (``mark=True``) by a notification workflow after that workflow has accepted the
  notification; no such workflow exists in this tool.
* It returns structured alert information only. It does not send e-mail, SNS or any other
  notification, and it says so in every response.
* It never evaluates compatibility, prices or savings: a threshold alert is not an
  alternative-service recommendation.
* Every string that reaches the model goes through the same redaction/sanitizing as the alternatives
  tools (role ARNs, external IDs, access keys and the stream marker are removed).
"""
import logging
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from strands import tool

from .base_tool import BaseTool
from .alternatives_tools import (
    _dump,
    _error,
    _exception_error,
    _inclusive_end_warning,
    _validate_dates,
)
from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.cost_thresholds import (
    STATUS_EVALUATED,
    CostThresholdDetector,
    CostThresholdSettings,
    load_cost_threshold_settings,
)
from agent.services.high_cost_identifier import HighCostServiceIdentifier
from agent.services.readonly_guard import ReadOnlySessionManager, ReadOnlyViolation
from agent.services.threshold_alerts import ThresholdAlertDeduplicator, build_alert_store

logger = logging.getLogger("ThresholdTools")

CONFIG_FLAG = "threshold_detection.enabled"
TOOL_NAME = "detect_cost_threshold_alerts"
ALERT_TYPE = "cost_threshold_alert"

NOTIFICATION_NEW = "new_alert"                  # crossed, not recorded as notified before
NOTIFICATION_ALREADY = "already_notified"       # crossed, a notification was already recorded for it
NOTIFICATION_NOT_APPLICABLE = "not_applicable"  # nothing crossed for this service
NOTIFICATION_UNKNOWN = "unknown"                # crossed, de-duplication state unavailable

NOTE = (
    "Cost threshold alert information only. A threshold alert is NOT an alternative-service "
    "recommendation: no compatibility evaluation, pricing or savings estimate has been performed. "
    "No notification was sent and no alert was recorded as notified."
)
NEXT_STEP_CROSSED = (
    "Report the crossed thresholds to the user. Do not suggest that an alternative service is suitable "
    "because a threshold was crossed: that would need a compatibility evaluation, which this tool does not perform."
)


# ------------------------------------------------------------------ the tool
class ThresholdTools(BaseTool):
    """Read-only cost threshold alert tool for the agent."""

    def __init__(
        self,
        session_manager: Any,
        account_registry: Any,
        catalog: AlternativesCatalog,
        settings: CostThresholdSettings,
        *,
        deduplicator: Optional[ThresholdAlertDeduplicator] = None,
        identifier_factory: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], date]] = None,
    ):
        """``identifier_factory`` and ``clock`` exist for tests (a fake cost source and a fixed UTC date)."""
        self._sm = session_manager
        self._registry = account_registry
        self._catalog = catalog
        self._settings = settings
        self._identifier_factory = identifier_factory
        self._clock = clock
        self._dedupe = deduplicator if deduplicator is not None else self._build_deduplicator(settings)

    def get_tools(self) -> List[Callable]:
        return [self.detect_cost_threshold_alerts]

    @tool
    def detect_cost_threshold_alerts(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> str:
        """Check whether any configured service has crossed its cost threshold.

        Read-only. Compares each configured service's cost (for example EC2 and S3) with the threshold
        set in the cost_thresholds configuration and reports, per service: threshold status, actual
        cost, configured threshold, currency, period and notification status. It does NOT send
        any notification, and a threshold alert is NOT an alternative-service recommendation: it does
        not evaluate compatibility, pricing or savings.

        Args:
            start_date: Optional inclusive start date, YYYY-MM-DD. Give both dates or neither.
            end_date: Optional EXCLUSIVE end date, YYYY-MM-DD. For a full month use the first day of
                the next month (e.g. 2026-11-01 for October 2026). When both dates are omitted the
                period is month-to-date (1st of the current UTC month up to, not including, today).

        Returns:
            JSON with status (evaluated, disabled or invalid_configuration), alerts (one entry per
            configured service), summary, notifications, warnings and a note. On failure, a JSON
            error with code and message.
        """
        api = TOOL_NAME
        if (start_date is None) != (end_date is None):
            return _dump(_error(api, "InvalidDate", "Provide both start_date and end_date, or neither (month-to-date)."))
        if start_date is not None:
            problem = _validate_dates(start_date, end_date, api)
            if problem:
                return _dump(problem)

        try:
            detector = self._make_detector()
            result = detector.detect(start_date, end_date, today=self._clock() if self._clock else None)
        except ReadOnlyViolation as e:
            return _dump(_error(api, "ReadOnlyViolation", str(e)))
        except Exception as e:  # never let the agent tool crash
            logger.exception("%s failed", api)
            return _dump(_exception_error(api, e))

        if not isinstance(result, dict):
            return _dump(_error(api, "UnexpectedResult", "Threshold detection returned no result"))
        if result.get("error"):
            # Cost source error (AWS API error, period problem ...): keep code/message/hint, name this tool as
            # the api and keep the underlying API name as source_api.
            passthrough = {k: v for k, v in result.items() if k != "api"}
            if result.get("api"):
                passthrough["source_api"] = result["api"]
            return _dump({**passthrough, "api": api})

        try:
            return _dump(self._build_response(result, end_date))
        except Exception as e:  # defensive: a malformed detector result must not crash the tool
            logger.exception("%s could not build its response", api)
            return _dump(_exception_error(api, e))

    # ------------------------------------------------------------- internals
    @staticmethod
    def _build_deduplicator(settings: CostThresholdSettings) -> ThresholdAlertDeduplicator:
        """In-memory by default; the optional local state file when configured. Never raises."""
        try:
            return ThresholdAlertDeduplicator(build_alert_store(settings.state_file))
        except Exception as e:
            logger.warning(
                "Alert de-duplication store could not be created (%s); using in-memory de-duplication",
                type(e).__name__,
            )
            return ThresholdAlertDeduplicator()

    def _make_detector(self) -> CostThresholdDetector:
        """A cost source is built only when detection can actually run (enabled and valid)."""
        source = None
        if self._settings.valid and self._settings.enabled:
            if self._identifier_factory:
                source = self._identifier_factory()
            else:
                source = HighCostServiceIdentifier(ReadOnlySessionManager(self._sm), self._registry, self._catalog)
        return CostThresholdDetector(self._settings, self._catalog, source)

    def _build_response(self, result: Dict[str, Any], end_date: Optional[str]) -> Dict[str, Any]:
        """Annotate with notification status (read-only: never marks) and shape the structured output."""
        if result.get("status") != STATUS_EVALUATED:
            # disabled / invalid configuration: the detector made no Cost Explorer call
            out = {
                "alert_type": ALERT_TYPE,
                "status": result.get("status"),
                "enabled": bool(result.get("enabled")),
                "alerts": [],
                "warnings": list(result.get("warnings", [])),
                "notification_sent": False,
                "note": f"{result.get('note', '')} {NOTE}".strip(),
            }
            if result.get("errors"):
                out["errors"] = list(result["errors"])
            return out

        warnings: List[str] = list(result.get("warnings", []))
        dedupe_available = True
        try:
            annotated = self._dedupe.annotate(result, mark=False)   # mark=False: never record an alert here
        except Exception as e:
            logger.warning("Alert de-duplication failed (%s); notification status is unknown", type(e).__name__)
            annotated = result
            dedupe_available = False
            warnings.append(
                "Notification de-duplication was unavailable, so whether a crossed threshold was already "
                "notified is unknown."
            )

        if end_date:
            hint = _inclusive_end_warning(end_date)
            if hint and hint not in warnings:
                warnings.append(hint)

        period = annotated.get("period") or {}
        period_info = {k: period.get(k) for k in ("start", "end_exclusive", "kind")}
        currency = annotated.get("currency")

        alerts: List[Dict[str, Any]] = []
        for s in annotated.get("services", []):
            notification = s.get("notification")
            if notification is not None:
                status = NOTIFICATION_ALREADY if notification.get("already_notified") else NOTIFICATION_NEW
            elif s.get("crossed") and not dedupe_available:
                status = NOTIFICATION_UNKNOWN
            else:
                status = NOTIFICATION_NOT_APPLICABLE
            alerts.append({
                "service_key": s.get("service_key"),
                "service_name": s.get("display_name"),
                "threshold_status": s.get("status"),
                "crossed": bool(s.get("crossed")),
                "actual_cost": s.get("cost_usd"),
                "actual_cost_exact": s.get("cost_usd_exact"),
                "configured_threshold": s.get("threshold_usd"),
                "configured_threshold_exact": s.get("threshold_usd_exact"),
                "currency": currency,
                "period": period_info,
                "notification_status": status,
                "notification": notification,
                "cost_data": s.get("cost_data"),
                "cost_explorer_services": s.get("cost_explorer_services", []),
                "reason": s.get("reason"),
                "notes": s.get("notes", []),
            })

        summary = dict(annotated.get("summary", {}))
        summary["new_alerts"] = sum(1 for a in alerts if a["notification_status"] == NOTIFICATION_NEW)
        summary["already_notified"] = sum(1 for a in alerts if a["notification_status"] == NOTIFICATION_ALREADY)

        notifications = dict(annotated.get("notifications") or {"available": False})
        notifications["delivery"] = "not_performed"

        out: Dict[str, Any] = {
            "alert_type": ALERT_TYPE,
            "status": annotated.get("status"),
            "enabled": True,
            "period": period,
            "metric": annotated.get("metric"),
            "currency": currency,
            "comparison_rule": annotated.get("comparison_rule"),
            "alerts": alerts,
            "crossed_service_keys": list(annotated.get("crossed_service_keys", [])),
            "summary": summary,
            "notifications": notifications,
            "notification_sent": False,
            "warnings": warnings,
            "note": NOTE,
        }
        if out["crossed_service_keys"]:
            out["next_step"] = NEXT_STEP_CROSSED
        return out


# ------------------------------------------------------------------ registration
def is_threshold_detection_enabled(config: Any) -> bool:
    """True only when ``threshold_detection.enabled`` is the boolean true. Absent/false/invalid -> False."""
    getter = getattr(config, "get", None)
    value = getter(CONFIG_FLAG, False) if callable(getter) else False
    if value is True:
        return True
    if value is not None and not isinstance(value, bool):
        logger.warning("%s must be true or false (got %r); the threshold tool stays disabled", CONFIG_FLAG, value)
    return False


def get_threshold_tools(
    config: Any,
    session_manager: Any,
    account_registry: Any,
    *,
    catalog: Optional[AlternativesCatalog] = None,
) -> List[Callable]:
    """Return the threshold tool, or [] when disabled or when initialization fails.

    Never raises: any problem is logged as a warning so the existing agent tools are unaffected.
    Only the registration is gated by ``threshold_detection.enabled``; whether the detector evaluates
    anything is still decided by ``cost_thresholds.enabled`` (the tool reports ``disabled`` or
    ``invalid_configuration`` instead of calling Cost Explorer).
    """
    if not is_threshold_detection_enabled(config):
        return []
    try:
        resolved_catalog = catalog or AlternativesCatalog.load()
        settings = load_cost_threshold_settings(config, resolved_catalog)
        instance = ThresholdTools(session_manager, account_registry, resolved_catalog, settings)
        tools = instance.get_tools()
        logger.info("ThresholdTools: registered %d tool(s)", len(tools))
        return tools
    except Exception as e:
        logger.warning(
            "Threshold tool could not be initialized (%s: %s); continuing without it",
            type(e).__name__, e,
        )
        return []
