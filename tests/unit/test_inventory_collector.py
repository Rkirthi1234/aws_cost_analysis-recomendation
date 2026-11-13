"""Phase 2 tests: inventory collection (botocore Stubber validates every request/response shape)."""
import json
from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

from agent.services.inventory_collector import InventoryCollector
from agent.services.readonly_guard import ReadOnlySessionManager

LAUNCH = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
ACCT = "111122223333"


class StubbedSM:
    """Session-manager double handing out stubbed boto3 clients (one per service)."""

    def __init__(self, *services, region="us-east-1"):
        self.clients, self.stubs = {}, {}
        for svc in services:
            c = boto3.client(svc, region_name=region, aws_access_key_id="x", aws_secret_access_key="y")
            self.clients[svc] = c
            self.stubs[svc] = Stubber(c)
            self.stubs[svc].activate()

    def get_client(self, account_id, service_name, region=None):
        return self.clients[service_name]

    def finish(self):
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()


def collector(sm, **kw):
    return InventoryCollector(ReadOnlySessionManager(sm), **kw)


def denied(stub, op, code="UnauthorizedOperation"):
    stub.add_client_error(op, service_error_code=code, service_message="not authorized", http_status_code=403)


# ------------------------------------------------------------------------ EC2
def instance(iid, itype="m5.large", state="running", tags=None, platform="Linux/UNIX", ebs=1):
    return {
        "InstanceId": iid,
        "InstanceType": itype,
        "State": {"Name": state, "Code": 16},
        "PlatformDetails": platform,
        "Architecture": "x86_64",
        "LaunchTime": LAUNCH,
        "Placement": {"AvailabilityZone": "us-east-1a", "Tenancy": "default"},
        "BlockDeviceMappings": [{"DeviceName": f"/dev/sd{i}", "Ebs": {"VolumeId": f"vol-{i}"}} for i in range(ebs)],
        "NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}],
        "Tags": tags or [],
    }


M5 = {"InstanceType": "m5.large", "VCpuInfo": {"DefaultVCpus": 2}, "MemoryInfo": {"SizeInMiB": 8192},
      "BareMetal": False, "BurstablePerformanceSupported": False}
G5 = {"InstanceType": "g5.xlarge", "VCpuInfo": {"DefaultVCpus": 4}, "MemoryInfo": {"SizeInMiB": 16384},
      "GpuInfo": {"Gpus": [{"Count": 1}]}, "InstanceStorageInfo": {"TotalSizeInGB": 250}}


def test_ec2_inventory_normalizes_and_summarizes():
    sm = StubbedSM("ec2")
    sm.stubs["ec2"].add_response(
        "describe_instances",
        {"Reservations": [
            {"Instances": [instance("i-bbb", tags=[{"Key": "Name", "Value": "web"},
                                                   {"Key": "aws:autoscaling:groupName", "Value": "asg-web"}]),
                           instance("i-aaa", "g5.xlarge", platform="Windows", ebs=2)]},
            {"Instances": [instance("i-ccc", state="stopped")]},
        ]},
        {"Filters": [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}],
         "MaxResults": 500},
    )
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5, G5]},
                                 {"InstanceTypes": ["g5.xlarge", "m5.large"]})
    r = collector(sm).collect_ec2_instances(ACCT, "us-east-1", ecs_instance_ids=["i-ccc"])
    sm.finish()

    assert [x["instance_id"] for x in r["resources"]] == ["i-aaa", "i-bbb", "i-ccc"]  # sorted, deterministic
    a, b, c = r["resources"]
    assert a["platform"] == "Windows" and a["ebs_volume_count"] == 2
    assert a["specs"]["gpu_count"] == 1 and a["specs"]["instance_store_gb"] == 250 and a["specs"]["vcpus"] == 4
    assert b["name"] == "web" and b["autoscaling_group"] == "asg-web" and b["tag_count"] == 2
    assert b["specs"] == {"vcpus": 2, "memory_mib": 8192, "gpu_count": 0, "inference_accelerator_count": 0,
                          "instance_store_gb": 0, "bare_metal": False, "burstable": False}
    assert c["state"] == "stopped" and c["is_ecs_container_instance"] is True and b["is_ecs_container_instance"] is False
    assert r["summary"]["by_state"] == {"running": 2, "stopped": 1}
    assert r["summary"]["by_platform"] == {"Windows": 1, "Linux/UNIX": 2}
    assert r["summary"]["in_autoscaling_group"] == 1 and r["summary"]["ecs_container_instances"] == 1
    assert r["count"] == 3 and r["truncated"] is False and r["errors"] == []
    json.dumps(r)


def test_ec2_inventory_does_not_dump_all_tags():
    sm = StubbedSM("ec2")
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [{"Instances": [
        instance("i-1", tags=[{"Key": "secret-project", "Value": "confidential"}])]}]})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    r = collector(sm).collect_ec2_instances(ACCT)
    assert "confidential" not in json.dumps(r) and r["resources"][0]["tag_count"] == 1


def test_ec2_pagination_follows_next_token():
    sm = StubbedSM("ec2")
    sm.stubs["ec2"].add_response("describe_instances",
                                 {"Reservations": [{"Instances": [instance("i-1")]}], "NextToken": "t2"})
    sm.stubs["ec2"].add_response("describe_instances",
                                 {"Reservations": [{"Instances": [instance("i-2")]}]},
                                 {"Filters": ANY, "MaxResults": 500, "NextToken": "t2"})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    r = collector(sm).collect_ec2_instances(ACCT)
    sm.finish()
    assert [x["instance_id"] for x in r["resources"]] == ["i-1", "i-2"]


def test_ec2_truncation_is_reported_not_silent():
    sm = StubbedSM("ec2")
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [{"Instances": [
        instance("i-1"), instance("i-2"), instance("i-3")]}]})
    sm.stubs["ec2"].add_response("describe_instance_types", {"InstanceTypes": [M5]})
    r = collector(sm, max_resources=2).collect_ec2_instances(ACCT)
    assert r["truncated"] is True and r["total_seen"] == 3 and r["count"] == 2
    assert any("Showing 2 of 3" in w for w in r["warnings"])


def test_ec2_access_denied_is_reported_with_the_missing_permission():
    sm = StubbedSM("ec2")
    denied(sm.stubs["ec2"], "describe_instances")
    r = collector(sm).collect_ec2_instances(ACCT, "us-east-1")
    assert r["count"] == 0 and r["resources"] == []
    err = r["errors"][0]
    assert err["api"] == "ec2.describe_instances" and err["missing_permission"] == "ec2:DescribeInstances"
    assert err["optional"] is False


def test_ec2_instance_type_specs_are_optional():
    sm = StubbedSM("ec2")
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": [{"Instances": [instance("i-1")]}]})
    denied(sm.stubs["ec2"], "describe_instance_types")
    r = collector(sm).collect_ec2_instances(ACCT)
    assert r["count"] == 1 and r["resources"][0]["specs"] is None
    assert r["summary"]["instance_type_specs_available"] is False
    err = r["errors"][0]
    assert err["optional"] is True and err["missing_permission"] == "ec2:DescribeInstanceTypes"


def test_client_creation_failure_is_reported_not_raised():
    class Broken:
        def get_client(self, *a, **k):
            raise RuntimeError("Failed to assume role")

    r = InventoryCollector(ReadOnlySessionManager(Broken())).collect_ec2_instances(ACCT)
    assert r["count"] == 0 and r["errors"][0]["code"] == "RuntimeError"


# --------------------------------------------------------------------- Lambda
def fn(name, **kw):
    base = {"FunctionName": name, "FunctionArn": f"arn:aws:lambda:us-east-1:{ACCT}:function:{name}",
            "Runtime": "python3.12", "MemorySize": 512, "Timeout": 30, "PackageType": "Zip",
            "Architectures": ["x86_64"], "EphemeralStorage": {"Size": 512}, "Layers": [],
            "LastModified": "2026-09-01T00:00:00.000+0000"}
    base.update(kw)
    return base


def test_lambda_inventory_attaches_event_source_mappings_by_base_arn():
    sm = StubbedSM("lambda")
    st = sm.stubs["lambda"]
    st.add_response("list_functions", {"Functions": [fn("b")], "NextMarker": "m2"}, {"MaxItems": 50})
    st.add_response("list_functions", {"Functions": [fn("a", VpcConfig={"VpcId": "vpc-1"}, PackageType="Image")]},
                    {"MaxItems": 50, "Marker": "m2"})
    st.add_response("list_event_source_mappings", {"EventSourceMappings": [
        {"FunctionArn": f"arn:aws:lambda:us-east-1:{ACCT}:function:a:live",
         "EventSourceArn": "arn:aws:sqs:us-east-1:111122223333:q", "State": "Enabled"},
        {"FunctionArn": f"arn:aws:lambda:us-east-1:{ACCT}:function:other",
         "EventSourceArn": "arn:aws:kinesis:us-east-1:111122223333:stream/s", "State": "Enabled"},
    ]}, {"MaxItems": 100})
    r = collector(sm).collect_lambda_functions(ACCT, "us-east-1")
    sm.finish()
    a, b = r["resources"]
    assert (a["function_name"], b["function_name"]) == ("a", "b")
    assert a["event_source_mappings"] == [{"source_type": "sqs", "state": "Enabled"}]
    assert b["event_source_mappings"] == []
    assert a["vpc_attached"] is True and a["package_type"] == "Image" and b["memory_mb"] == 512
    assert r["summary"]["with_event_source_mappings"] == 1 and r["summary"]["in_use"] is True
    assert any("API Gateway" in w for w in r["warnings"])  # unobserved trigger types are disclosed


def test_lambda_presence_only_makes_exactly_one_list_call():
    sm = StubbedSM("lambda")
    sm.stubs["lambda"].add_response("list_functions", {"Functions": [fn("a")], "NextMarker": "more"})
    r = collector(sm).collect_lambda_functions(ACCT, presence_only=True)
    sm.finish()
    assert r["present"] is True and r["errors"] == [] and r["warnings"] == []


def test_lambda_presence_false_when_no_functions():
    sm = StubbedSM("lambda")
    sm.stubs["lambda"].add_response("list_functions", {"Functions": []})
    assert collector(sm).collect_lambda_functions(ACCT, presence_only=True)["present"] is False


def test_lambda_event_source_mappings_are_optional():
    sm = StubbedSM("lambda")
    sm.stubs["lambda"].add_response("list_functions", {"Functions": [fn("a")]})
    denied(sm.stubs["lambda"], "list_event_source_mappings", "AccessDeniedException")
    r = collector(sm).collect_lambda_functions(ACCT)
    assert r["count"] == 1
    assert r["errors"][0]["missing_permission"] == "lambda:ListEventSourceMappings"
    assert r["errors"][0]["optional"] is True


def test_lambda_list_denied():
    sm = StubbedSM("lambda")
    denied(sm.stubs["lambda"], "list_functions", "AccessDeniedException")
    r = collector(sm).collect_lambda_functions(ACCT)
    assert r["errors"][0]["missing_permission"] == "lambda:ListFunctions"


# ------------------------------------------------------------------------ ECS
CL_ARN = f"arn:aws:ecs:us-east-1:{ACCT}:cluster/prod"


def test_ecs_usage_detects_fargate_and_container_instances():
    sm = StubbedSM("ecs")
    st = sm.stubs["ecs"]
    st.add_response("list_clusters", {"clusterArns": [CL_ARN]}, {"maxResults": 100})
    st.add_response("describe_clusters", {"clusters": [{
        "clusterArn": CL_ARN, "clusterName": "prod", "status": "ACTIVE",
        "registeredContainerInstancesCount": 2, "runningTasksCount": 7, "activeServicesCount": 3,
        "statistics": [{"name": "activeFargateServiceCount", "value": "2"},
                       {"name": "RunningFargateTasksCount", "value": "4"},
                       {"name": "runningEC2TasksCount", "value": "3"}],
    }]}, {"clusters": [CL_ARN], "include": ["STATISTICS"]})
    st.add_response("list_container_instances", {"containerInstanceArns": ["ci-1", "ci-2"]},
                    {"cluster": CL_ARN, "maxResults": 100})
    st.add_response("describe_container_instances", {"containerInstances": [
        {"ec2InstanceId": "i-2"}, {"ec2InstanceId": "i-1"}]}, {"cluster": CL_ARN, "containerInstances": ["ci-1", "ci-2"]})
    r = collector(sm).collect_ecs_usage(ACCT, "us-east-1")
    sm.finish()
    c = r["resources"][0]
    assert c["active_fargate_services"] == 2 and c["running_fargate_tasks"] == 4 and c["running_tasks"] == 7
    assert r["container_instance_ec2_ids"] == ["i-1", "i-2"]
    assert r["summary"] == {"ecs_in_use": True, "fargate_in_use": True, "container_instance_count": 2}


def test_ecs_without_clusters_makes_no_further_calls():
    sm = StubbedSM("ecs")
    sm.stubs["ecs"].add_response("list_clusters", {"clusterArns": []})
    r = collector(sm).collect_ecs_usage(ACCT)
    sm.finish()
    assert r["summary"]["ecs_in_use"] is False and r["summary"]["fargate_in_use"] is False


def test_ecs_ec2_only_cluster_is_not_fargate_and_skips_instances_when_asked():
    sm = StubbedSM("ecs")
    st = sm.stubs["ecs"]
    st.add_response("list_clusters", {"clusterArns": [CL_ARN]})
    st.add_response("describe_clusters", {"clusters": [{
        "clusterArn": CL_ARN, "clusterName": "prod", "registeredContainerInstancesCount": 2,
        "statistics": [{"name": "activeFargateServiceCount", "value": "0"}]}]})
    r = collector(sm).collect_ecs_usage(ACCT, include_container_instances=False)
    sm.finish()
    assert r["summary"]["ecs_in_use"] is True and r["summary"]["fargate_in_use"] is False
    assert r["container_instance_ec2_ids"] == []


def test_ecs_list_denied():
    sm = StubbedSM("ecs")
    denied(sm.stubs["ecs"], "list_clusters", "AccessDeniedException")
    r = collector(sm).collect_ecs_usage(ACCT)
    assert r["errors"][0]["missing_permission"] == "ecs:ListClusters"


# ------------------------------------------------------------------------ RDS
def dbi(name, engine, klass="db.r5.large", cluster=None, replica_of=None):
    d = {"DBInstanceIdentifier": name, "Engine": engine, "EngineVersion": "15.4", "DBInstanceClass": klass,
         "DBInstanceStatus": "available", "MultiAZ": False, "StorageType": "gp3", "AllocatedStorage": 100,
         "InstanceCreateTime": LAUNCH}
    if cluster:
        d["DBClusterIdentifier"] = cluster
    if replica_of:
        d["ReadReplicaSourceDBInstanceIdentifier"] = replica_of
    return d


def test_rds_inventory_flags_source_engines_and_existing_serverless_v2():
    sm = StubbedSM("rds")
    st = sm.stubs["rds"]
    st.add_response("describe_db_instances", {"DBInstances": [
        dbi("pg", "postgres"), dbi("my", "mysql", replica_of="my-src"), dbi("maria", "mariadb"),
        dbi("au-1", "aurora-postgresql", "db.serverless", cluster="au"),
    ]}, {"MaxRecords": 100})
    st.add_response("describe_db_clusters", {"DBClusters": [{
        "DBClusterIdentifier": "au", "Engine": "aurora-postgresql", "EngineVersion": "15.4",
        "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 8.0},
        "DBClusterMembers": [{"DBInstanceIdentifier": "au-1"}]}]}, {"MaxRecords": 100})
    r = collector(sm).collect_rds(ACCT, "us-east-1")
    sm.finish()
    by = {x["db_instance_id"]: x for x in r["resources"]}
    assert by["pg"]["is_mysql_or_postgres"] and not by["pg"]["is_aurora"]
    assert by["my"]["is_read_replica"] and by["my"]["is_mysql_or_postgres"]
    assert not by["maria"]["is_mysql_or_postgres"]
    assert by["au-1"]["is_aurora"] and by["au-1"]["serverless_v2_instance"] and by["au-1"]["in_cluster"]
    assert r["clusters"][0]["serverless_v2"] and r["clusters"][0]["min_acu"] == 0.5
    assert r["summary"] == {"mysql_or_postgres_instances": 2, "aurora_clusters": 1, "serverless_v2_in_use": True}
    json.dumps(r)


def test_rds_clusters_are_optional():
    sm = StubbedSM("rds")
    sm.stubs["rds"].add_response("describe_db_instances", {"DBInstances": [dbi("pg", "postgres")]})
    denied(sm.stubs["rds"], "describe_db_clusters", "AccessDenied")
    r = collector(sm).collect_rds(ACCT)
    assert r["count"] == 1 and r["summary"]["serverless_v2_in_use"] is False
    assert r["errors"][0]["missing_permission"] == "rds:DescribeDBClusters" and r["errors"][0]["optional"]


def test_rds_instances_denied():
    sm = StubbedSM("rds")
    denied(sm.stubs["rds"], "describe_db_instances", "AccessDenied")
    r = collector(sm).collect_rds(ACCT)
    assert r["errors"][0]["missing_permission"] == "rds:DescribeDBInstances"


# ------------------------------------------------------------------------ NAT
def test_nat_inventory_detects_existing_gateway_endpoints_per_vpc():
    sm = StubbedSM("ec2")
    st = sm.stubs["ec2"]
    st.add_response("describe_nat_gateways", {"NatGateways": [
        {"NatGatewayId": "nat-b", "VpcId": "vpc-b", "SubnetId": "subnet-b", "State": "available", "CreateTime": LAUNCH},
        {"NatGatewayId": "nat-a", "VpcId": "vpc-a", "SubnetId": "subnet-a", "State": "available",
         "ConnectivityType": "public", "CreateTime": LAUNCH},
    ]}, {"Filter": [{"Name": "state", "Values": ["available"]}]})
    st.add_response("describe_vpc_endpoints", {"VpcEndpoints": [
        {"VpcEndpointId": "vpce-1", "VpcEndpointType": "Gateway", "ServiceName": "com.amazonaws.us-east-1.s3", "VpcId": "vpc-a"},
        {"VpcEndpointId": "vpce-2", "VpcEndpointType": "Gateway", "ServiceName": "com.amazonaws.us-east-1.dynamodb", "VpcId": "vpc-a"},
        {"VpcEndpointId": "vpce-3", "VpcEndpointType": "Interface", "ServiceName": "com.amazonaws.us-east-1.s3", "VpcId": "vpc-b"},
    ]}, {})
    r = collector(sm).collect_nat_gateways(ACCT, "us-east-1")
    sm.finish()
    a, b = r["resources"]
    assert (a["nat_gateway_id"], b["nat_gateway_id"]) == ("nat-a", "nat-b")
    assert a["vpc_has_s3_gateway_endpoint"] and a["vpc_has_dynamodb_gateway_endpoint"]
    assert not b["vpc_has_s3_gateway_endpoint"]  # an Interface endpoint is not a gateway endpoint
    assert r["summary"]["with_s3_gateway_endpoint"] == 1
    assert any("VPC Flow Logs" in w for w in r["warnings"])


def test_nat_endpoints_are_optional():
    sm = StubbedSM("ec2")
    sm.stubs["ec2"].add_response("describe_nat_gateways", {"NatGateways": [
        {"NatGatewayId": "nat-a", "VpcId": "vpc-a", "State": "available", "CreateTime": LAUNCH}]})
    denied(sm.stubs["ec2"], "describe_vpc_endpoints")
    r = collector(sm).collect_nat_gateways(ACCT)
    assert r["count"] == 1 and r["resources"][0]["vpc_has_s3_gateway_endpoint"] is False
    assert r["errors"][0]["missing_permission"] == "ec2:DescribeVpcEndpoints" and r["errors"][0]["optional"]


# --------------------------------------------------------------------- safety
def test_collector_rejects_invalid_max_resources():
    with pytest.raises(ValueError):
        InventoryCollector(object(), max_resources=0)


def test_collector_only_ever_uses_read_only_clients():
    sm = StubbedSM("ec2")
    sm.stubs["ec2"].add_response("describe_instances", {"Reservations": []})
    c = collector(sm)
    assert c.collect_ec2_instances(ACCT)["errors"] == []
    # The wrapped client given to the collector refuses writes outright.
    from agent.services.readonly_guard import ReadOnlyViolation
    with pytest.raises(ReadOnlyViolation):
        ReadOnlySessionManager(sm).get_client(ACCT, "ec2").terminate_instances


# ------------------------------------------------- Lambda pagination limit (R-5)
def test_lambda_page_limit_produces_an_explicit_truncation_warning():
    sm = StubbedSM("lambda")
    st = sm.stubs["lambda"]
    st.add_response("list_functions", {"Functions": [fn("a")], "NextMarker": "m2"}, {"MaxItems": 50})
    st.add_response("list_functions", {"Functions": [fn("b")], "NextMarker": "m3"}, {"MaxItems": 50, "Marker": "m2"})
    st.add_response("list_event_source_mappings", {"EventSourceMappings": []}, {"MaxItems": 100})
    r = collector(sm, max_pages=2).collect_lambda_functions(ACCT, "us-east-1")
    sm.finish()
    assert r["count"] == 2
    assert any("lambda.list_functions pagination stopped after 2 pages" in w and "incomplete" in w
               for w in r["warnings"])


def test_lambda_listing_that_fits_in_the_page_limit_has_no_truncation_warning():
    sm = StubbedSM("lambda")
    st = sm.stubs["lambda"]
    st.add_response("list_functions", {"Functions": [fn("a")], "NextMarker": "m2"}, {"MaxItems": 50})
    st.add_response("list_functions", {"Functions": [fn("b")]}, {"MaxItems": 50, "Marker": "m2"})
    st.add_response("list_event_source_mappings", {"EventSourceMappings": []}, {"MaxItems": 100})
    r = collector(sm, max_pages=2).collect_lambda_functions(ACCT, "us-east-1")
    sm.finish()
    assert not any("pagination stopped" in w for w in r["warnings"])


def test_lambda_default_page_limit_is_the_collector_max_pages_not_a_single_page():
    # Regression: the full listing used to receive an explicit max_pages, which also silenced the warning.
    sm = StubbedSM("lambda")
    st = sm.stubs["lambda"]
    st.add_response("list_functions", {"Functions": [fn("a")], "NextMarker": "m2"}, {"MaxItems": 50})
    st.add_response("list_functions", {"Functions": [fn("b")], "NextMarker": "m3"}, {"MaxItems": 50, "Marker": "m2"})
    st.add_response("list_functions", {"Functions": [fn("c")]}, {"MaxItems": 50, "Marker": "m3"})
    st.add_response("list_event_source_mappings", {"EventSourceMappings": []}, {"MaxItems": 100})
    r = collector(sm).collect_lambda_functions(ACCT, "us-east-1")
    sm.finish()
    assert [x["function_name"] for x in r["resources"]] == ["a", "b", "c"]
