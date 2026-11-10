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

## Tool Routing

The agent automatically routes queries to the right tools:

- **Service-level costs** (e.g., "total EC2 cost") → Billing tools (`get_cost_and_usage`)
- **Resource-level details** (e.g., "which EC2 instances") → Athena tools (`execute_cur_athena_query`)
- **Network traffic** (e.g., "top talkers") → VPC Flow Logs tools (`execute_vpc_flowlog_query`)
- **Best practices** (e.g., "how to optimize S3") → AWS Knowledge MCP tools
