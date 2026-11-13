# Alternative service recommendations

The existing Strands agent now has a complete **on-demand, read-only** path:
cost threshold → resource inventory and metrics → typed catalog rules → equivalent
workload cost comparison → recommendation report. The command-line runner uses the
same service. There is no migration, AWS write, schedule, email or SNS delivery.
The report itself is the on-demand notification. Live AWS validation remains a
separate deployment check; offline tests use fake clients only.

## Enable and run

Keep your private `agent/config.yaml` and AWS authentication on your machine.
For a single-account local test using an existing AWS CLI profile, replace the
example `accounts` section with the following, using your actual 12-digit ID:

```yaml
accounts:
  - account_id: "YOUR_12_DIGIT_ACCOUNT_ID"
    account_type: payer
    region: us-east-1
    use_current_credentials: true
```

Omit `role_arn` and `external_id` in this explicit mode. Set `AWS_PROFILE` to your
existing profile. The session verifies `sts:GetCallerIdentity` matches the account
before reading account data. This does not grant permissions or create/modify IAM
roles. Cross-account configurations continue to use their existing role ARNs.

Merge these sections into your actual configuration (do not replace the accounts,
model or other existing configuration):

```yaml
alternatives:
  enabled: true
threshold_detection:
  enabled: true
cost_thresholds:
  enabled: true
  currency: USD
  services:
    ec2: 0.01
    s3: 0.01
alternative_decisions:
  min_coverage_pct: 80
  ec2_to_fargate:
    headroom_pct: 20
    sizing_statistic: max
  attestation:
    max_age_days: 30
  pricing:
    hours_per_month: 730
    currency: USD
  cache:
    ttl_seconds: 86400
```

The cent thresholds are for testing. Set appropriate production thresholds later.
Lambda → Fargate additionally needs your policy values for
`alternative_decisions.lambda_to_fargate.min_monthly_invocations`,
`min_average_concurrency` and `min_active_duty_cycle_pct`. These intentionally have
no invented defaults. Optional `pricing.min_monthly_savings_usd` and
`pricing.min_savings_pct` require savings to meet **both** configured minima.
Leaving savings minima unset still requires strictly positive savings.

From the project root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m agent.recommend --config agent/config.yaml --request examples/recommendation-request.json
```

`examples/recommendation-request.json` checks month-to-date in `us-east-1`.
Omit `regions` to use each account's configured region. Only configured accounts
are inspected; omit `account_id` to inspect all of them. To select a period, give
both `start_date` and **exclusive** `end_date`, for example November 1 to November
13. Service spending is payer-level; it is never silently assigned to an instance.

An explicit alternative can be checked without reaching a threshold:

```json
{
  "alternative_id": "ec2-short-event-driven-to-lambda",
  "account_id": "YOUR_CONFIGURED_ACCOUNT_ID",
  "regions": ["us-east-1"],
  "resource_id": "YOUR_INSTANCE_ID",
  "workload_inputs": []
}
```

The Strands tools are `analyze_cost_recommendations` (threshold workflow) and
`evaluate_service_alternative` (explicit alternative). They register when
`alternatives.enabled` is true. The original evidence and threshold tools keep
their existing contracts. An earlier evidence-only prompt does not override the
new recommendation-tool instructions. No FastAPI server is required or added.

## Workload inputs

AWS cannot infer all application requirements. A missing statement, memory metric,
capacity benchmark, resource cost or exact price is reported as missing; it is
never replaced with a guessed passing value.

Each `workload_inputs` item selects **one** account, region and resource. Every
evidence record inside repeats that scope and includes:

```json
{
  "account_id": "YOUR_CONFIGURED_ACCOUNT_ID",
  "region": "us-east-1",
  "resource_id": "YOUR_RESOURCE_ID",
  "observed_at": "2026-11-13T00:00:00Z",
  "source": "Workload owner statement / benchmark report / CUR query reference",
  "value": true
}
```

For a complete input scaffold, copy
`examples/ec2-lambda-workload.example.json` to your private request file. Its
null values, placeholder dates and false confirmations intentionally block a
recommendation until you replace them with verified facts. For a different
CloudWatch memory schema, add a measured memory record as described below.

Dates must be timezone-aware and not in the future. Attestations expire according
to `attestation.max_age_days` when configured. Measurements must have been checked
within `cache.ttl_seconds` (default one day). An old measurement can be refreshed
only by checking its real source, not by changing a timestamp without verification.
Duplicate resource profiles and profiles outside the inspected scope are rejected.

Each profile needs `workload_assessment`, a record whose `value` is `true` only
after the owner has assessed **state, storage, networking, runtime/dependencies,
security and availability** requirements for the proposed alternative. This covers
application constraints the catalog/CloudWatch cannot establish. A benchmark is
still required for pricing equivalence; a passing catalog rule is not a production
migration guarantee. Use separate requests if alternative-specific assessments or
target architectures differ.

The profile's `evidence` maps the following keys to dated records:

| Alternative | Owner-supplied or measured input | Collected automatically |
| --- | --- | --- |
| EC2 → Lambda | `workload.event_driven`, `workload.lambda_invocation_type` (`synchronous`, `asynchronous`, `event_source_mapping`), `workload.max_event_duration_seconds`, `workload.max_request_payload_mb`, `workload.max_response_payload_mb` | Instance RAM; existing CWAgent peak memory when available |
| EC2 → ECS Fargate | `workload.containerizable` | Instance architecture/OS/vCPU/RAM, CPU peaks and memory measurement |
| Lambda → ECS Fargate | `workload.containerizable`, `workload.irreplaceable_lambda_behavior` (`absent`/`present`), `workload.trigger_replacement_assessed` | Invocation, duration and active-hour evidence; derived monthly invocation/concurrency/duty-cycle figures |
| RDS → Aurora Serverless v2 | `rds.aurora_version_mapping`, `rds.workload_capacity_estimate` | Exact source engine/version; regional `db.serverless` availability and engine-specific capacity bounds |
| NAT → gateway endpoints | `nat.s3.same_region_bytes`, `nat.s3.cross_region_bytes`, `nat.dynamodb.same_region_bytes`, `nat.dynamodb.cross_region_bytes` from deduplicated destination-attributed CUR/Flow Logs | Active NAT routes and active gateway endpoint prefix-list routes, per route table and destination service |

Default EC2 memory lookup uses an **existing** `CWAgent/mem_used_percent`, Maximum,
with exactly the `InstanceId` dimension. It never installs the CloudWatch agent.
For another schema or Windows memory metric, provide a measured
`ec2.memory_utilization_max_pct` record. Include:

```json
{
  "coverage": {"coverage_pct": 100, "window_days": 30},
  "collection_complete": true
}
```

Coverage is **raw** observed hours divided by expected hours across the entire
window, before zero filling. Standard metrics now emit this metadata too. Missing
counts are not treated as observations. Sparse Lambda metrics can therefore need
more evidence; the coverage policy is not automatically relaxed. Default minimum
history is 14 days and preferred collection window is 30 days.

The Aurora mapping record's `value` is:

```json
{
  "source_engine": "mysql",
  "source_version": "EXACT_SOURCE_VERSION",
  "target_engine": "aurora-mysql",
  "target_version": "EXACT_VERIFIED_TARGET_VERSION",
  "migration_assessed": true
}
```

For PostgreSQL use source `postgres`, target `aurora-postgresql`. The owner verifies
the migration mapping; the API checks regional target availability. The capacity
estimate value must contain `min_acu`, `average_acu`, `peak_acu`,
`features_assessed: true`, `performance_assessed: true`. All capacities must be in
order and within the **specific target version's** returned bounds. CPU alone is
never converted into ACUs. An API that does not return the needed bounds leaves
the result incomplete instead of using a universal ACU constant.

NAT traffic records also need `period_start`, `period_end_exclusive`,
`collection_complete: true` and a completed representative window. For pricing,
same-Region traffic must be **bidirectional and deduplicated**, with
`bidirectional: true` and `traffic_scope: "uncovered_route_tables"`. Count only
traffic still traversing the evaluated NAT from uncovered route tables. Do not
double-count ENIs or include traffic already using endpoints. Cross-Region and
non-S3/DynamoDB traffic cannot be offloaded by this mapping. S3 and DynamoDB
decisions are separate; a VPC endpoint flag alone does not prove route coverage.

## Cost inputs and equivalent workload

Each profile can contain a `pricing` record with the same scope, date and source.
The `value` member is not used for pricing; fields below live directly in the record.
Required confirmations are `workload_equivalent`, `performance_assessed`,
`overheads_assessed`, `commitments_assessed` (all true only after real assessment),
`currency: "USD"` and explicit `retained_monthly_cost_usd` (including zero).
Retained costs include commitments that continue after migration. Do not assume
public on-demand prices represent your current discounted bill.

Except for NAT's scoped processing estimate, supply `current_cost`, itself a
fresh dated scope/source record with `attribution: "resource"`, `currency: "USD"`,
`amount_usd`, `period_start`, `period_end_exclusive`. Use actual resource-attributed
CUR/reconciled billing, including relevant dependent costs. The code normalizes
this observed amount to the configured month length. Service totals from the
threshold alert are rejected as resource costs.

| Target | Additional pricing fields | Quantity calculation |
| --- | --- | --- |
| Lambda | `monthly_invocations`, `lambda_memory_mb`, `lambda_billed_duration_ms`, `target_architecture`, `architecture_assessed: true` | Requests; invocations × duration rounded up to milliseconds × configured GB |
| EC2 → Fargate | `target_os_family`, `target_architecture`, `monthly_billable_task_hours`, `task_billing_assessed: true` | Smallest valid CPU/RAM combination covering measured peaks plus configured headroom × task hours |
| Lambda → Fargate | Same Fargate fields plus benchmarked `target_peak_vcpu`, `target_peak_memory_mib` | Valid target task size from benchmark, not average Lambda concurrency treated as a capacity guarantee |
| Aurora | `aurora_node_count`, `aurora_storage_gb_month`, `aurora_io_mode` (`standard`/`io_optimized`), `io_mode_rates_assessed: true`; standard mode also `aurora_monthly_io_requests` | Average benchmarked ACUs × hours × nodes, storage, and applicable I/O |
| Gateway endpoints | Destination bytes above, `rates.nat_data_processing` | Eligible bytes normalized to month × NAT per-GB rate; documented gateway endpoint additional cost is zero |

Task hours must already include **number of tasks, image-pull/startup time and
per-task billing minima**. Qualify capacity with a workload benchmark that covers
peaks and availability. Large Linux Fargate sizes need
`platform_version_assessed: true`. Windows/ARM is rejected. Windows Fargate includes
a separate Windows license component. Multi-AZ RDS requires at least two assessed
Aurora nodes. Storage, backup, I/O, load balancers, queues, gateways, data transfer,
logs and other changed dependent costs must be included or explicitly assessed as
unchanged. Add changed costs using `additional_target_components`:

```json
{
  "name": "Assessed target dependent cost",
  "monthly_quantity": 100,
  "query": {
    "service_code": "AmazonEC2",
    "unit": "GB",
    "filters": {"usagetype": "EXACT_AWS_USAGE_TYPE"}
  }
}
```

For dependent services beyond the initial five, the price resolver accepts AWS
Price List service codes (including load balancers, queues, storage and logs).
No hand-entered unit rate is accepted. If a changed dependency cannot be priced by
the resolver, report insufficient data instead of omitting it.

### Exact rates

Standard target selectors are generated automatically from the evaluated service,
architecture, OS and billing mode. No price constants are embedded. Optional
`pricing.rates` overrides refine AWS Price List products; every rate query has
`service_code`, exact `unit`, and `filters` mapping AWS attribute names to exact
values. Region is always checked/injected by the resolver. Use existing billing
tools `describe_services`, `get_attribute_values`, `get_products` to inspect the
real SKU attributes rather than guessing names or selecting a cheapest SKU.
Overrides can specify the exact `usagetype` and other distinguishing attributes.
Protected usage-type suffixes still prevent selecting ARM for x86, a Windows
license as CPU infrastructure, or Aurora Serverless v1 instead of v2. Exact
selectors and units were checked against the public AWS regional catalog and
are covered by offline snapshot tests. The fixtures are price-format evidence,
not a live account validation or a substitute for fetching current prices.

| Rate key | Service | Unit | Required attributes / restrictions |
| --- | --- | --- | --- |
| `lambda_requests` | AWSLambda | Requests | `group`: AWS-Lambda-Requests or AWS-Lambda-Requests-ARM |
| `lambda_duration` | AWSLambda | Lambda-GB-Second | `group`: AWS-Lambda-Duration or AWS-Lambda-Duration-ARM |
| `fargate_cpu` | AmazonECS | hours | CPU usage-type suffix for Linux/x86, ARM or Windows |
| `fargate_memory` | AmazonECS | hours | Memory usage-type suffix; quantity is GB × task hours although AWS's unit string is hours |
| `fargate_windows_license` | AmazonECS | hours | Windows OS license usage type, separate from CPU infrastructure |
| `aurora_acu` | AmazonRDS | ACU-Hr | Target `databaseEngine`, exact mode-specific serverless SKU |
| `aurora_storage` | AmazonRDS | GB-Mo | Target engine/mode; standard MySQL uses AWS's shared `Any` engine SKU |
| `aurora_io` | AmazonRDS | IOs | Standard mode; MySQL uses the shared `Any` engine I/O SKU |
| `nat_data_processing` | AmazonEC2 | GB | `productFamily`: NAT Gateway, data processing usage type |

No match or multiple matches blocks pricing. Units must match exactly. Tiered
prices require a sourced `account_prior_units` value in the query for the same
aggregated billing usage class; the resolver integrates across contiguous tiers,
never selects the cheapest tier. Free-tier offsets, private discounts, taxes and
unmodelled rate conditions are not silently assumed. The response includes SKU,
rate codes, exact rates, effective/retrieval dates, quantities and assumptions.
Prices are cached per account, region and exact query with a bounded TTL.

## Outputs

| Status | Meaning |
| --- | --- |
| `recommended` | Compatible, equivalent-workload pricing complete and positive qualifying savings |
| `no_cost_advantage` | Compatible and priced, but not cheaper enough |
| `needs_information` | Missing/expired inputs, incomplete/denied collection, unset thresholds or pricing gaps |
| `incompatible` | A catalog incompatibility rule fired |
| `already_in_use` | Alternative already present in inspected scope or all NAT subject routes already covered |

Every decision has account/region/resource scope, matched rules or reasons, and
evidence sources. Inventory truncation and collection failures prevent a positive
recommendation. Presence is checked across **the listed regions only**, using
Lambda/ECS Fargate/Aurora inventory; uninspected regions are never claimed unused.
ECS Fargate is the target here; EKS Fargate presence is not inferred from ECS.

Reports are capped at 25 per alternative. Remaining resources are explicitly
unevaluated; request their `resource_id` individually. `preferred_recommendations`
chooses the lowest target monthly estimate among qualifying candidates for each
resource/subject. Never add savings for two competing replacements of one workload.
NAT savings exclude hourly gateway removal. S3 has no source-alternative mapping:
an S3 threshold breach correctly produces an alert without an invented replacement.

## Offline checks and your live validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q agent shared tests
```

For live validation: enable flags, confirm the read permissions, run without
workload inputs first, inspect the returned gaps, then add only verified scoped
facts and pricing inputs. Check both cheaper and costlier results; remove a needed
fact and confirm `needs_information`. Verify actual threshold amounts, scope,
metrics, SKU rates and formulas against your AWS account. None of this requires
creating additional AWS resources.

AWS reference sources used by the implementation:

- [Price List GetProducts](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_pricing_GetProducts.html)
- [CloudWatch agent metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/metrics-collected-by-CloudWatch-agent.html)
- [Aurora Serverless v2 requirements](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.requirements.html)
- [RDS engine version API](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DBEngineVersion.html)
- [Fargate pricing and billing duration](https://aws.amazon.com/fargate/pricing/)
- [Gateway endpoint pricing](https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html)
