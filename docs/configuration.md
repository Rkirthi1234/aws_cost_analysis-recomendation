# Configuration Guide

The agent is configured via `agent/config.yaml`. Copy from the example template and edit:

```bash
cp agent/config.yaml.example agent/config.yaml
```

> **Before configuring**, ensure the required AWS infrastructure (CUR, Athena, VPC Flow Logs) is set up. See [Infrastructure Prerequisites](#infrastructure-prerequisites) at the bottom of this page.

## Full Configuration File

Below is the complete configuration structure. Each section is explained in detail after.

```yaml
# =============================================================================
# 1. AWS Region
# =============================================================================
aws:
  region: us-east-1

# =============================================================================
# 2. Account Configuration (REQUIRED)
# =============================================================================
accounts:
  - account_id: "<PAYER_ACCOUNT_ID>"
    role_arn: "arn:aws:iam::<PAYER_ACCOUNT_ID>:role/CostAnalyzerAgentPayerRole"
    account_type: payer
    athena:
      cur:
        database: <YOUR_CUR_DATABASE>

  - account_id: "<MEMBER_ACCOUNT_ID>"
    role_arn: "arn:aws:iam::<MEMBER_ACCOUNT_ID>:role/CostAnalyzerAgentMemberRole"
    account_type: member
    athena:
      vpc_flowlogs:
        database: <YOUR_VPC_DATABASE>

# =============================================================================
# 3. AgentCore Deployment
# =============================================================================
agentcore:
  execution_role_arn: ""

# =============================================================================
# 4. Agent Model
# =============================================================================
agent:
  model:
    provider: bedrock
    model_id: global.anthropic.claude-sonnet-4-5-20250929-v1:0
    temperature: 0.1
    max_tokens: 8192
    cache_tools: true
    cache_ttl: "5m"

# =============================================================================
# 5. MCP (Model Context Protocol)
# =============================================================================
mcp:
  enabled: true
  servers:
    aws_knowledge:
      type: http
      url: https://knowledge-mcp.global.api.aws
      enabled: true

# =============================================================================
# 6. Tools
# =============================================================================
tools:
  enabled:
    - date
    - athena
    - analysis
    - billing
    - mcp
```

---

## Section Details

### 1. AWS Region (`aws`)

| Field | Description | Default |
|-------|-------------|---------|
| `region` | AWS region for deployment and API calls | `us-east-1` |

### 2. Account Configuration (`accounts`)

This is the most important section. It defines which AWS accounts the agent accesses.

| Field | Description | Required |
|-------|-------------|----------|
| `account_id` | AWS account ID (12 digits) | Yes |
| `account_type` | `"payer"` or `"member"` | Yes |
| `role_arn` | IAM role ARN to assume for cross-account access | Payer: Yes, Member: No |
| `external_id` | External ID for confused deputy prevention | No |
| `region` | Override region for this account (defaults to `aws.region`) | No |
| `athena.cur.database` | Athena database containing CUR data | No |
| `athena.cur.table` | CUR table name (auto-discovered if omitted) | No |
| `athena.vpc_flowlogs.database` | Athena database containing VPC Flow Logs | No |
| `athena.vpc_flowlogs.table` | VPC Flow Logs table (auto-discovered if omitted) | No |

**Rules:**
- Exactly one payer account is required (must have `role_arn`)
- Member accounts may omit `role_arn` when the agent runs in the same account
- Athena config can appear on any account type
- When `table` is omitted, the agent auto-discovers it via AWS Glue Catalog

**Deployment scenarios:**

| Scenario | Description | When to use |
|----------|-------------|-------------|
| A: Centralized | Agent runs in same account as CUR/VPC data | Single account or shared logging account |
| B: Distributed | Agent runs in audit account, data in payer/member accounts | Multi-account with separate data locations |

See `agent/config.yaml.example` for complete examples of both scenarios.

### 3. AgentCore Deployment (`agentcore`)

| Field | Description | Default |
|-------|-------------|---------|
| `execution_role_arn` | IAM role ARN for the AgentCore runtime | Auto-created if empty |

When left empty, AgentCore auto-creates a role named `AmazonBedrockAgentCoreSDKRuntime-{region}-{hash}`. After deployment, attach the required inline policy — see [IAM Permissions](iam-permissions.md#2-agentcore-execution-role-agent-account).

### 4. Agent Model (`agent.model`)

| Field | Description | Default |
|-------|-------------|---------|
| `provider` | Model provider | `bedrock` |
| `model_id` | Bedrock model ID | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `temperature` | Response randomness (0.0–1.0) | `0.1` |
| `max_tokens` | Maximum output tokens per response | `8192` |
| `cache_tools` | Enable tool definition caching | `true` |
| `cache_ttl` | Cache time-to-live | `"5m"` |

**Prompt caching** reduces costs by ~90% and latency by ~85%. The system prompt and tool definitions are cached across requests.

| `cache_ttl` value | Best for |
|-------------------|----------|
| `"5m"` | Interactive CLI sessions (default) |
| `"1h"` | Batch processing, infrequent access |

### 5. MCP Configuration (`mcp`)

| Field | Description | Default |
|-------|-------------|---------|
| `enabled` | Enable/disable MCP servers | `true` |
| `servers.aws_knowledge.type` | Server transport type | `http` |
| `servers.aws_knowledge.url` | MCP server endpoint | `https://knowledge-mcp.global.api.aws` |
| `servers.aws_knowledge.enabled` | Enable this specific server | `true` |

The AWS Knowledge MCP server provides documentation search for cost optimization best practices. No additional cost.

### 6. Tools Configuration (`tools`)

| Tool | Description |
|------|-------------|
| `date` | Current date context for accurate time-based queries |
| `athena` | CUR and VPC Flow Log queries via Amazon Athena |
| `analysis` | Follow-up suggestions and optimization tips |
| `billing` | 43 AWS billing APIs (Cost Explorer, Compute Optimizer, etc.) |
| `mcp` | AWS Knowledge documentation search |

All tools are enabled by default. Remove a tool from the list to disable it.

---

## Customizing Prompts

Edit `shared/prompts.yaml` to add or modify the prompt library:

```yaml
custom:
  name: "My Team's Queries"
  icon: "🎯"
  prompts:
    - title: "Production Costs"
      prompt: "Show me costs for resources tagged with env=prod"
```

---

## Infrastructure Prerequisites

Before deploying, ensure the required AWS infrastructure is set up. See the following guides:

- **CUR setup:** [AWS Data Exports documentation](https://docs.aws.amazon.com/cur/latest/userguide/dataexports-create-standard.html) — export CUR to S3 in Parquet format with Athena integration enabled
- **VPC Flow Logs setup:** [VPC Flow Logs to S3 guide](https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-s3.html) — deliver to S3 in Parquet format with a [custom log format](#vpc-flow-logs-custom-log-format)
- **IAM roles:** [IAM Permissions](iam-permissions.md) — required policies for all four roles

### VPC Flow Logs Custom Log Format

The agent's VPC Flow Log queries rely on fields **not included in the default log format**. You must configure a custom log format when creating your VPC Flow Logs.

**Recommended custom log format:**

```
${version} ${account-id} ${interface-id} ${srcaddr} ${dstaddr} ${srcport} ${dstport} ${protocol} ${packets} ${bytes} ${start} ${end} ${action} ${log-status} ${vpc-id} ${subnet-id} ${instance-id} ${az-id} ${pkt-srcaddr} ${pkt-dstaddr} ${flow-direction} ${traffic-path}
```

**Required fields:**

| Field | Purpose |
|-------|---------|
| `srcaddr` | Source IP address |
| `dstaddr` | Destination IP address |
| `bytes` | Data transfer volume |
| `start` / `end` | Time range for traffic analysis |
| `action` | Filter accepted vs rejected traffic |
| `log-status` | Filter valid records |
| `instance-id` | Map traffic to EC2 instances |
| `az-id` | Cross-AZ traffic analysis |

**Optional but recommended:**

| Field | Purpose |
|-------|---------|
| `srcport` / `dstport` | Port-based traffic analysis |
| `protocol` | Protocol breakdown (TCP, UDP, ICMP) |
| `flow-direction` | Ingress vs egress analysis |
| `traffic-path` | Identify NAT Gateway, VPC peering, etc. |
| `pkt-srcaddr` / `pkt-dstaddr` | Original IPs when traffic traverses NAT |
| `vpc-id` / `subnet-id` | Network topology context |

> **Note:** Existing VPC Flow Logs with the default format cannot be retroactively updated. Create new flow logs with the custom format. See [AWS documentation](https://docs.aws.amazon.com/vpc/latest/userguide/flow-log-records.html#flow-logs-fields).
