"""Phase 1 tests: high-cost service identification (Cost Explorer faked, no AWS calls)."""
import copy
import json

import pytest
import yaml
from botocore.exceptions import ClientError

from agent.services.alternatives_catalog import DEFAULT_CATALOG_PATH, AlternativesCatalog
from agent.services.high_cost_identifier import HighCostServiceIdentifier

METRIC = "AmortizedCost"
START, END = "2026-10-01", "2026-11-01"

NAT_ID = "nat-gateway-to-gateway-endpoints"
RDS_ID = "rds-mysql-postgres-to-aurora-serverless-v2"
LAMBDA_ID = "lambda-steady-high-volume-to-fargate"
EC2_LAMBDA_ID = "ec2-short-event-driven-to-lambda"
EC2_FARGATE_ID = "ec2-containerized-to-fargate"

# (service, usage_type) -> cost.  Tax must be ignored; Kinesis is unmapped.
DATASET_A = [
    ("EC2 - Other", "USE1-NatGateway-Bytes", "300"),
    ("EC2 - Other", "USE1-NatGateway-Hours", "100"),
    ("EC2 - Other", "USE1-EBS:VolumeUsage.gp3", "50"),
    ("Amazon Elastic Compute Cloud - Compute", "USE1-BoxUsage:m5.large", "1000"),
    ("Amazon Relational Database Service", "USE1-InstanceUsage:db.r5.large", "800"),
    ("AWS Lambda", "USE1-Lambda-GB-Second", "600"),
    ("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "200"),
    ("Amazon Virtual Private Cloud", "USE1-PublicIPv4:InUseAddress", "40"),
    ("Tax", "Tax", "500"),
    ("Amazon Kinesis", "Shard-Hours", "10"),
]
DATASET_B = [("MySQL", "500"), ("PostgreSQL", "150"), ("Aurora MySQL", "100"), ("MariaDB", "50")]


def group(keys, amount, unit="USD"):
    return {"Keys": list(keys), "Metrics": {METRIC: {"Amount": str(amount), "Unit": unit}}}


def response(groups, token=None):
    resp = {"ResultsByTime": [{"Groups": groups}]}
    if token:
        resp["NextPageToken"] = token
    return resp


def client_error(code="AccessDeniedException", msg="denied"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "GetCostAndUsage")


class FakeCE:
    """Fake Cost Explorer. Records calls and any attempt to use another API."""

    def __init__(self, a=None, b=None, a_error=None, b_error=None, paged=False):
        self.a = DATASET_A if a is None else a
        self.b = DATASET_B if b is None else b
        self.a_error, self.b_error, self.paged = a_error, b_error, paged
        self.calls = []
        self.unexpected = []

    def __getattr__(self, name):  # any API other than get_cost_and_usage is a violation
        self.unexpected.append(name)
        raise AttributeError(name)

    def get_cost_and_usage(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        keys = [g["Key"] for g in kw["GroupBy"]]
        if keys == ["SERVICE", "USAGE_TYPE"]:
            if self.a_error:
                raise self.a_error
            groups = [group((s, u), c) for s, u, c in self.a]
            if self.paged:
                if "NextPageToken" not in kw:
                    return response(groups[:4], token="page2")
                return response(groups[4:])
            return response(groups)
        if keys == ["DATABASE_ENGINE"]:
            if self.b_error:
                raise self.b_error
            return response([group((e,), c) for e, c in self.b])
        raise AssertionError(f"unexpected GroupBy {keys}")


@pytest.fixture(scope="module")
def catalog():
    return AlternativesCatalog.load()


def make(catalog, ce=None, **kw):
    ce = ce or FakeCE()
    return HighCostServiceIdentifier(catalog=catalog, ce_client=ce, **kw), ce


def cand(result, entry_id):
    return next((c for c in result["candidates"] if c["alternative_id"] == entry_id), None)


# ---------------------------------------------------- high-cost identification
def test_ranking_top_n_and_tax_exclusion(catalog):
    ident, _ = make(catalog)
    r = ident.identify(START, END)
    names = [s["ce_service"] for s in r["high_cost_services"]]
    assert names == [
        "Amazon Elastic Compute Cloud - Compute",
        "Amazon Relational Database Service",
        "AWS Lambda",
        "EC2 - Other",
        "Amazon Simple Storage Service",
    ]
    assert "Tax" not in names
    assert r["total_cost_usd"] == 3100.0  # Tax excluded from total
    assert r["other_services"] == {"count": 2, "cost_usd": 50.0}
    assert r["high_cost_services"][0]["share_of_total_pct"] == 32.3
    assert [s["rank"] for s in r["high_cost_services"]] == [1, 2, 3, 4, 5]
    assert "top 5" in r["rule"]


def test_min_cost_floor_applies(catalog):
    ident, _ = make(catalog, min_cost_usd=500)
    r = ident.identify(START, END)
    assert [s["rank"] for s in r["high_cost_services"]] == [1, 2, 3]
    assert cand(r, NAT_ID) is None  # EC2 - Other ($450) fell below the floor


def test_top_n_override_and_unmapped_services_reported(catalog):
    ident, _ = make(catalog, top_n=7)
    r = ident.identify(START, END)
    assert r["unmapped_high_cost_services"] == ["Amazon Kinesis"]
    s3 = next(s for s in r["high_cost_services"] if s["service_key"] == "s3")
    assert s3["has_catalog_entry"] is False
    lam = next(s for s in r["high_cost_services"] if s["service_key"] == "lambda")
    assert lam["has_catalog_entry"] is True


def test_pagination_is_followed_and_results_match(catalog):
    paged, ce = make(catalog, FakeCE(paged=True))
    plain, _ = make(catalog)
    a = paged.identify(START, END)
    b = plain.identify(START, END)
    assert a["high_cost_services"] == b["high_cost_services"]
    a_calls = [c for c in ce.calls if [g["Key"] for g in c["GroupBy"]] == ["SERVICE", "USAGE_TYPE"]]
    assert len(a_calls) == 2 and a_calls[1]["NextPageToken"] == "page2"


def test_multi_month_results_are_summed(catalog):
    class TwoMonths(FakeCE):
        def get_cost_and_usage(self, **kw):
            base = super().get_cost_and_usage(**kw)
            return {"ResultsByTime": base["ResultsByTime"] * 2}

    ident, _ = make(catalog, TwoMonths())
    r = ident.identify("2026-09-01", END)
    assert r["total_cost_usd"] == 6200.0


def test_result_is_json_serializable(catalog):
    ident, _ = make(catalog)
    json.dumps(ident.identify(START, END))


# --------------------------------------------------------------- candidates
def test_candidates_ordered_by_scoped_cost_and_cost_only_status(catalog):
    ident, _ = make(catalog)
    r = ident.identify(START, END)
    # EC2 pairs (1000, instance hours only) tie and keep catalog order, then RDS, Lambda, NAT.
    assert [c["alternative_id"] for c in r["candidates"]] == [
        EC2_LAMBDA_ID, EC2_FARGATE_ID, RDS_ID, LAMBDA_ID, NAT_ID,
    ]
    assert all(c["status"] == "cost_screen_passed_usage_analysis_required" for c in r["candidates"])
    assert "not been evaluated" in r["note"]


def test_ec2_candidates_count_instance_hours_only(catalog):
    ident, _ = make(catalog)
    r = ident.identify(START, END)
    for entry_id in (EC2_LAMBDA_ID, EC2_FARGATE_ID):
        c = cand(r, entry_id)
        # BoxUsage 1000 only: EBS (50) and NAT (400) under "EC2 - Other" are excluded.
        assert c["scoped_cost_usd"] == 1000.0
        assert c["matched_ce_services"] == ["Amazon Elastic Compute Cloud - Compute"]
        assert c["share_of_matched_service_cost_pct"] == 100.0
        assert c["status"] == "cost_screen_passed_usage_analysis_required"


def test_ec2_candidates_absent_without_instance_hour_cost(catalog):
    only_nat = [("EC2 - Other", "USE1-NatGateway-Bytes", "300"), ("EC2 - Other", "USE1-EBS:VolumeUsage.gp3", "50")]
    ident, _ = make(catalog, FakeCE(a=only_nat))
    r = ident.identify(START, END)
    assert EC2_LAMBDA_ID not in [c["alternative_id"] for c in r["candidates"]]
    reasons = {n["alternative_id"] for n in r["not_applicable"]}
    assert {EC2_LAMBDA_ID, EC2_FARGATE_ID} <= reasons


def test_nat_cost_isolated_by_usage_type(catalog):
    ident, _ = make(catalog)
    c = cand(ident.identify(START, END), NAT_ID)
    assert c["scoped_cost_usd"] == 400.0  # bytes + hours only, EBS excluded
    assert c["share_of_matched_service_cost_pct"] == 88.9
    assert c["matched_ce_services"] == ["EC2 - Other"]


def test_database_engine_scope_excludes_aurora_and_mariadb(catalog):
    ident, ce = make(catalog)
    c = cand(ident.identify(START, END), RDS_ID)
    assert c["scoped_cost_usd"] == 650.0  # MySQL 500 + PostgreSQL 150
    b_call = next(x for x in ce.calls if [g["Key"] for g in x["GroupBy"]] == ["DATABASE_ENGINE"])
    assert b_call["Filter"]["Dimensions"]["Values"] == ["Amazon Relational Database Service"]


def test_lambda_whole_service_scope(catalog):
    ident, _ = make(catalog)
    c = cand(ident.identify(START, END), LAMBDA_ID)
    assert c["scoped_cost_usd"] == 600.0
    assert c["scope"]["kind"] == "whole_service"


def test_high_cost_vpc_without_nat_usage_is_not_a_candidate(catalog):
    a = [("Amazon Virtual Private Cloud", "USE1-PublicIPv4:InUseAddress", "900")]
    ident, _ = make(catalog, FakeCE(a=a))
    r = ident.identify(START, END)
    assert r["candidates"] == []
    assert r["not_applicable"][0]["alternative_id"] == NAT_ID


def test_no_high_cost_service_in_catalog_means_no_candidates(catalog):
    a = [("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "900"),
         ("Amazon Kinesis", "Shard-Hours", "100")]
    ident, ce = make(catalog, FakeCE(a=a))
    r = ident.identify(START, END)
    assert r["candidates"] == [] and r["not_applicable"] == []
    assert len(ce.calls) == 1  # no DATABASE_ENGINE call when RDS is not high-cost


def test_pending_entries_are_not_candidates():
    data = yaml.safe_load(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    data["alternatives"][0]["status"] = "pending_approval"
    cat = AlternativesCatalog.from_dict(data)
    ident, _ = make(cat)
    assert cand(ident.identify(START, END), NAT_ID) is None


# ------------------------------------------------- errors / missing permissions
def test_access_denied_on_main_query_returns_error_not_exception(catalog):
    ident, _ = make(catalog, FakeCE(a_error=client_error()))
    r = ident.identify(START, END)
    assert r["error"] is True and r["code"] == "AccessDeniedException"
    assert "ce:GetCostAndUsage" in r["hint"]


def test_other_client_error_has_no_hint(catalog):
    ident, _ = make(catalog, FakeCE(a_error=client_error("DataUnavailableException", "no data")))
    r = ident.identify(START, END)
    assert r["code"] == "DataUnavailableException" and "hint" not in r


def test_unexpected_exception_is_contained(catalog):
    class Boom(FakeCE):
        def get_cost_and_usage(self, **kw):
            raise RuntimeError("network down")

    ident, _ = make(catalog, Boom())
    r = ident.identify(START, END)
    assert r["error"] is True and r["exception_type"] == "RuntimeError"


def test_engine_breakdown_denied_degrades_gracefully(catalog):
    ident, _ = make(catalog, FakeCE(b_error=client_error()))
    r = ident.identify(START, END)
    c = cand(r, RDS_ID)
    assert c["scoped_cost_usd"] is None and c["share_of_matched_service_cost_pct"] is None
    assert any("DATABASE_ENGINE" in w and "AccessDeniedException" in w for w in r["warnings"])
    assert cand(r, LAMBDA_ID)["scoped_cost_usd"] == 600.0  # other candidates unaffected


def test_non_usd_unit_is_warned(catalog):
    a = [("AWS Lambda", "USE1-Lambda-GB-Second", "600")]

    class Eur(FakeCE):
        def get_cost_and_usage(self, **kw):
            resp = super().get_cost_and_usage(**kw)
            for g in resp["ResultsByTime"][0]["Groups"]:
                g["Metrics"][METRIC]["Unit"] = "EUR"
            return resp

    ident, _ = make(catalog, Eur(a=a))
    assert any("Non-USD unit" in w for w in ident.identify(START, END)["warnings"])


def test_unparseable_amount_is_skipped_with_warning(catalog):
    a = [("AWS Lambda", "x", "not-a-number"), ("Amazon DynamoDB", "y", "10")]
    ident, _ = make(catalog, FakeCE(a=a))
    r = ident.identify(START, END)
    assert any("unparseable" in w for w in r["warnings"])
    assert r["total_cost_usd"] == 10.0


@pytest.mark.parametrize(
    "start,end,code",
    [
        ("2026-1-01", "2026-11-01", "InvalidDate"),
        ("2026-02-30", "2026-11-01", "InvalidDate"),
        ("yesterday", "2026-11-01", "InvalidDate"),
        (None, "2026-11-01", "InvalidDate"),
        ("2026-11-01", "2026-11-01", "InvalidDateRange"),
        ("2026-11-02", "2026-11-01", "InvalidDateRange"),
    ],
)
def test_invalid_dates_are_rejected_without_calling_aws(catalog, start, end, code):
    ident, ce = make(catalog)
    r = ident.identify(start, end)
    assert r["error"] is True and r["code"] == code
    assert ce.calls == []


# ---------------------------------------------------- read-only / construction
def test_only_get_cost_and_usage_is_ever_called(catalog):
    ident, ce = make(catalog)
    ident.identify(START, END)
    assert ce.calls and ce.unexpected == []
    assert all(c["Granularity"] == "MONTHLY" and c["Metrics"] == [METRIC] for c in ce.calls)


class _Payer:
    account_id = "111111111111"


class _Registry:
    def __init__(self, payer=_Payer()):
        self._payer = payer

    def get_payer_account(self):
        return self._payer


class _SessionManager:
    def __init__(self, ce):
        self.ce, self.requests = ce, []

    def get_client(self, account_id, service_name, region=None):
        self.requests.append((account_id, service_name))
        return self.ce


def test_uses_existing_session_manager_for_payer_ce_client_once(catalog):
    sm = _SessionManager(FakeCE())
    ident = HighCostServiceIdentifier(sm, _Registry(), catalog)
    ident.identify(START, END)
    ident.identify(START, END)
    assert sm.requests == [("111111111111", "ce")]  # payer account, cached client


def test_requires_payer_account(catalog):
    with pytest.raises(ValueError, match="payer"):
        HighCostServiceIdentifier(_SessionManager(FakeCE()), _Registry(payer=None), catalog)


@pytest.mark.parametrize("kwargs", [{"top_n": 0}, {"min_cost_usd": -1}])
def test_invalid_rule_parameters_rejected(catalog, kwargs):
    with pytest.raises(ValueError):
        HighCostServiceIdentifier(catalog=catalog, ce_client=FakeCE(), **kwargs)


def test_catalog_and_clients_are_required():
    with pytest.raises(ValueError):
        HighCostServiceIdentifier(ce_client=FakeCE())
    with pytest.raises(ValueError):
        HighCostServiceIdentifier(catalog=AlternativesCatalog.load())


# ------------------------------------------------ warning dedupe (R-9) / read-only (R-4)
def test_non_usd_warning_is_deduplicated_per_service_and_unit(catalog):
    a = [("AWS Lambda", "u1", "10"), ("AWS Lambda", "u2", "20"), ("AWS Lambda", "u3", "30"),
         ("Amazon DynamoDB", "u4", "5"), ("Amazon DynamoDB", "u5", "6")]

    class Eur(FakeCE):
        def get_cost_and_usage(self, **kw):
            resp = super().get_cost_and_usage(**kw)
            for g in resp["ResultsByTime"][0]["Groups"]:
                g["Metrics"][METRIC]["Unit"] = "EUR"
            return resp

    ident, _ = make(catalog, Eur(a=a))
    warnings = [w for w in ident.identify(START, END)["warnings"] if "Non-USD" in w]
    assert len(warnings) == 2 and len(set(warnings)) == 2
    assert any("AWS Lambda" in w for w in warnings) and any("Amazon DynamoDB" in w for w in warnings)


def test_identifier_works_through_the_read_only_session_manager(catalog):
    from agent.services.readonly_guard import ReadOnlySessionManager, ReadOnlyViolation

    ce = FakeCE()
    sm = _SessionManager(ce)
    ident = HighCostServiceIdentifier(ReadOnlySessionManager(sm), _Registry(), catalog)
    result = ident.identify(START, END)
    assert result["high_cost_services"] and ce.unexpected == []
    assert sm.requests == [("111111111111", "ce")]
    with pytest.raises(ReadOnlyViolation):
        ident._get_ce().update_anomaly_monitor
