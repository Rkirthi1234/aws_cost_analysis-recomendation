# Tools Reference

The agent provides 43 billing API tools + 8 specialized tools (Athena, date, analysis) + AWS Knowledge via MCP.

## Billing Tools (43 billing APIs)

All billing tools route through the payer account via `SessionManager` for org-wide data access.

### Cost Explorer

| Tool | Description |
|------|-------------|
| `get_cost_and_usage` | Cost and usage data with filtering and grouping |
| `get_cost_forecast` | Cost forecasts for future periods |
| `get_usage_forecast` | Usage forecasts for future periods |
| `get_dimension_values` | Available dimension values (services, regions, accounts) |
| `get_tags` | Tag keys and values for cost allocation |
| `get_cost_categories` | Cost category names and values |
| `get_anomalies` | Cost anomaly detection |
| `get_cost_and_usage_comparisons` | Period-over-period cost comparisons |
| `get_cost_comparison_drivers` | Drivers of cost changes between periods |

### Reserved Instances

| Tool | Description |
|------|-------------|
| `get_reservation_purchase_recommendation` | RI purchase recommendations |
| `get_reservation_coverage` | RI coverage analysis |
| `get_reservation_utilization` | RI utilization analysis |

### Savings Plans

| Tool | Description |
|------|-------------|
| `get_savings_plans_purchase_recommendation` | SP purchase recommendations |
| `get_savings_plans_utilization` | SP utilization analysis |
| `get_savings_plans_coverage` | SP coverage analysis |
| `get_savings_plans_details` | SP details |

### Cost Optimization Hub

| Tool | Description |
|------|-------------|
| `get_recommendation` | Single optimization recommendation |
| `list_recommendations` | List optimization recommendations |
| `list_recommendation_summaries` | Summarized recommendations by dimension |

### Compute Optimizer

| Tool | Description |
|------|-------------|
| `get_ec2_instance_recommendations` | EC2 rightsizing |
| `get_ebs_volume_recommendations` | EBS volume recommendations |
| `get_lambda_function_recommendations` | Lambda recommendations |
| `get_auto_scaling_group_recommendations` | ASG recommendations |
| `get_ecs_service_recommendations` | ECS service recommendations |
| `get_rds_db_instance_recommendations` | RDS instance recommendations |
| `get_idle_recommendations` | Idle resource recommendations |
| `get_enrollment_status` | Enrollment status |

### Budgets & Free Tier

| Tool | Description |
|------|-------------|
| `describe_budgets` | Budget configurations and tracking |
| `get_free_tier_usage` | Free Tier usage tracking |

### Pricing

| Tool | Description |
|------|-------------|
| `describe_services` | Available AWS services and attributes |
| `get_attribute_values` | Attribute values for a service |
| `get_products` | Product pricing information |

### Pricing Calculator

| Tool | Description |
|------|-------------|
| `get_preferences` | Pricing Calculator preferences |
| `get_workload_estimate` | Workload cost estimate |
| `list_workload_estimate_usage` | Workload estimate usage items |
| `list_workload_estimates` | List workload estimates |

### Billing Conductor

| Tool | Description |
|------|-------------|
| `list_billing_groups` | Billing groups |
| `list_billing_group_cost_reports` | Billing group cost reports |
| `get_billing_group_cost_report` | Single billing group cost report |
| `list_account_associations` | Account associations |
| `list_pricing_plans` | Pricing plans |
| `list_pricing_rules` | Pricing rules |
| `list_custom_line_items` | Custom line items |

## Athena Tools

| Tool | Description |
|------|-------------|
| `execute_cur_athena_query` | SQL queries against CUR data for resource-level cost analysis |
| `execute_vpc_flowlog_query` | SQL queries against VPC Flow Logs (per-account) |
| `execute_multi_account_vpc_flowlog_query` | Parallel VPC Flow Log queries across all member accounts |
| `get_cur_schema_info` | CUR table schema and query examples |
| `get_vpc_flowlog_schema_info` | VPC Flow Logs schema and query examples |

## AWS Knowledge (MCP)

| Tool | Description |
|------|-------------|
| `search_documentation` | Search AWS documentation |
| `read_documentation` | Read specific AWS doc pages |
| `recommend` | Related documentation recommendations |
| `get_regional_availability` | Service availability by region |

## Helper Tools

| Tool | Description |
|------|-------------|
| `get_current_date_context` | Current date and calculated date ranges |
| `suggest_followup_questions` | Generate follow-up query suggestions |
| `get_optimization_recommendations` | Quick optimization tips |

## Alternatives Evidence Tools (optional, disabled by default)

Registered only when `alternatives.enabled: true` in `agent/config.yaml` (see the [Configuration Guide](configuration.md#7-alternative-service-evidence-tools-alternatives)). They are in addition to the tools above and need the read-only permissions in [IAM Permissions, section 5](iam-permissions.md#5-optional-read-only-permissions-for-the-alternatives-evidence-tools).

| Tool | Description |
|------|-------------|
| `identify_high_cost_services` | Cost Explorer screening for a date range (`end_date` is exclusive): high-cost services, applicable alternative candidates and the evidence each one requires |
| `collect_usage_evidence` | Inventory, CloudWatch metrics, usage patterns and evidence gaps for one `alternative_id`, across all configured accounts unless `account_id` is given |
| `collect_resource_inventory` | Compact read-only inventory of one resource type (`ec2`, `lambda`, `ecs`, `rds`, `nat_gateway`) |

These tools gather evidence only. They never recommend an alternative, assess compatibility or estimate savings. Detailed resources are capped at 25 per response; aggregate counts, warnings, errors, missing permissions and evidence gaps are always preserved.

## Cost Threshold Alert Tool (optional, disabled by default)

Registered only when `threshold_detection.enabled: true` in `agent/config.yaml` (see the [Configuration Guide](configuration.md#8-cost-threshold-alert-tool-threshold_detection)). Independent of `alternatives.enabled`; it needs only `ce:GetCostAndUsage` on the payer role.

| Tool | Description |
|------|-------------|
| `detect_cost_threshold_alerts` | Compares each service configured under `cost_thresholds.services` (for example EC2 and S3) with its threshold and returns, per service: threshold status, actual cost, configured threshold, currency, period and notification status. Optional `start_date` / `end_date` (`end_date` exclusive; both or neither, default month-to-date) |

The tool is read-only and returns structured alert information only:

- It sends **no** notification (no e-mail, SNS or other delivery) and every response says `notification_sent: false`.
- A threshold alert is **not** an alternative-service recommendation. It does not evaluate compatibility, pricing or savings.
- `notification_status` uses the existing de-duplication: `new_alert`, `already_notified` or `not_applicable`. The tool never records an alert as notified; only a notification workflow that has accepted the notification may do that.
- Invalid configuration, a disabled `cost_thresholds` section, missing cost data, non-USD units and AWS API errors are reported as structured results, never as exceptions.

## Tool Routing

The agent automatically routes queries to the right tools:

- **Service-level costs** (e.g., "total EC2 cost") → Billing tools (`get_cost_and_usage`)
- **Resource-level details** (e.g., "which EC2 instances") → Athena tools (`execute_cur_athena_query`)
- **Network traffic** (e.g., "top talkers") → VPC Flow Logs tools (`execute_vpc_flowlog_query`)
- **Best practices** (e.g., "how to optimize S3") → AWS Knowledge MCP tools
- **Alternative-service evidence** (only when `alternatives.enabled` is true) → `identify_high_cost_services`, then `collect_usage_evidence` / `collect_resource_inventory`
- **Cost threshold alerts** (only when `threshold_detection.enabled` is true) → `detect_cost_threshold_alerts`
# Validated alternative-service tools

With `alternatives.enabled: true`, `analyze_cost_recommendations` executes the
on-demand threshold/evidence/compatibility/pricing/report flow.
`evaluate_service_alternative` evaluates one approved catalog alternative, with
optional `resource_id`, independently of a threshold. Both accept dated,
resource-scoped `workload_inputs`. They never invent missing application facts,
prices or resource cost attribution and never modify AWS resources or send email/SNS.
See [schemas, configuration and CLI usage](recommendations.md).

The local FastAPI interface also supports a date-driven two-step flow:
POST `/cost-analysis` returns ranked service costs and a saved `analysis_id`;
POST `/recommendations` consumes that ID and a selected service to evaluate
candidates without a cost threshold. The earlier HTTP threshold route is now
POST `/threshold-recommendations`. The Strands tool and CLI threshold contracts
are unchanged. See the same guide for request examples and analysis expiry.
