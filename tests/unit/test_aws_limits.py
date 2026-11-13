"""Step 38.4 tests: AWS limits and sizing constants (source URL and verification date on every value)."""
from datetime import date

import pytest

import agent.services.aws_limits as limits
from agent.services.aws_limits import (
    AURORA_SERVERLESS_V2_VERIFICATION_REQUIREMENT,
    FARGATE_TASK_SIZE_SOURCE,
    FARGATE_TASK_SIZES,
    FARGATE_WINDOWS_MIN_CPU_UNITS,
    INVOCATION_ASYNCHRONOUS,
    INVOCATION_EVENT_SOURCE_MAPPING,
    INVOCATION_SYNCHRONOUS,
    LAMBDA_INVOCATION_TYPES,
    LAMBDA_LIMIT_KINDS,
    LAMBDA_LIMITS,
    LIMIT_APPLIES,
    LIMIT_NOT_APPLICABLE,
    LIMIT_NOT_DETERMINABLE,
    LIMIT_REQUEST_PAYLOAD_MB,
    LIMIT_RESPONSE_PAYLOAD_MB,
    LIMIT_TIMEOUT_SECONDS,
    OS_LINUX,
    OS_WINDOWS,
    AppliedLimit,
    SourcedValue,
    applicable_lambda_limit,
    is_valid_fargate_combination,
)


# ----------------------------------------------------------------- provenance
def all_sourced_values():
    return [*LAMBDA_LIMITS.values(), FARGATE_TASK_SIZE_SOURCE, FARGATE_WINDOWS_MIN_CPU_UNITS]


def test_every_sourced_value_has_an_aws_docs_url_and_an_iso_verification_date():
    for item in all_sourced_values():
        assert isinstance(item, SourcedValue)
        assert item.source_url.startswith("https://docs.aws.amazon.com/"), item
        date.fromisoformat(item.verified_on)          # raises if not an ISO date


def test_module_level_sourced_values_are_all_covered_by_the_provenance_check():
    module_values = {name for name, v in vars(limits).items() if isinstance(v, SourcedValue)}
    covered = {"LAMBDA_TIMEOUT_MAX_SECONDS", "LAMBDA_MANAGED_INSTANCES_TIMEOUT_MAX_SECONDS", "LAMBDA_MEMORY_MIN_MB",
               "LAMBDA_MEMORY_MAX_MB", "LAMBDA_SYNC_PAYLOAD_MAX_MB", "LAMBDA_ASYNC_PAYLOAD_MAX_MB",
               "LAMBDA_STREAMED_RESPONSE_MAX_MB", "LAMBDA_MEMORY_MB_PER_VCPU", "FARGATE_TASK_SIZE_SOURCE",
               "FARGATE_WINDOWS_MIN_CPU_UNITS"}
    assert module_values == covered


# ----------------------------------------------------------------- Lambda
def test_lambda_limits_match_the_documented_values():
    assert LAMBDA_LIMITS["timeout_max_seconds"].value == 900
    assert (LAMBDA_LIMITS["memory_min_mb"].value, LAMBDA_LIMITS["memory_max_mb"].value) == (128, 10240)
    assert LAMBDA_LIMITS["sync_payload_max_mb"].value == 6
    assert LAMBDA_LIMITS["memory_mb_per_vcpu"].value == 1769
    assert LAMBDA_LIMITS["timeout_max_seconds"].unit == "seconds"


def test_lambda_timeout_note_flags_the_managed_instances_exception():
    assert "Managed Instances" in LAMBDA_LIMITS["timeout_max_seconds"].note


def test_additional_documented_lambda_limits():
    assert LAMBDA_LIMITS["managed_instances_timeout_max_seconds"].value == 5400
    assert LAMBDA_LIMITS["async_payload_max_mb"].value == 1
    assert LAMBDA_LIMITS["streamed_response_max_mb"].value == 200
    assert set(LAMBDA_LIMITS) == {
        "timeout_max_seconds", "managed_instances_timeout_max_seconds", "memory_min_mb", "memory_max_mb",
        "sync_payload_max_mb", "async_payload_max_mb", "streamed_response_max_mb", "memory_mb_per_vcpu",
    }


# ----------------------------------------------------------------- Lambda limits by invocation type
def limit(kind, invocation, **kw):
    return applicable_lambda_limit(kind, invocation, **kw)


@pytest.mark.parametrize("invocation", LAMBDA_INVOCATION_TYPES)
def test_standard_timeout_is_900_for_every_invocation_type(invocation):
    result = limit(LIMIT_TIMEOUT_SECONDS, invocation)
    assert (result.status, result.value, result.unit) == (LIMIT_APPLIES, 900, "seconds")


def test_managed_instances_timeout_differs_only_for_async_and_event_source_mappings():
    assert limit(LIMIT_TIMEOUT_SECONDS, INVOCATION_SYNCHRONOUS, managed_instances=True).value == 900
    assert limit(LIMIT_TIMEOUT_SECONDS, INVOCATION_ASYNCHRONOUS, managed_instances=True).value == 5400
    assert limit(LIMIT_TIMEOUT_SECONDS, INVOCATION_EVENT_SOURCE_MAPPING, managed_instances=True,
                 event_source="sqs").value == 5400


@pytest.mark.parametrize("source", ["mq", "MQ", " documentdb "])
def test_managed_instances_event_source_exceptions_keep_900(source):
    result = limit(LIMIT_TIMEOUT_SECONDS, INVOCATION_EVENT_SOURCE_MAPPING, managed_instances=True, event_source=source)
    assert (result.status, result.value) == (LIMIT_APPLIES, 900)


def test_managed_instances_event_source_mapping_without_a_source_is_not_determinable():
    result = limit(LIMIT_TIMEOUT_SECONDS, INVOCATION_EVENT_SOURCE_MAPPING, managed_instances=True)
    assert result.status == LIMIT_NOT_DETERMINABLE and result.value is None


def test_request_payload_limit_depends_on_the_invocation_type():
    assert limit(LIMIT_REQUEST_PAYLOAD_MB, INVOCATION_SYNCHRONOUS).value == 6
    assert limit(LIMIT_REQUEST_PAYLOAD_MB, INVOCATION_ASYNCHRONOUS).value == 1
    esm = limit(LIMIT_REQUEST_PAYLOAD_MB, INVOCATION_EVENT_SOURCE_MAPPING)
    assert esm.status == LIMIT_NOT_DETERMINABLE and esm.value is None     # source-specific, never guessed


def test_response_payload_limit_exists_only_for_synchronous_invocations():
    sync = limit(LIMIT_RESPONSE_PAYLOAD_MB, INVOCATION_SYNCHRONOUS)
    assert (sync.status, sync.value) == (LIMIT_APPLIES, 6)
    for invocation in (INVOCATION_ASYNCHRONOUS, INVOCATION_EVENT_SOURCE_MAPPING):
        result = limit(LIMIT_RESPONSE_PAYLOAD_MB, invocation)
        assert result.status == LIMIT_NOT_APPLICABLE and result.value is None


def test_every_applied_limit_carries_its_source_and_verification_date():
    for kind in LAMBDA_LIMIT_KINDS:
        for invocation in LAMBDA_INVOCATION_TYPES:
            result = limit(kind, invocation)
            assert isinstance(result, AppliedLimit)
            assert result.source_url.startswith("https://docs.aws.amazon.com/lambda/")
            date.fromisoformat(result.verified_on)


@pytest.mark.parametrize("kind,invocation", [("memory", INVOCATION_SYNCHRONOUS), (None, INVOCATION_SYNCHRONOUS),
                                             (LIMIT_TIMEOUT_SECONDS, "batch"), (LIMIT_TIMEOUT_SECONDS, None)])
def test_unknown_kind_or_invocation_type_is_rejected(kind, invocation):
    with pytest.raises(ValueError):
        applicable_lambda_limit(kind, invocation)


# ----------------------------------------------------------------- Fargate table
def test_fargate_cpu_sizes_and_order():
    assert [s.cpu_units for s in FARGATE_TASK_SIZES] == [256, 512, 1024, 2048, 4096, 8192, 16384, 32768]
    assert [s.vcpus for s in FARGATE_TASK_SIZES] == [0.25, 0.5, 1, 2, 4, 8, 16, 32]


def test_fargate_memory_ranges_match_the_documented_table():
    by_cpu = {s.cpu_units: s.memory_mib for s in FARGATE_TASK_SIZES}
    assert by_cpu[256] == (512, 1024, 2048)
    assert by_cpu[512] == (1024, 2048, 3072, 4096)
    assert by_cpu[1024] == tuple(g * 1024 for g in range(2, 9))
    assert by_cpu[2048] == tuple(g * 1024 for g in range(4, 17))
    assert by_cpu[4096] == tuple(g * 1024 for g in range(8, 31))
    assert by_cpu[8192] == tuple(g * 1024 for g in range(16, 61, 4))
    assert by_cpu[16384] == tuple(g * 1024 for g in range(32, 121, 8))
    assert by_cpu[32768] == (61440, 122880, 249856)           # 60, 120, 244 GB


def test_fargate_operating_systems_and_platform_requirement():
    by_cpu = {s.cpu_units: s for s in FARGATE_TASK_SIZES}
    for cpu in (256, 512):
        assert by_cpu[cpu].operating_systems == (OS_LINUX,)
    for cpu in (1024, 2048, 4096):
        assert by_cpu[cpu].operating_systems == (OS_LINUX, OS_WINDOWS)
    for cpu in (8192, 16384, 32768):
        assert by_cpu[cpu].operating_systems == (OS_LINUX,) and by_cpu[cpu].requires_linux_platform_1_4
    assert not any(by_cpu[c].requires_linux_platform_1_4 for c in (256, 512, 1024, 2048, 4096))


def test_windows_never_uses_less_than_one_vcpu():
    windows_cpus = [s.cpu_units for s in FARGATE_TASK_SIZES if OS_WINDOWS in s.operating_systems]
    assert min(windows_cpus) == FARGATE_WINDOWS_MIN_CPU_UNITS.value == 1024


# ----------------------------------------------------------------- combination validator
@pytest.mark.parametrize("cpu,mem", [(256, 512), (256, 2048), (512, 4096), (1024, 8192), (2048, 4096),
                                     (4096, 30720), (8192, 16384), (8192, 61440), (16384, 122880),
                                     (32768, 249856)])
def test_documented_linux_combinations_are_valid(cpu, mem):
    assert is_valid_fargate_combination(cpu, mem) is True


@pytest.mark.parametrize("cpu,mem", [(256, 4096), (256, 256), (512, 512), (1024, 1024), (2048, 2048),
                                     (4096, 31744), (8192, 18432), (16384, 36864), (32768, 65536),
                                     (300, 512), (0, 512)])
def test_undocumented_combinations_are_invalid(cpu, mem):
    assert is_valid_fargate_combination(cpu, mem) is False


def test_windows_combinations_follow_the_windows_rows_only():
    assert is_valid_fargate_combination(1024, 2048, "WINDOWS") is True
    assert is_valid_fargate_combination(4096, 8192, "windows") is True
    assert is_valid_fargate_combination(256, 512, "WINDOWS") is False       # below 1 vCPU
    assert is_valid_fargate_combination(8192, 16384, "WINDOWS") is False    # Linux only
    assert is_valid_fargate_combination(1024, 2048, "SOLARIS") is False


@pytest.mark.parametrize("cpu,mem,os_family", [(True, 512, "LINUX"), (256, True, "LINUX"), (256.0, 512, "LINUX"),
                                              (256, "512", "LINUX"), (None, None, "LINUX"), (256, 512, None)])
def test_validator_never_raises_on_bad_types(cpu, mem, os_family):
    assert is_valid_fargate_combination(cpu, mem, os_family) is False


# ----------------------------------------------------------------- Aurora: no universal ACU limits
def test_no_fixed_aurora_capacity_constants_are_defined():
    names = [n for n in dir(limits) if not n.startswith("_")]
    assert not [n for n in names if "ACU" in n.upper().split("_") or "AURORA" in n.upper() and "CAPACITY" in n.upper()]
    assert not [n for n in names if n.upper().startswith("AURORA") and n != "AURORA_SERVERLESS_V2_VERIFICATION_REQUIREMENT"]


def test_aurora_requirement_states_engine_version_and_region_must_be_verified():
    text = AURORA_SERVERLESS_V2_VERIFICATION_REQUIREMENT
    assert "engine version" in text and "Region" in text and "NOT universal" in text
