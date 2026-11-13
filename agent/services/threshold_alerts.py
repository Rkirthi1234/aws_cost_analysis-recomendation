"""Notification de-duplication for cost threshold alerts.

Without it, a service that stays above its threshold would trigger the same notification on every
run. The de-duplicator gives each alert a fingerprint and tells the caller whether a notification
for it was already produced.

WHAT MAKES TWO ALERTS "THE SAME"
    service key + calendar month of the period + configured threshold + currency + metric.
    * Within one calendar month a service that keeps crossing the same threshold is reported as
      ``already_notified`` on later runs, even if its cost keeps growing. The full threshold result
      is still returned every time; only the *notification* is suppressed.
    * A new month, a changed threshold, currency or metric, or a different service produces a new
      fingerprint, so a new notification.
    * A period that is not inside one calendar month is keyed by its exact start and end dates.

WHEN AN ALERT IS RECORDED
    ``annotate(result, mark=False)`` is read-only. The caller passes ``mark=True`` only when the
    notification is really being produced, so an alert is never recorded as notified without having
    been shown.

STORAGE
    * Default: in memory (``InMemoryAlertStore``). It lives as long as the process or agent
      session; a new process starts empty, so a notification may repeat after a restart.
    * Optional: one local JSON file (``JsonFileAlertStore``), enabled with
      ``cost_thresholds.dedupe.state_file``. Only fingerprints and UTC timestamps are written (no
      account IDs, costs or credentials). Writes are atomic (temporary file + replace). A missing
      file means no history; an unreadable or corrupt file is treated as empty and reported in
      ``warnings`` (it is replaced by the next write); a failed write is reported in ``warnings``
      and the state is kept in memory. Nothing here ever raises into the agent.
    * No AWS storage (S3, DynamoDB, ...) is used or created.
"""
import copy
import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 1000
STORE_VERSION = 1
DEDUPE_KEY_DESCRIPTION = "service + calendar month + threshold + currency + metric"


def period_key(start: Any, end_exclusive: Any) -> str:
    """``YYYY-MM`` when the period lies inside one calendar month, else ``start..end``."""
    try:
        first = date.fromisoformat(str(start))
        last = date.fromisoformat(str(end_exclusive)) - timedelta(days=1)
    except ValueError:
        return f"{start}..{end_exclusive}"
    if last >= first and (first.year, first.month) == (last.year, last.month):
        return f"{first.year:04d}-{first.month:02d}"
    return f"{start}..{end_exclusive}"


def alert_fingerprint(service_key: str, period: str, threshold: Decimal, currency: Any, metric: Any) -> str:
    """Stable fingerprint of one alert (see the module docstring for what it contains)."""
    payload = json.dumps(
        [str(service_key), str(period), format(threshold.normalize(), "f"), str(currency), str(metric)],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------- stores
class InMemoryAlertStore:
    """Default store: lives as long as the process."""

    kind = "in_memory"

    def __init__(self) -> None:
        self._seen: Dict[str, str] = {}
        self._lock = threading.Lock()
        self.warnings: List[str] = []

    def contains(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._seen

    def add(self, fingerprint: str) -> None:
        with self._lock:
            self._seen[fingerprint] = _now()


class JsonFileAlertStore:
    """Optional local-file store. Only fingerprints and UTC timestamps are written."""

    kind = "local_file"

    def __init__(self, path: str, *, max_entries: int = DEFAULT_MAX_ENTRIES):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty local file path")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._path = os.path.abspath(path.strip())
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self.warnings: List[str] = []
        self._entries: Dict[str, str] = self._load()

    def contains(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._entries

    def add(self, fingerprint: str) -> None:
        with self._lock:
            self._entries[fingerprint] = _now()
            if len(self._entries) > self._max:
                oldest_first = sorted(self._entries.items(), key=lambda kv: (kv[1], kv[0]))
                self._entries = dict(oldest_first[len(self._entries) - self._max:])
            self._flush()

    def _load(self) -> Dict[str, str]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            self._warn(f"Alert state file could not be read ({type(e).__name__}); starting with no history")
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            self._warn("Alert state file has an unexpected structure; starting with no history")
            return {}
        return {k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, str)}

    def _flush(self) -> None:
        tmp_path = None
        try:
            directory = os.path.dirname(self._path) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".alerts-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": STORE_VERSION, "entries": self._entries}, fh, sort_keys=True)
            os.replace(tmp_path, self._path)
            tmp_path = None
        except OSError as e:
            self._warn(f"Alert state could not be saved ({type(e).__name__}); it is kept in memory only")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            logger.warning("threshold alert store: %s", message)


def build_alert_store(state_file: Optional[str] = None) -> Any:
    """In-memory store by default; a local JSON file only when ``state_file`` is given."""
    if state_file:
        return JsonFileAlertStore(state_file)
    return InMemoryAlertStore()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------- de-duplicator
class ThresholdAlertDeduplicator:
    """Marks each crossed service of a threshold result as new or already notified."""

    def __init__(self, store: Optional[Any] = None):
        self._store = store if store is not None else InMemoryAlertStore()

    @property
    def store_kind(self) -> str:
        return getattr(self._store, "kind", "custom")

    def annotate(self, result: Dict[str, Any], *, mark: bool = False) -> Dict[str, Any]:
        """Return a copy of an ``evaluated`` threshold result with notification information.

        Each crossed service gets ``notification: {fingerprint, already_notified, notify}`` and the
        result gets a ``notifications`` block. With ``mark=True`` the new alerts are recorded as
        notified. Results that are not ``evaluated`` (disabled, invalid, errors) are returned as is.
        """
        if not isinstance(result, dict) or result.get("status") != "evaluated":
            return result

        out = copy.deepcopy(result)
        period = out.get("period") or {}
        key = period_key(period.get("start"), period.get("end_exclusive"))
        new: List[str] = []
        suppressed: List[str] = []
        for service in out.get("services", []):
            if not service.get("crossed"):
                continue
            fingerprint = alert_fingerprint(
                service["service_key"], key, Decimal(service["threshold_usd_exact"]),
                out.get("currency"), out.get("metric"),
            )
            seen = self._store.contains(fingerprint)
            service["notification"] = {"fingerprint": fingerprint, "already_notified": seen, "notify": not seen}
            (suppressed if seen else new).append(service["service_key"])
            if mark and not seen:
                self._store.add(fingerprint)

        out["notifications"] = {
            "new": new,
            "already_notified": suppressed,
            "dedupe_key": DEDUPE_KEY_DESCRIPTION,
            "store": self.store_kind,
            "marked_as_notified": bool(mark),
            "store_warnings": list(getattr(self._store, "warnings", [])),
        }
        return out
