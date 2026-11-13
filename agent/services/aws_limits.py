"""AWS service limits and sizing constants used by the alternative-service decision foundation (Step 38.4).

Pure constants module: no AWS calls, no I/O. Every source-backed value carries the AWS documentation
URL it came from and the date it was last verified against that page, so a stale value can be found
and re-checked. These are the limits of Lambda functions (the source workload or the EC2->Lambda
target) and of ECS on AWS Fargate (the target task size); they never change an AWS resource.

LAMBDA LIMITS DO NOT ALL APPLY TO EVERY INVOCATION
    The Lambda quotas page documents different limits for different invocation types, so no single
    "Lambda limit" may be applied universally. Use ``applicable_lambda_limit`` to look up the limit
    that applies to an invocation type; it states explicitly when a limit does not apply or cannot
    be determined instead of guessing:

    =====================  ==========================  ==============================  ===========================
    limit                  synchronous                 asynchronous                    event source mapping
    =====================  ==========================  ==============================  ===========================
    timeout                900 s                       900 s (5,400 s on Lambda        900 s (5,400 s on Lambda
                                                       Managed Instances)              Managed Instances, except
                                                                                       Amazon MQ and DocumentDB)
    request payload        6 MB                        1 MB                            source-specific: not
                                                                                       determinable here
    response payload       6 MB (200 MB when the       not applicable (no response     not applicable
                           response is streamed;       is returned to the caller)
                           streaming is not modelled)
    =====================  ==========================  ==============================  ===========================

    Memory (128 MB to 10,240 MB) and the 1,769 MB per vCPU ratio apply to every function. The
    documentation uses "MB" for 1,024 KB, so the values are directly comparable with MiB figures.

    NOT MODELLED: the same quotas page also lists separate quotas for other Lambda compute types
    (Lambda MicroVMs, with an 8-hour maximum execution duration, and Lambda Web Functions, with their own
    request timeout, memory and vCPU quotas). Those are not the functions the catalog's rules reason about,
    so none of their values is used here and none of them may be applied to a function's limits.

NOT INCLUDED ON PURPOSE: Aurora Serverless v2 capacity (ACU) limits. They are not universal: the
allowed minimum and maximum capacity and the availability of features such as scaling to zero depend
on the Aurora engine version, the platform version and the Region. Later validation must look these
up for the specific engine version and Region instead of using a fixed number; see
``AURORA_SERVERLESS_V2_VERIFICATION_REQUIREMENT``.
"""
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

VERIFIED_ON = "2026-11-13"

LAMBDA_LIMITS_URL = "https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html"
FARGATE_TASK_SIZE_URL = "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html"


@dataclass(frozen=True)
class SourcedValue:
    """A constant together with where it came from and when it was last checked."""

    value: int
    unit: str
    source_url: str
    verified_on: str
    note: str = ""


# ----------------------------------------------------------------------------- Lambda
# Standard Lambda functions: the timeout is 900 seconds for every invocation type. A longer maximum
# exists only for Lambda Managed Instances functions and only for some invocation types (see below).
LAMBDA_TIMEOUT_MAX_SECONDS = SourcedValue(
    900, "seconds", LAMBDA_LIMITS_URL, VERIFIED_ON,
    "Standard functions, every invocation type; cannot be increased. Lambda Managed Instances have a "
    "longer maximum for some invocation types only (LAMBDA_MANAGED_INSTANCES_TIMEOUT_MAX_SECONDS).",
)
LAMBDA_MANAGED_INSTANCES_TIMEOUT_MAX_SECONDS = SourcedValue(
    5400, "seconds", LAMBDA_LIMITS_URL, VERIFIED_ON,
    "Lambda Managed Instances functions only, and only for asynchronous invocations and event source "
    "mapping invocations other than Amazon MQ and Amazon DocumentDB. Synchronous invocations stay at 900 s.",
)
LAMBDA_MEMORY_MIN_MB = SourcedValue(128, "MB", LAMBDA_LIMITS_URL, VERIFIED_ON, "1 MB increments.")
LAMBDA_MEMORY_MAX_MB = SourcedValue(10240, "MB", LAMBDA_LIMITS_URL, VERIFIED_ON, "1 MB increments.")
LAMBDA_SYNC_PAYLOAD_MAX_MB = SourcedValue(
    6, "MB", LAMBDA_LIMITS_URL, VERIFIED_ON, "Applies to the request and, separately, to the response (synchronous).",
)
LAMBDA_ASYNC_PAYLOAD_MAX_MB = SourcedValue(
    1, "MB", LAMBDA_LIMITS_URL, VERIFIED_ON, "Asynchronous invocation request payload.",
)
LAMBDA_STREAMED_RESPONSE_MAX_MB = SourcedValue(
    200, "MB", LAMBDA_LIMITS_URL, VERIFIED_ON,
    "Synchronous invocations that stream the response only. Response streaming is NOT modelled: the "
    "applicable-limit lookup uses the non-streamed 6 MB response limit.",
)
LAMBDA_MEMORY_MB_PER_VCPU = SourcedValue(
    1769, "MB", LAMBDA_LIMITS_URL, VERIFIED_ON,
    "At 1,769 MB a function has the equivalent of one vCPU; CPU scales in proportion to memory.",
)

LAMBDA_LIMITS: Dict[str, SourcedValue] = {
    "timeout_max_seconds": LAMBDA_TIMEOUT_MAX_SECONDS,
    "managed_instances_timeout_max_seconds": LAMBDA_MANAGED_INSTANCES_TIMEOUT_MAX_SECONDS,
    "memory_min_mb": LAMBDA_MEMORY_MIN_MB,
    "memory_max_mb": LAMBDA_MEMORY_MAX_MB,
    "sync_payload_max_mb": LAMBDA_SYNC_PAYLOAD_MAX_MB,
    "async_payload_max_mb": LAMBDA_ASYNC_PAYLOAD_MAX_MB,
    "streamed_response_max_mb": LAMBDA_STREAMED_RESPONSE_MAX_MB,
    "memory_mb_per_vcpu": LAMBDA_MEMORY_MB_PER_VCPU,
}

INVOCATION_SYNCHRONOUS = "synchronous"
INVOCATION_ASYNCHRONOUS = "asynchronous"
INVOCATION_EVENT_SOURCE_MAPPING = "event_source_mapping"
LAMBDA_INVOCATION_TYPES: Tuple[str, ...] = (
    INVOCATION_SYNCHRONOUS, INVOCATION_ASYNCHRONOUS, INVOCATION_EVENT_SOURCE_MAPPING,
)

LIMIT_TIMEOUT_SECONDS = "timeout_seconds"
LIMIT_REQUEST_PAYLOAD_MB = "request_payload_mb"
LIMIT_RESPONSE_PAYLOAD_MB = "response_payload_mb"
LAMBDA_LIMIT_KINDS: Tuple[str, ...] = (LIMIT_TIMEOUT_SECONDS, LIMIT_REQUEST_PAYLOAD_MB, LIMIT_RESPONSE_PAYLOAD_MB)

# Event source mapping sources that do NOT get the 5,400 s Managed Instances timeout (callers normalise
# the source to these names).
LAMBDA_ESM_SOURCES_WITHOUT_EXTENDED_TIMEOUT = frozenset({"mq", "documentdb"})

LIMIT_APPLIES = "applies"                       # ``value`` is the limit
LIMIT_NOT_APPLICABLE = "not_applicable"         # no such limit exists for this invocation type
LIMIT_NOT_DETERMINABLE = "not_determinable"     # a limit exists but this module has no universal value for it


@dataclass(frozen=True)
class AppliedLimit:
    """The result of looking up one Lambda limit for one invocation type.

    ``status`` is ``applies`` (``value`` is the limit), ``not_applicable`` (no such limit for this
    invocation type, so nothing can be exceeded) or ``not_determinable`` (the limit depends on
    something not modelled here; a caller must treat it as unknown and never as passed or failed).
    """

    kind: str
    invocation_type: str
    status: str
    value: Optional[int]
    unit: str
    source_url: str
    verified_on: str
    note: str = ""


def applicable_lambda_limit(
    kind: str,
    invocation_type: str,
    *,
    managed_instances: bool = False,
    event_source: Optional[str] = None,
) -> AppliedLimit:
    """The Lambda limit of ``kind`` that applies to ``invocation_type``.

    Args:
        kind: one of ``LAMBDA_LIMIT_KINDS``.
        invocation_type: one of ``LAMBDA_INVOCATION_TYPES``.
        managed_instances: True for Lambda Managed Instances functions (default: standard functions).
            Only the timeout of asynchronous and event-source-mapping invocations differs.
        event_source: normalised event source of an event-source-mapping invocation (``mq`` and
            ``documentdb`` do not get the Managed Instances 5,400 s timeout). Only needed for
            Managed Instances; if it is missing there the timeout is ``not_determinable``.

    Raises:
        ValueError: for an unknown ``kind`` or ``invocation_type`` (invalid input is never coerced).
    """
    if kind not in LAMBDA_LIMIT_KINDS:
        raise ValueError(f"kind must be one of {list(LAMBDA_LIMIT_KINDS)}, got {kind!r}")
    if invocation_type not in LAMBDA_INVOCATION_TYPES:
        raise ValueError(f"invocation_type must be one of {list(LAMBDA_INVOCATION_TYPES)}, got {invocation_type!r}")

    def make(status: str, source: Optional[SourcedValue], unit: str, note: str = "") -> AppliedLimit:
        return AppliedLimit(
            kind=kind, invocation_type=invocation_type, status=status,
            value=source.value if status == LIMIT_APPLIES and source is not None else None,
            unit=unit, source_url=LAMBDA_LIMITS_URL, verified_on=VERIFIED_ON, note=note,
        )

    if kind == LIMIT_TIMEOUT_SECONDS:
        standard = LAMBDA_TIMEOUT_MAX_SECONDS
        if not managed_instances or invocation_type == INVOCATION_SYNCHRONOUS:
            return make(LIMIT_APPLIES, standard, "seconds")
        extended = LAMBDA_MANAGED_INSTANCES_TIMEOUT_MAX_SECONDS
        if invocation_type == INVOCATION_ASYNCHRONOUS:
            return make(LIMIT_APPLIES, extended, "seconds")
        if event_source is None:
            return make(LIMIT_NOT_DETERMINABLE, None, "seconds",
                        "The Managed Instances timeout of an event source mapping depends on the event source.")
        if str(event_source).strip().lower() in LAMBDA_ESM_SOURCES_WITHOUT_EXTENDED_TIMEOUT:
            return make(LIMIT_APPLIES, standard, "seconds")
        return make(LIMIT_APPLIES, extended, "seconds")

    if kind == LIMIT_REQUEST_PAYLOAD_MB:
        if invocation_type == INVOCATION_SYNCHRONOUS:
            return make(LIMIT_APPLIES, LAMBDA_SYNC_PAYLOAD_MAX_MB, "MB")
        if invocation_type == INVOCATION_ASYNCHRONOUS:
            return make(LIMIT_APPLIES, LAMBDA_ASYNC_PAYLOAD_MAX_MB, "MB")
        return make(LIMIT_NOT_DETERMINABLE, None, "MB",
                    "The payload an event source mapping delivers depends on the event source; "
                    "no universal Lambda limit is defined here.")

    # response payload
    if invocation_type == INVOCATION_SYNCHRONOUS:
        return make(LIMIT_APPLIES, LAMBDA_SYNC_PAYLOAD_MAX_MB, "MB",
                    "Non-streamed response; streamed synchronous responses may be larger and are not modelled.")
    return make(LIMIT_NOT_APPLICABLE, None, "MB", "No response is returned to the caller for this invocation type.")


# ----------------------------------------------------------------------------- Fargate
OS_LINUX = "LINUX"
OS_WINDOWS = "WINDOWS"


@dataclass(frozen=True)
class FargateTaskSize:
    """One task-level CPU value and the memory values (MiB) AWS allows with it."""

    cpu_units: int                              # 1024 units = 1 vCPU
    memory_mib: Tuple[int, ...]                 # allowed task memory, in MiB (1 GB = 1024 MiB)
    operating_systems: Tuple[str, ...]
    requires_linux_platform_1_4: bool = False   # "requires Linux platform 1.4.0 or later"

    @property
    def vcpus(self) -> float:
        return self.cpu_units / 1024


def _gb(*values: int) -> Tuple[int, ...]:
    return tuple(v * 1024 for v in values)


def _gb_range(low: int, high: int, step: int) -> Tuple[int, ...]:
    return tuple(v * 1024 for v in range(low, high + 1, step))


_LINUX = (OS_LINUX,)
_LINUX_WINDOWS = (OS_LINUX, OS_WINDOWS)

# Valid task-level CPU/memory combinations, in the order AWS lists them.
FARGATE_TASK_SIZES: Tuple[FargateTaskSize, ...] = (
    FargateTaskSize(256, (512, 1024, 2048), _LINUX),                       # 0.25 vCPU: 512 MiB, 1 GB, 2 GB
    FargateTaskSize(512, _gb(1, 2, 3, 4), _LINUX),                         # 0.5 vCPU: 1-4 GB
    FargateTaskSize(1024, _gb_range(2, 8, 1), _LINUX_WINDOWS),             # 1 vCPU: 2-8 GB
    FargateTaskSize(2048, _gb_range(4, 16, 1), _LINUX_WINDOWS),            # 2 vCPU: 4-16 GB
    FargateTaskSize(4096, _gb_range(8, 30, 1), _LINUX_WINDOWS),            # 4 vCPU: 8-30 GB
    FargateTaskSize(8192, _gb_range(16, 60, 4), _LINUX, True),             # 8 vCPU: 16-60 GB, 4 GB steps
    FargateTaskSize(16384, _gb_range(32, 120, 8), _LINUX, True),           # 16 vCPU: 32-120 GB, 8 GB steps
    FargateTaskSize(32768, _gb(60, 120, 244), _LINUX, True),               # 32 vCPU: 60, 120, 244 GB
)

FARGATE_TASK_SIZE_SOURCE = SourcedValue(
    len(FARGATE_TASK_SIZES), "task CPU sizes", FARGATE_TASK_SIZE_URL, VERIFIED_ON,
    "Valid task-level CPU and memory combinations. Windows containers cannot use less than 1 vCPU. "
    "8, 16 and 32 vCPU sizes require Linux platform version 1.4.0 or later.",
)

FARGATE_WINDOWS_MIN_CPU_UNITS = SourcedValue(
    1024, "CPU units", FARGATE_TASK_SIZE_URL, VERIFIED_ON, "For Windows containers on Fargate the cpu value cannot be less than 1 vCPU.",
)


def is_valid_fargate_combination(cpu_units: int, memory_mib: int, os_family: str = OS_LINUX) -> bool:
    """True when ``cpu_units`` / ``memory_mib`` is a documented Fargate task size for ``os_family``.

    Never raises: unknown CPU values, non-integers, booleans and unknown operating systems are simply
    not valid combinations. It checks only the documented size table, not account quotas, Regions or
    platform-version availability.
    """
    if isinstance(cpu_units, bool) or isinstance(memory_mib, bool):
        return False
    if not isinstance(cpu_units, int) or not isinstance(memory_mib, int):
        return False
    if not isinstance(os_family, str):
        return False
    family = os_family.strip().upper()
    for size in FARGATE_TASK_SIZES:
        if size.cpu_units == cpu_units:
            return family in size.operating_systems and memory_mib in size.memory_mib
    return False


# ----------------------------------------------------------------------------- Aurora
AURORA_SERVERLESS_V2_VERIFICATION_REQUIREMENT = (
    "Aurora Serverless v2 capacity limits (minimum and maximum ACUs, and whether scaling to zero is "
    "supported) are NOT universal. They depend on the Aurora engine version, the platform version and "
    "the AWS Region and must be verified for the specific engine version and Region during validation; "
    "no fixed ACU limits are defined in this module."
)
