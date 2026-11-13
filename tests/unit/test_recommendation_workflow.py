"""Offline behavioral tests: catalog decisions, evidence isolation, prices and orchestration.

All AWS clients are fakes. These tests never use real credentials or the network.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent.services.alternative_decision_settings import load_alternative_decision_settings
from agent.services.alternative_pricing import AWSPriceList, AlternativeCostComparison, PricingUnavailable
from agent.services.alternatives_catalog import AlternativesCatalog
from agent.services.compatibility_evaluator import CompatibilityEvaluator
from agent.services.recommendation_evidence import RecommendationEvidence, available, validate_record
from agent.services.recommendation_service import RecommendationService
from agent.tools.recommendation_tools import RecommendationTools, get_recommendation_tools
from agent.prompts.system_prompt import get_system_prompt

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)
AID = "111111111111"
REGION = "us-east-1"
EC2L = "ec2-short-event-driven-to-lambda"
EC2F = "ec2-containerized-to-fargate"
LF = "lambda-steady-high-volume-to-fargate"
RDS = "rds-mysql-postgres-to-aurora-serverless-v2"
NAT = "nat-gateway-to-gateway-endpoints"
CATALOG = AlternativesCatalog.load()
CONFIG = {"alternative_decisions": {"attestation": {"max_age_days": 30},
          "lambda_to_fargate": {"min_monthly_invocations": 100, "min_average_concurrency": 0.01,
                                "min_active_duty_cycle_pct": 10}},
          "cost_thresholds": {"enabled": True, "currency": "USD", "services": {"ec2": 0.01}}}
SCOPE = dict(account_id=AID, region=REGION, resource_id="i-test")


def entry(eid):
    return next(e for e in CATALOG.entries if e.id == eid)


def record(value=None, **kw):
    return {**SCOPE, "value": value, "observed_at": NOW.isoformat(), "source": "Workload owner benchmark / CUR query",
            "coverage": {"coverage_pct": 100, "window_days": 30}, "collection_complete": True, **kw}


def settings(config=None):
    return load_alternative_decision_settings(config or CONFIG)


def facts(eid):
    data = {
        EC2L: {"workload.event_driven": True, "workload.lambda_invocation_type": "synchronous",
               "workload.max_event_duration_seconds": 20, "workload.max_request_payload_mb": 1,
               "workload.max_response_payload_mb": 1, "ec2.memory_mib": 2048,
               "ec2.memory_utilization_max_pct": 25, "ec2.peak_memory_mib": 512},
        EC2F: {"workload.containerizable": True, "ec2.is_ecs_container_instance": False,
               "ec2.architecture": "x86_64", "ec2.platform": "Linux/UNIX", "ec2.target_os_family": "LINUX",
               "ec2.vcpus": 2, "ec2.cpu_max_pct": 10, "ec2.peak_vcpus_used": .2,
               "ec2.memory_mib": 2048, "ec2.memory_utilization_max_pct": 25, "ec2.peak_memory_mib": 512},
        LF: {"workload.irreplaceable_lambda_behavior": "absent", "workload.trigger_replacement_assessed": True,
             "workload.containerizable": True, "lambda.invocations_hourly_mean": 100000,
             "lambda.duration_avg_ms": 100, "lambda.active_hour_fraction": 1,
             "lambda.monthly_invocations": 73000000, "lambda.average_concurrency": 2.77,
             "lambda.active_duty_cycle_pct": 100},
        RDS: {"rds.engine": "mysql", "rds.engine_version": "8.0.35", "rds.region": REGION,
              "rds.aurora_version_mapping": {"target_version": "8.0.mysql_aurora.3.08.0"},
              "rds.serverless_v2_region_version_support": "supported",
              "rds.serverless_v2_capacity_limits": {"min_acu": .5, "max_acu": 128},
              "rds.workload_capacity_estimate": {"min_acu": .5, "average_acu": 1, "peak_acu": 4}},
        NAT: {"nat.route_tables_using_nat": ["rtb-a", "rtb-b"],
              **{f"nat.{s}.{k}": v for s in ("s3", "dynamodb") for k, v in {
                  "same_region_bytes": 1024**3, "cross_region_bytes": 0,
                  "covered_route_table_ids": ["rtb-a"], "uncovered_route_table_ids": ["rtb-b"],
                  "endpoint_coverage": "partial"}.items()}},
    }[eid]
    return {k: {**record(v), "status": "available"} for k, v in data.items()}


def evaluate(eid, observations=None, **kw):
    return CompatibilityEvaluator(settings(), lambda: NOW).evaluate(
        entry(eid), observations if observations is not None else facts(eid), **SCOPE, target_in_use=False, **kw)


@pytest.mark.parametrize("eid,subject", [(EC2L, None), (EC2F, None), (LF, None), (RDS, None), (NAT, "s3"), (NAT, "dynamodb")])
def test_all_five_catalog_alternatives_execute_compatible_rules(eid, subject):
    result = evaluate(eid, subject=subject)
    assert result["verdict"] == "compatible", result
    assert result["matched_rules"]


@pytest.mark.parametrize("eid,key,val", [(EC2L, "workload.event_driven", False), (EC2L, "workload.max_event_duration_seconds", 901),
    (EC2L, "workload.max_request_payload_mb", 7), (EC2L, "ec2.peak_memory_mib", 10241),
    (EC2F, "workload.containerizable", False), (EC2F, "ec2.architecture", "sparc"),
    (LF, "workload.irreplaceable_lambda_behavior", "present"), (RDS, "rds.serverless_v2_region_version_support", "unsupported")])
def test_known_incompatibilities_never_produce_recommendations(eid, key, val):
    obs = facts(eid)
    obs[key]["value"] = val
    assert evaluate(eid, obs)["verdict"] == "incompatible"


@pytest.mark.parametrize("pct,days,complete", [(79, 30, True), (100, 1, True), (100, 30, False)])
def test_raw_coverage_history_and_collection_completion_gate_compatibility(pct, days, complete):
    obs = facts(EC2F)
    obs["ec2.cpu_max_pct"].update(coverage={"coverage_pct": pct, "window_days": days}, collection_complete=complete)
    assert evaluate(EC2F, obs)["verdict"] == "insufficient_data"


def test_missing_memory_cannot_be_zero_or_assumed_to_fit():
    obs = facts(EC2L)
    del obs["ec2.peak_memory_mib"]
    assert evaluate(EC2L, obs)["verdict"] == "insufficient_data"


def test_attestation_expired_and_future_dates_block():
    for when, code in [(NOW - timedelta(days=31), "attestation_expired"), (NOW + timedelta(seconds=1), "evidence_missing")]:
        obs = facts(EC2L)
        obs["workload.event_driven"]["observed_at"] = when.isoformat()
        result = evaluate(EC2L, obs)
        assert result["verdict"] == "insufficient_data"
        assert code in {r["code"] for r in result["reasons"]}


def test_invalid_configuration_fails_closed_and_unset_threshold_is_reported():
    ev = CompatibilityEvaluator(settings({"alternative_decisions": {"min_coverage_pct": -1}}), lambda: NOW)
    assert ev.evaluate(entry(EC2F), facts(EC2F), **SCOPE, target_in_use=False)["configuration_issues"]
    ev = CompatibilityEvaluator(load_alternative_decision_settings({}), lambda: NOW)
    result = ev.evaluate(entry(LF), facts(LF), **SCOPE, target_in_use=False)
    assert result["verdict"] == "insufficient_data"
    assert "threshold_unset" in {r["code"] for r in result["reasons"]}


def test_presence_precedence_and_unknown_presence():
    ev = CompatibilityEvaluator(settings(), lambda: NOW)
    assert ev.evaluate(entry(EC2L), facts(EC2L), **SCOPE, target_in_use=True)["verdict"] == "already_in_use"
    assert ev.evaluate(entry(EC2L), facts(EC2L), **SCOPE, target_in_use=None)["verdict"] == "insufficient_data"


def test_async_payload_limit_and_unknown_event_source_mapping_limit():
    obs = facts(EC2L)
    obs["workload.lambda_invocation_type"]["value"] = "asynchronous"
    obs["workload.max_request_payload_mb"]["value"] = 2
    assert evaluate(EC2L, obs)["verdict"] == "incompatible"
    obs["workload.lambda_invocation_type"]["value"] = "event_source_mapping"
    assert evaluate(EC2L, obs)["verdict"] == "insufficient_data"


def test_nat_subjects_do_not_share_route_coverage_verdicts():
    obs = facts(NAT)
    obs["nat.s3.endpoint_coverage"]["value"] = "full"
    assert evaluate(NAT, obs, subject="s3")["verdict"] == "already_in_use"
    assert evaluate(NAT, obs, subject="dynamodb")["verdict"] == "compatible"


class NoAWS:
    def get_client(self, *args, **kw):
        raise AssertionError("Unexpected AWS call")


def ec2_region():
    resource = {"instance_id": "i-test", "platform": "Linux/UNIX", "architecture": "x86_64",
                "specs": {"memory_mib": 2048, "vcpus": 2}, "is_ecs_container_instance": False}
    metrics = {"metrics": {"cpu_max": {"max": 10}}, "collection_complete": True,
               "metric_coverage": {"cpu_max": {"coverage_pct": 100, "window_days": 30}}}
    return {"region": REGION, "inventory": {"resources": [resource], "count": 1},
            "ecs": {"count": 0, "summary": {"fargate_in_use": False}},
            "lambda_presence": {"count": 0}, "metrics": {"resources": {"i-test": metrics}}}


def ec2_inputs(eid=EC2L):
    return {**SCOPE, "workload_assessment": record(True), "evidence": {k: record(v["value"]) for k, v in facts(eid).items()
                                if entry(eid).evidence_requirement(k).origin in ("attestation", "planned")}}


def test_resolver_maps_resource_metrics_and_propagates_memory_coverage():
    rr = ec2_region()
    resolver = RecommendationEvidence(NoAWS(), settings(), lambda: NOW)
    obs, issues, scope = resolver.resolve(entry(EC2F), rr, rr["inventory"]["resources"][0], AID, ec2_inputs(EC2F))
    assert obs["ec2.cpu_max_pct"]["value"] == 10
    assert obs["ec2.peak_memory_mib"]["value"] == 512
    assert obs["ec2.peak_vcpus_used"]["value"] == .2
    assert CompatibilityEvaluator(settings(), lambda: NOW).evaluate(entry(EC2F), obs, **scope, target_in_use=False)["verdict"] == "compatible"


def test_foreign_scope_cannot_override_memory_or_inventory():
    rr = ec2_region()
    inputs = ec2_inputs()
    inputs["evidence"]["ec2.memory_utilization_max_pct"]["resource_id"] = "i-other"
    inputs["evidence"]["ec2.memory_mib"] = record(1)
    obs, _, _ = RecommendationEvidence(NoAWS(), settings(), lambda: NOW).resolve(entry(EC2L), rr, rr["inventory"]["resources"][0], AID, inputs)
    assert obs["ec2.memory_mib"]["value"] == 2048
    assert "ec2.peak_memory_mib" not in obs


def test_low_coverage_measurement_cannot_be_laundered_through_derived_values():
    rr, inputs = ec2_region(), ec2_inputs()
    inputs["evidence"]["ec2.memory_utilization_max_pct"]["coverage"]["coverage_pct"] = 1
    obs, _, _ = RecommendationEvidence(NoAWS(), settings(), lambda: NOW).resolve(entry(EC2L), rr, rr["inventory"]["resources"][0], AID, inputs)
    assert obs["ec2.peak_memory_mib"]["status"] == "missing"


def product(rate="0.1", unit="GB-Second", tiers=None, sku="sku-1", attrs=None):
    dimensions = tiers or [{"beginRange": "0", "endRange": "Inf", "unit": unit, "pricePerUnit": {"USD": rate}, "rateCode": "r1"}]
    return {"product": {"sku": sku, "attributes": {"regionCode": REGION, **(attrs or {})}, "productFamily": "Compute"},
            "publicationDate": "2026-11-01T00:00:00Z", "terms": {"OnDemand": {"t": {"effectiveDate": "2026-11-01T00:00:00Z",
            "offerTermCode": "OnDemand", "priceDimensions": {str(i): d for i, d in enumerate(dimensions)}}}}}


class PriceSM:
    def __init__(self, products, token=None):
        self.products, self.calls, self.token = products, 0, token

    def get_client(self, aid, service, region=None):
        assert service == "pricing"
        return self

    def get_products(self, **params):
        self.calls += 1
        return {"PriceList": [json.dumps(p) for p in self.products], **({"NextToken": self.token} if self.token else {})}


QUERY = {"service_code": "AWSLambda", "unit": "GB-Second", "filters": {"productFamily": "Compute"}}


def test_exact_prices_use_decimal_and_are_cached_without_aws_repeated_calls():
    sm = PriceSM([product("0.123456789")])
    prices = AWSPriceList(sm, now_fn=lambda: NOW)
    quote = prices.quote(AID, REGION, QUERY, "1000")
    assert quote["amount_exact"] == "123.456789000"
    prices.quote(AID, REGION, QUERY, 2000)
    assert sm.calls == 1


@pytest.mark.parametrize("products", [[], [product(), product(sku="sku-2")],
                                      [product(attrs={"regionCode": "eu-west-1"})]])
def test_missing_ambiguous_or_wrong_region_prices_are_rejected(products):
    with pytest.raises(PricingUnavailable):
        AWSPriceList(PriceSM(products), now_fn=lambda: NOW).quote(AID, REGION, QUERY, 100)


def test_incomplete_pagination_is_not_a_valid_price():
    with pytest.raises(PricingUnavailable, match="token"):
        AWSPriceList(PriceSM([product()], token="repeat"), now_fn=lambda: NOW).quote(AID, REGION, QUERY, 1)


def test_progressive_tiers_require_account_usage_and_never_pick_cheapest_tier():
    tiers = [{"beginRange": "0", "endRange": "100", "unit": "GB-Second", "pricePerUnit": {"USD": "1"}},
             {"beginRange": "100", "endRange": "Inf", "unit": "GB-Second", "pricePerUnit": {"USD": ".5"}}]
    prices = AWSPriceList(PriceSM([product(tiers=tiers)]), now_fn=lambda: NOW)
    with pytest.raises(PricingUnavailable, match="account_prior_units"):
        prices.quote(AID, REGION, QUERY, 150)
    assert Decimal(prices.quote(AID, REGION, {**QUERY, "account_prior_units": 50}, 100)["amount_exact"]) == 75


def pricing_profile():
    return record(currency="USD", workload_equivalent=True, performance_assessed=True, overheads_assessed=True,
                  commitments_assessed=True, retained_monthly_cost_usd=0, monthly_invocations=1000,
                  lambda_memory_mb=512, lambda_billed_duration_ms=100, target_architecture="x86_64", architecture_assessed=True,
                  current_cost=record(amount_usd=100, attribution="resource", currency="USD",
                                      period_start=(NOW-timedelta(hours=730)).isoformat(), period_end_exclusive=NOW.isoformat()),
                  rates={"lambda_requests": {"service_code": "AWSLambda", "unit": "Requests", "filters": {"group": "AWS-Lambda-Requests"}},
                         "lambda_duration": {"service_code": "AWSLambda", "unit": "Lambda-GB-Second", "filters": {"group": "AWS-Lambda-Duration"}}})


class FixedPrices:
    def __init__(self, price="1"):
        self.price, self.calls = price, []

    def quote(self, aid, region, query, quantity):
        self.calls.append((query, quantity))
        return {"amount_exact": self.price, "source": "fake AWS SKU", "quantity": str(quantity)}


def comparison(profile=None, price="1", decision=None):
    prices = FixedPrices(price)
    out = AlternativeCostComparison(prices, settings(), lambda: NOW).compare(entry(EC2L), decision or evaluate(EC2L), {},
                                                                            {"pricing": profile or pricing_profile()})
    return out, prices


def test_equivalent_workload_comparison_computes_savings_and_normalized_costs():
    result, prices = comparison()
    assert result["status"] == "estimated", result
    assert result["monthly_savings_usd"] == "98.00"
    assert result["savings_pct"] == "98.00"
    assert prices.calls[1][1] == 50  # 1000 calls x .1 sec x .5 GB
    assert result["recommend"] is True


def test_costlier_target_and_retained_commitment_costs_do_not_recommend():
    assert comparison(price="60")[0]["recommend"] is False
    p = pricing_profile()
    p["retained_monthly_cost_usd"] = 100
    assert comparison(p)[0]["recommend"] is False


@pytest.mark.parametrize("field,value", [("overheads_assessed", False), ("performance_assessed", False),
    ("commitments_assessed", False), ("currency", "INR"), ("lambda_memory_mb", 128), ("monthly_invocations", -1)])
def test_incomplete_or_invalid_pricing_never_returns_savings(field, value):
    p = pricing_profile()
    p[field] = value
    result, _ = comparison(p)
    assert result["status"] == "insufficient_data", result
    assert "monthly_savings_usd" not in result


def test_service_total_or_stale_baseline_cannot_be_resource_savings():
    for patch in [{"attribution": "service"}, {"observed_at": (NOW-timedelta(days=2)).isoformat()}]:
        p = pricing_profile()
        p["current_cost"].update(patch)
        assert comparison(p)[0]["status"] == "insufficient_data"


def test_prices_are_never_queried_before_compatible_verdict():
    result, prices = comparison(decision={"verdict": "incompatible"})
    assert result["status"] == "not_evaluated"
    assert prices.calls == []


class Registry:
    entries = {AID: SimpleNamespace(account_id=AID, region=REGION)}

    def get_account(self, aid):
        return self.entries[aid]


class FakeCollector:
    def __init__(self, rr=None):
        self.rr, self.calls = rr or ec2_region(), []

    def collect(self, eid, aid, regions):
        self.calls.append((eid, aid, regions))
        return {"regions": [copy.deepcopy(self.rr)], "evidence_gaps": ["Local dependencies require owner assessment"]}


class CostSource:
    def service_costs(self, start, end):
        return {"costs": {"Amazon Elastic Compute Cloud - Compute": Decimal("1")},
                "units": {"Amazon Elastic Compute Cloud - Compute": ["USD"]}, "metric": "AmortizedCost"}


def service(collector=None, config=None):
    return RecommendationService(config or CONFIG, NoAWS(), Registry(), now_fn=lambda: NOW,
                                 collector=collector or FakeCollector(), prices=FixedPrices(), cost_source=CostSource())


def test_offline_service_pipeline_collects_evaluates_prices_and_explains():
    profile = ec2_inputs()
    profile["pricing"] = pricing_profile()
    result = service().evaluate(EC2L, AID, [REGION], [profile])
    assert not result["errors"], result
    assert result["reports"][0]["status"] == "recommended", result
    assert result["reports"][0]["cost_comparison"]["monthly_savings_usd"] == "98.00"
    assert "Local dependencies" in result["reports"][0]["evidence_gaps"][0]


def test_pipeline_returns_missing_information_without_fabricating_application_facts():
    result = service().evaluate(EC2L)
    assert result["reports"][0]["status"] == "needs_information"
    assert result["reports"][0]["decision"]["reasons"]


def test_threshold_pipeline_crossing_calls_only_applicable_catalog_entries():
    collector = FakeCollector()
    result = service(collector).run("2026-11-01", "2026-11-13")
    assert result["thresholds"]["crossed_service_keys"] == ["ec2"]
    assert {c[0] for c in collector.calls} == {EC2L, EC2F, NAT}
    assert result["notification_sent"] is False
    assert result["aws_resources_changed"] is False


def test_below_threshold_and_disabled_configuration_make_no_inventory_calls():
    for patch in [{"enabled": False}, {"services": {"ec2": 10}}]:
        config, collector = copy.deepcopy(CONFIG), FakeCollector()
        config["cost_thresholds"].update(patch)
        result = service(collector, config).run("2026-11-01", "2026-11-13")
        assert result["alternatives"] == []
        assert collector.calls == []


def test_foreign_accounts_duplicate_profiles_and_unapproved_alternatives_rejected():
    svc = service()
    for kwargs in [{"account_id": "999999999999"}, {"workload_inputs": [ec2_inputs(), ec2_inputs()]}]:
        with pytest.raises(ValueError):
            svc.evaluate(EC2L, **kwargs)
    with pytest.raises(ValueError):
        svc.evaluate("unapproved-alternative")


def test_collection_permission_failure_blocks_positive_compatibility():
    rr, profile = ec2_region(), ec2_inputs()
    rr["inventory"]["errors"] = [{"code": "AccessDenied", "missing_permission": "ec2:DescribeInstances"}]
    result = service(FakeCollector(rr)).evaluate(EC2L, workload_inputs=[profile])
    assert result["reports"][0]["decision"]["verdict"] == "insufficient_data"


def test_tools_registration_prompt_and_structured_error_contract():
    class Config:
        def get(self, key, default=None):
            return False
    assert get_recommendation_tools(Config(), NoAWS(), Registry()) == []
    tools = RecommendationTools(service())
    assert len(tools.get_tools()) == 2
    result = json.loads(tools.evaluate_service_alternative("not-approved"))
    assert result["code"] == "InvalidInput"
    prompt = get_system_prompt(include_alternatives=True, include_recommendations=True)
    assert "analyze_cost_recommendations" in prompt
    assert "NEVER invent attestations" in prompt


def test_workload_assessment_is_required_before_compatibility_not_just_for_price():
    rr, inputs = ec2_region(), ec2_inputs()
    del inputs["workload_assessment"]
    result = service(FakeCollector(rr)).evaluate(EC2L, workload_inputs=[inputs])
    assert result["reports"][0]["decision"]["verdict"] == "insufficient_data"


def test_memory_collector_counts_raw_hours_and_ignores_zero_fill():
    times = [NOW-timedelta(hours=h) for h in range(1, 31*24)]
    class CW:
        def get_client(self, *args):
            return self
        def get_metric_data(self, **kw):
            return {"MetricDataResults": [{"Id": "memory", "StatusCode": "Complete", "Timestamps": times,
                                          "Values": [25]*len(times)}]}
    rr = ec2_region()
    rr["metrics"]["window"] = {"start": (NOW-timedelta(days=30)).isoformat(), "end_exclusive": NOW.isoformat()}
    out, _, _ = RecommendationEvidence(CW(), settings(), lambda: NOW).resolve(entry(EC2L), rr, rr["inventory"]["resources"][0], AID)
    assert out["ec2.memory_utilization_max_pct"]["coverage"]["observed_datapoints"] == 720
    assert out["ec2.peak_memory_mib"]["value"] == 512


def test_memory_pagination_failure_is_not_complete_even_with_many_points():
    class CW:
        def get_client(self, *args): return self
        def get_metric_data(self, **kw):
            return {"MetricDataResults": [{"Id": "memory", "StatusCode": "Forbidden", "Timestamps": [NOW-timedelta(hours=1)], "Values": [1]}]}
    rr = ec2_region()
    rr["metrics"]["window"] = {"start": (NOW-timedelta(days=30)).isoformat(), "end_exclusive": NOW.isoformat()}
    out, _, _ = RecommendationEvidence(CW(), settings(), lambda: NOW).resolve(entry(EC2L), rr, rr["inventory"]["resources"][0], AID)
    assert out["ec2.memory_utilization_max_pct"]["collection_complete"] is False
    assert out["ec2.peak_memory_mib"]["status"] == "missing"


class NatClient:
    def get_client(self, *args): return self
    def describe_route_tables(self, **kw):
        return {"RouteTables": [
            {"RouteTableId": "rtb-a", "Routes": [{"NatGatewayId": "nat-test", "State": "active"},
                                                  {"GatewayId": "vpce-s3", "DestinationPrefixListId": "pl-s3", "State": "active"}]},
            {"RouteTableId": "rtb-b", "Routes": [{"NatGatewayId": "nat-test", "State": "active"}]}]}
    def describe_vpc_endpoints(self, **kw):
        return {"VpcEndpoints": [{"VpcEndpointId": "vpce-s3", "VpcEndpointType": "Gateway", "State": "available",
                                  "ServiceName": f"com.amazonaws.{REGION}.s3", "RouteTableIds": ["rtb-a"]}]}


def test_nat_active_routes_determine_partial_coverage_not_vpc_flag():
    resource = {"nat_gateway_id": "i-test", "vpc_id": "vpc-test"}
    rr = {"region": REGION, "inventory": {"resources": [resource]}}
    # Use nat-test for the actual NAT routes, preserving resource-scoped record isolation.
    resource["nat_gateway_id"] = "nat-test"
    inputs = {"workload_assessment": record(True, resource_id="nat-test")}
    out, _, _ = RecommendationEvidence(NatClient(), settings(), lambda: NOW).resolve(entry(NAT), rr, resource, AID, inputs)
    assert out["nat.s3.endpoint_coverage"]["value"] == "partial"
    assert out["nat.s3.uncovered_route_table_ids"]["value"] == ["rtb-b"]
    assert out["nat.dynamodb.endpoint_coverage"]["value"] == "none"


class RDSClient:
    def get_client(self, *args): return self
    def describe_db_engine_versions(self, **kw):
        return {"DBEngineVersions": [{"ServerlessV2FeaturesSupport": {"MinCapacity": .5, "MaxCapacity": 128}}]}
    def describe_orderable_db_instance_options(self, **kw):
        return {"OrderableDBInstanceOptions": [{"DBInstanceClass": "db.serverless"}]}


def rds_inputs():
    return {**SCOPE, "workload_assessment": record(True), "evidence": {
        "rds.aurora_version_mapping": record({"source_engine": "mysql", "source_version": "8.0.35",
            "target_engine": "aurora-mysql", "target_version": "8.0.mysql_aurora.3.08.0", "migration_assessed": True}),
        "rds.workload_capacity_estimate": record({"min_acu": .5, "average_acu": 1, "peak_acu": 4,
                                                  "features_assessed": True, "performance_assessed": True})}}


def test_aurora_support_uses_exact_version_region_and_measured_capacity():
    res = {"db_instance_id": "i-test", "engine": "mysql", "engine_version": "8.0.35"}
    rr = {"region": REGION, "inventory": {"resources": [res]}}
    out, issues, scope = RecommendationEvidence(RDSClient(), settings(), lambda: NOW).resolve(entry(RDS), rr, res, AID, rds_inputs())
    assert not issues
    assert out["rds.serverless_v2_capacity_limits"]["value"]["max_acu"] == 128
    assert CompatibilityEvaluator(settings(), lambda: NOW).evaluate(entry(RDS), out, **scope, target_in_use=False)["verdict"] == "compatible"


@pytest.mark.parametrize("bad", [{"source_version": "different"}, {"migration_assessed": False}])
def test_unverified_aurora_mapping_never_becomes_supported(bad):
    res = {"db_instance_id": "i-test", "engine": "mysql", "engine_version": "8.0.35"}
    inputs = rds_inputs()
    inputs["evidence"]["rds.aurora_version_mapping"]["value"].update(bad)
    out, _, _ = RecommendationEvidence(NoAWS(), settings(), lambda: NOW).resolve(entry(RDS), {"region": REGION}, res, AID, inputs)
    assert out["rds.aurora_version_mapping"]["status"] == "missing"


def test_capacity_exceeding_verified_aurora_bounds_blocks_compatibility():
    res = {"db_instance_id": "i-test", "engine": "mysql", "engine_version": "8.0.35"}
    inputs = rds_inputs()
    inputs["evidence"]["rds.workload_capacity_estimate"]["value"]["peak_acu"] = 1000
    out, _, _ = RecommendationEvidence(RDSClient(), settings(), lambda: NOW).resolve(entry(RDS), {"region": REGION}, res, AID, inputs)
    assert out["rds.workload_capacity_estimate"]["status"] == "missing"


def test_nat_cost_savings_cover_processing_only_and_do_not_require_whole_nat_cost():
    decision = evaluate(NAT, subject="s3")
    decision["evidence"]["nat.s3.same_region_bytes"].update(
        period_start=(NOW-timedelta(hours=730)).isoformat(), period_end_exclusive=NOW.isoformat(),
        bidirectional=True, traffic_scope="uncovered_route_tables")
    p = pricing_profile()
    del p["current_cost"]
    p["rates"] = {"nat_data_processing": {"service_code": "AmazonEC2", "unit": "GB", "filters": {"productFamily": "NAT Gateway"}}}
    prices = FixedPrices("0.045")
    result = AlternativeCostComparison(prices, settings(), lambda: NOW).compare(entry(NAT), decision, {}, {"pricing": p})
    assert result["status"] == "estimated", result
    assert result["alternative_monthly_cost_usd"] == "0.00"
    assert result["scope"] == "eligible NAT data-processing only"
    assert result["recommend"] is True
    assert len(prices.calls) == 1


def test_nat_one_way_or_unattributed_bytes_cannot_claim_savings():
    decision = evaluate(NAT, subject="s3")
    decision["evidence"]["nat.s3.same_region_bytes"].update(period_start=(NOW-timedelta(days=30)).isoformat(),
        period_end_exclusive=NOW.isoformat(), bidirectional=False)
    result = AlternativeCostComparison(FixedPrices(), settings(), lambda: NOW).compare(entry(NAT), decision, {}, {"pricing": pricing_profile()})
    assert result["status"] == "insufficient_data"


def test_empty_inventory_never_implies_a_compatible_workload():
    result = service(FakeCollector({"region": REGION, "inventory": {"resources": [], "count": 0}})).evaluate(EC2L)
    assert result["reports"] == []
    assert result["recommendations"] == 0
    assert "no source resources" in result["warnings"][0]


def test_existing_target_in_another_inspected_region_prevents_unused_recommendation():
    rs = [ec2_region(), {"region": "us-west-2", "lambda_presence": {"count": 1}}]
    assert RecommendationService._presence(entry(EC2L), rs) is True
    rs[1]["lambda_presence"]["errors"] = [{"code": "AccessDenied"}]
    assert RecommendationService._presence(entry(EC2L), rs) is None


def test_missing_ecs_fargate_statistics_does_not_prove_target_unused():
    rr = ec2_region()
    rr["ecs"]["fargate_presence_complete"] = False
    assert RecommendationService._presence(entry(EC2F), [rr]) is None


def test_cheapest_candidate_selection_does_not_sum_competing_savings():
    svc = service()
    def fake_evaluate(eid, *args):
        reports = []
        if eid != NAT:
            decision = evaluate(eid)
            cost = "5" if eid == EC2L else "3"
            reports = [{"status": "recommended", "decision": decision,
                        "cost_comparison": {"current_monthly_cost_usd": "10", "alternative_monthly_cost_usd": cost,
                                            "monthly_savings_usd": str(10-int(cost)), "exact": {"alternative": cost}}}]
        return {"status": "evaluated", "reports": reports, "recommendations": len(reports)}
    svc.evaluate = fake_evaluate
    result = svc.run("2026-11-01", "2026-11-13")
    assert len(result["preferred_recommendations"]) == 1
    assert result["preferred_recommendations"][0]["alternative_id"] == EC2F


class PublicCatalogSM:
    """Offline fake GetProducts that applies AWS exact filter semantics to real product snapshots."""
    def __init__(self):
        self.products = json.loads((Path(__file__).parents[1]/"fixtures/aws_public_price_products.json").read_text())["products"]
    def get_client(self, *args): return self
    def get_products(self, ServiceCode, Filters, **kw):
        out = []
        for item in self.products:
            if item["serviceCode"] != ServiceCode:
                continue
            a = {**item["product"]["attributes"], "productFamily": item["product"]["productFamily"]}
            if all(a.get(f["Field"]) == f["Value"] for f in Filters):
                out.append(json.dumps(item))
        return {"PriceList": out}


@pytest.mark.parametrize("arch", ["x86_64", "arm64"])
def test_lambda_default_queries_match_actual_aws_catalog_units_and_regional_products(arch):
    p = pricing_profile()
    p["rates"] = {"lambda_duration": {"service_code": "AWSLambda", "unit": "Lambda-GB-Second",
                  "filters": {"group": "AWS-Lambda-Duration" + ("-ARM" if arch == "arm64" else "")}, "account_prior_units": 0}}
    p["target_architecture"] = arch
    prices = AWSPriceList(PublicCatalogSM(), now_fn=lambda: NOW)
    result = AlternativeCostComparison(prices, settings(), lambda: NOW).compare(entry(EC2L), evaluate(EC2L), {}, {"pricing": p})
    assert result["status"] == "estimated", result
    assert result["recommend"] is True
    assert result["target_components"][1]["unit"] == "Lambda-GB-Second"
    assert all(c["filters"]["regionCode"] == REGION for c in result["target_components"])


@pytest.mark.parametrize("eid,os_family,arch", [(EC2F, "LINUX", "x86_64"), (EC2F, "LINUX", "arm64"),
                                                (LF, "WINDOWS", "x86_64"), (LF, "LINUX", "x86_64")])
def test_fargate_defaults_match_real_cpu_memory_and_windows_license_skus(eid, os_family, arch):
    p = pricing_profile()
    p.update(rates={}, target_os_family=os_family, target_architecture=arch, monthly_billable_task_hours=730,
             task_billing_assessed=True, target_peak_vcpu=.25, target_peak_memory_mib=1024)
    obs = facts(eid)
    resource = {"architecture": arch}
    if eid == EC2F:
        obs["ec2.architecture"]["value"] = arch
    decision = evaluate(eid, obs)
    prices = AWSPriceList(PublicCatalogSM(), now_fn=lambda: NOW)
    result = AlternativeCostComparison(prices, settings(), lambda: NOW).compare(entry(eid), decision, resource, {"pricing": p})
    assert result["status"] == "estimated", result
    assert all(c["unit"] == "hours" for c in result["target_components"])
    assert len(result["target_components"]) == (3 if os_family == "WINDOWS" else 2)
    assert any("GB-Hours" in c["usage_type_suffix"] for c in result["target_components"])


@pytest.mark.parametrize("engine,mode", [("mysql", "standard"), ("mysql", "io_optimized"),
                                        ("postgres", "standard"), ("postgres", "io_optimized")])
def test_aurora_defaults_price_v2_not_v1_and_correct_storage_io_billing(engine, mode):
    p = pricing_profile()
    p.update(rates={}, aurora_node_count=2, aurora_storage_gb_month=100, aurora_io_mode=mode,
             io_mode_rates_assessed=True, aurora_monthly_io_requests=1000)
    obs = facts(RDS)
    obs["rds.aurora_version_mapping"]["value"]["target_engine"] = "aurora-mysql" if engine == "mysql" else "aurora-postgresql"
    decision = evaluate(RDS, obs)
    prices = AWSPriceList(PublicCatalogSM(), now_fn=lambda: NOW)
    result = AlternativeCostComparison(prices, settings(), lambda: NOW).compare(entry(RDS), decision, {"multi_az": True}, {"pricing": p})
    assert result["status"] == "estimated", result
    assert "ServerlessV2" in result["target_components"][0]["usage_type_suffix"]
    assert len(result["target_components"]) == (3 if mode == "standard" else 2)


def test_wrong_os_usage_type_cannot_underprice_fargate_even_if_user_overrides_rate():
    p = pricing_profile()
    p.update(target_os_family="LINUX", target_architecture="x86_64", monthly_billable_task_hours=730, task_billing_assessed=True)
    p["rates"] = {"fargate_cpu": {"service_code": "AmazonECS", "unit": "hours", "filters": {"usagetype": "USE1-Fargate-ARM-vCPU-Hours:perCPU"}}}
    result = AlternativeCostComparison(AWSPriceList(PublicCatalogSM(), now_fn=lambda: NOW), settings(), lambda: NOW).compare(
        entry(EC2F), evaluate(EC2F), {"architecture": "x86_64"}, {"pricing": p})
    assert result["status"] == "insufficient_data"


def test_expired_pricing_assessment_does_not_authorize_cost_comparison():
    p = pricing_profile()
    p["observed_at"] = (NOW-timedelta(days=31)).isoformat()
    result, prices = comparison(p)
    assert result["status"] == "insufficient_data"
    assert "expired" in result["reason"]
    assert prices.calls == []


def test_pipeline_exposes_configuration_issues_instead_of_falling_back():
    config = copy.deepcopy(CONFIG)
    config["alternative_decisions"]["min_coverage_pct"] = -1
    result = service(config=config).evaluate(EC2F)
    assert not result["errors"]
    decision = result["reports"][0]["decision"]
    assert decision["verdict"] == "insufficient_data"
    assert decision["configuration_issues"]


def test_pipeline_does_not_silently_accept_an_empty_mapping_in_place_of_profile_list():
    with pytest.raises(ValueError):
        service().evaluate(EC2L, workload_inputs={})


def test_detail_limit_explicitly_marks_remaining_resources_unevaluated():
    rr = ec2_region()
    rr["inventory"]["resources"] = [{**rr["inventory"]["resources"][0], "instance_id": f"i-{i}"} for i in range(30)]
    rr["inventory"]["count"] = 30
    result = service(FakeCollector(rr)).evaluate(EC2L)
    assert result["status"] == "partial"
    assert result["reports_shown"] == 25
    assert result["resources_seen"] == 30
    assert any("not evaluated" in w for w in result["warnings"])
