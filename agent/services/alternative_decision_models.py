"""Decision models for the alternative-service decision engine (Step 38.1).

Pure data module: no AWS calls, no I/O, and no dependency on the usage-evidence modules. Decision
results are deliberately a DIFFERENT structure from the usage-evidence output
(``usage_evidence`` / ``metrics_collector``): evidence describes what was observed, a decision
describes what the decision engine concluded from it. Nothing here changes evidence output.

VERDICTS (exactly four)
    compatible        the alternative can run the source workload (rules satisfied)
    incompatible      at least one rule definitively rules the alternative out
    already_in_use    the alternative is already used in the account, so it is not a new option
    insufficient_data the available evidence or configuration does not allow a verdict

REASON CODES
    The codes below all explain an ``insufficient_data`` verdict, so a decision that carries
    one of them must have that verdict (enforced in ``AlternativeDecision``). Which rule produced a
    ``compatible`` / ``incompatible`` verdict is identified by the catalog rule id (the compatibility
    evaluator, a later step, reports it); no separate reason code is needed for those verdicts.

    configuration_invalid          a safety-critical setting is invalid; the decision is blocked
    coverage_below_minimum         a required metric series covers less of the window than allowed
    evidence_missing               required evidence was not collected / is absent
    evidence_collection_failed     collecting required evidence failed (error, missing permission)
    attestation_expired            a required user attestation exists but is older than the configured
                                   ``attestation.max_age_days`` (a missing attestation is evidence_missing)
    threshold_unset                a rule needs a threshold that is intentionally unset (null) in the
                                   configuration; no default is invented, so no verdict can be reached

A ``configuration_invalid`` decision always lists the problems in ``configuration_issues`` so the
caller can show exactly which setting blocked the recommendation.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple


class Verdict(str, Enum):
    """The four possible outcomes of a decision (string-valued so results serialize as plain JSON)."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    ALREADY_IN_USE = "already_in_use"
    INSUFFICIENT_DATA = "insufficient_data"


class ReasonCode(str, Enum):
    """Structured reason codes for an ``insufficient_data`` verdict."""

    CONFIGURATION_INVALID = "configuration_invalid"
    COVERAGE_BELOW_MINIMUM = "coverage_below_minimum"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_COLLECTION_FAILED = "evidence_collection_failed"
    ATTESTATION_EXPIRED = "attestation_expired"
    THRESHOLD_UNSET = "threshold_unset"


# Reason codes that may only accompany an ``insufficient_data`` verdict.
INSUFFICIENCY_REASON_CODES = frozenset(ReasonCode)


@dataclass(frozen=True)
class DecisionReason:
    """One structured reason: a code, a human-readable message and optional machine-readable context."""

    code: ReasonCode
    message: str
    evidence_key: Optional[str] = None            # which required evidence key it concerns, if any
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, ReasonCode):
            raise ValueError(f"reason code must be a ReasonCode, got {self.code!r}")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("a decision reason needs a non-empty message")

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.evidence_key is not None:
            out["evidence_key"] = self.evidence_key
        if self.details:
            out["details"] = dict(self.details)
        return out


@dataclass(frozen=True)
class AlternativeDecision:
    """The decision for one catalog alternative (optionally scoped to one resource).

    Invariants (raise ``ValueError`` when broken, so an inconsistent decision can never be built):
      * ``insufficient_data`` requires at least one reason.
      * The four reason codes above only accompany ``insufficient_data``.
      * ``configuration_invalid`` requires non-empty ``configuration_issues`` (and only then are
        ``configuration_issues`` allowed).
    """

    alternative_id: str
    verdict: Verdict
    reasons: Tuple[DecisionReason, ...] = ()
    configuration_issues: Tuple[str, ...] = ()
    account_id: Optional[str] = None
    region: Optional[str] = None
    resource_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.alternative_id, str) or not self.alternative_id.strip():
            raise ValueError("alternative_id is required")
        if not isinstance(self.verdict, Verdict):
            raise ValueError(f"verdict must be a Verdict, got {self.verdict!r}")
        codes = {r.code for r in self.reasons}
        if self.verdict is Verdict.INSUFFICIENT_DATA and not self.reasons:
            raise ValueError("an insufficient_data decision needs at least one reason")
        if codes & INSUFFICIENCY_REASON_CODES and self.verdict is not Verdict.INSUFFICIENT_DATA:
            raise ValueError(
                f"reason codes {sorted(c.value for c in codes)} are only valid with an insufficient_data verdict"
            )
        invalid = ReasonCode.CONFIGURATION_INVALID in codes
        if invalid and not self.configuration_issues:
            raise ValueError("a configuration_invalid decision must list its configuration_issues")
        if self.configuration_issues and not invalid:
            raise ValueError("configuration_issues are only allowed with a configuration_invalid reason")

    @property
    def blocked_by_configuration(self) -> bool:
        return any(r.code is ReasonCode.CONFIGURATION_INVALID for r in self.reasons)

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serializable form. Contains no usage-evidence structures."""
        out: Dict[str, Any] = {
            "alternative_id": self.alternative_id,
            "verdict": self.verdict.value,
            "reasons": [r.as_dict() for r in self.reasons],
            "configuration_issues": list(self.configuration_issues),
        }
        for key in ("account_id", "region", "resource_id"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def insufficient_data(
    alternative_id: str,
    reasons: Tuple[DecisionReason, ...],
    *,
    account_id: Optional[str] = None,
    region: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> AlternativeDecision:
    """Build an ``insufficient_data`` decision (reasons that need configuration issues must use
    ``configuration_invalid`` instead)."""
    return AlternativeDecision(
        alternative_id=alternative_id,
        verdict=Verdict.INSUFFICIENT_DATA,
        reasons=tuple(reasons),
        account_id=account_id,
        region=region,
        resource_id=resource_id,
    )


def configuration_invalid(
    alternative_id: str,
    issues: Tuple[str, ...],
    *,
    account_id: Optional[str] = None,
    region: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> AlternativeDecision:
    """Build the decision returned when a safety-critical setting is invalid.

    The verdict is ``insufficient_data`` with the ``configuration_invalid`` reason, and every
    problem is exposed in ``configuration_issues``. The invalid value is never replaced by a default.
    """
    cleaned = tuple(str(i) for i in issues if str(i).strip())
    if not cleaned:
        raise ValueError("configuration_invalid needs at least one issue")
    reason = DecisionReason(
        code=ReasonCode.CONFIGURATION_INVALID,
        message="A safety-critical alternative_decisions setting is invalid; the recommendation is blocked.",
    )
    return AlternativeDecision(
        alternative_id=alternative_id,
        verdict=Verdict.INSUFFICIENT_DATA,
        reasons=(reason,),
        configuration_issues=cleaned,
        account_id=account_id,
        region=region,
        resource_id=resource_id,
    )
