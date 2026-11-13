# IAM Permissions

This document covers the four IAM roles required for the Cost Analyzer Agent. Each role serves a distinct purpose in the deployment and runtime flow.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Developer Machine / CI                                              │
│  ┌────────────────────────────────┐                                  │
│  │ 1. Client Policy               │  Deploy, invoke, connect         │
│  └────────────┬───────────────────┘                                  │
└───────────────┼──────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AgentCore Runtime (Agent Account)                                   │
│  ┌────────────────────────────────┐                                  │
│  │ 2. Execution Role              │  sts:AssumeRole → Payer/Member   │
│  └────────────┬───────────────────┘                                  │
└───────────────┼──────────────────────────────────────────────────────┘
                │
        ┌───────┴────────┐
        ▼                ▼
┌───────────────┐  ┌───────────────┐
│ Payer Account │  │ Member Accts  │
│ 3. Payer Role │  │ 4. Member Role│
│ (billing +    │  │ (Athena VPC   │
│  CUR Athena)  │  │  Flow Logs)   │
└───────────────┘  └───────────────┘
```

---

## 1. Client Policy (Deploy + Invoke)

Attach to the IAM user or role used for deploying and invoking the agent (the `agentcore` CLI or CI/CD).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DeployIAMRoleManagement",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:PassRole"
      ],
      "Resource": "arn:aws:iam::*:role/*BedrockAgentCore*"
    },
    {
      "Sid": "DeployCodeBuild",
      "Effect": "Allow",
      "Action": [
        "codebuild:StartBuild",
        "codebuild:BatchGetBuilds",
        "codebuild:CreateProject"
      ],
      "Resource": "arn:aws:codebuild:*:*:project/bedrock-agentcore-*"
    },
    {
      "Sid": "DeployECR",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:PutImage"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DeployS3",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::bedrock-agentcore-*"
    },
    {
      "Sid": "InvokeAgent",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:InvokeAgentRuntimeStreaming"
      ],
      "Resource": "arn:aws:bedrock-agentcore:*:*:runtime/*"
    },
    {
      "Sid": "AgentManagement",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:ListAgentRuntimes",
        "bedrock-agentcore:GetAgentRuntimeEndpoint"
      ],
      "Resource": "arn:aws:bedrock-agentcore:*:*:runtime/*"
    },
    {
      "Sid": "GetAccountInfo",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    }
  ]
}
```

Also attach the managed policy: `BedrockAgentCoreFullAccess` (required for the `agentcore` CLI to create, update, and manage agent runtimes).

> **Note:** `AmazonBedrockFullAccess` is **not needed**. Bedrock model invocation happens at runtime via the AgentCore execution role, not the deployer's credentials.

> **Tip:** For teams where deployers and end users are different people, you can split this into two policies: give analysts only the `InvokeAgent` + `AgentManagement` + `GetAccountInfo` statements, and give platform engineers the full policy above.

---

## 2. AgentCore Execution Role (Agent Account)

This is the IAM role that the AgentCore runtime assumes when running the agent. AgentCore auto-creates a role named `AmazonBedrockAgentCoreSDKRuntime-{region}-{hash}`. After deployment, attach an inline policy with the following permissions.

This role needs:
- `sts:AssumeRole` to assume roles in payer and member accounts
- Bedrock model invocation for Claude Sonnet 4.5
- S3/Glue access if CUR or VPC Flow Log Athena tables are in the same account (Scenario A)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumePayerAndMemberRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "arn:aws:iam::PAYER_ACCOUNT_ID:role/CostAnalyzerAgentPayerRole",
        "arn:aws:iam::MEMBER_ACCOUNT_ID:role/CostAnalyzerAgentMemberRole"
      ]
    },
    {
      "Sid": "BedrockModelInvocation",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:*:inference-profile/*anthropic.claude-*"
      ]
    },
    {
      "Sid": "MarketplaceModelAccess",
      "Effect": "Allow",
      "Action": [
        "aws-marketplace:Subscribe",
        "aws-marketplace:ViewSubscriptions"
      ],
      "Resource": "*"
    }
  ]
}
```

> **Note on Marketplace permissions:** Anthropic Claude models on Bedrock are served through AWS Marketplace. The `aws-marketplace:Subscribe` permission is required the first time a model is invoked in an account — Bedrock auto-subscribes on your behalf. Once the model is enabled in the account, subsequent invocations do not require marketplace permissions. If you prefer, a designated admin can enable the model once (via console or API), after which you can remove the marketplace permissions from the execution role.

> **Note on Bedrock resource ARNs:** The policy includes both `foundation-model` and `inference-profile` resource patterns. If your config uses a `global.*` model ID (e.g., `global.anthropic.claude-sonnet-4-5-20250929-v1:0`), that routes through a cross-region inference profile. If you use a direct model ID (e.g., `anthropic.claude-sonnet-4-5-20250929-v1:0`), that uses the foundation model ARN. Both are included for flexibility.

If Athena data (CUR or VPC Flow Logs) is in the same account as the agent (Scenario A — centralized logging), also add:

```json
{
  "Sid": "LocalAthenaAccess",
  "Effect": "Allow",
  "Action": [
    "athena:StartQueryExecution",
    "athena:GetQueryExecution",
    "athena:GetQueryResults",
    "athena:StopQueryExecution"
  ],
  "Resource": "arn:aws:athena:*:*:workgroup/*"
},
{
  "Sid": "LocalS3AthenaResults",
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"],
  "Resource": [
    "arn:aws:s3:::YOUR_ATHENA_RESULTS_BUCKET",
    "arn:aws:s3:::YOUR_ATHENA_RESULTS_BUCKET/*"
  ]
},
{
  "Sid": "LocalS3CURData",
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::YOUR_CUR_DATA_BUCKET",
    "arn:aws:s3:::YOUR_CUR_DATA_BUCKET/*"
  ]
},
{
  "Sid": "LocalS3VPCFlowLogData",
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::YOUR_VPC_FLOWLOG_DATA_BUCKET",
    "arn:aws:s3:::YOUR_VPC_FLOWLOG_DATA_BUCKET/*"
  ]
},
{
  "Sid": "LocalGlueDataCatalog",
  "Effect": "Allow",
  "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"],
  "Resource": "*"
}
```

Replace `PAYER_ACCOUNT_ID`, `MEMBER_ACCOUNT_ID`, and S3 bucket names with your actual values. Add additional member account ARNs as needed.

---

## 3. Payer Account Role (`CostAnalyzerAgentPayerRole`)

This role lives in the payer (management) account. The AgentCore execution role assumes it to call billing APIs and optionally query CUR data via Athena.

### Trust policy

Allow the AgentCore execution role to assume this role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::AGENT_ACCOUNT_ID:role/AmazonBedrockAgentCoreSDKRuntime-REGION-HASH"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "YOUR_EXTERNAL_ID"
        }
      }
    }
  ]
}
```

Remove the `Condition` block if you are not using external IDs.

### Permissions policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CostExplorer",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "ce:GetUsageForecast",
        "ce:GetDimensionValues",
        "ce:GetTags",
        "ce:GetCostCategories",
        "ce:GetAnomalies",
        "ce:GetCostAndUsageComparisons",
        "ce:GetCostComparisonDrivers",
        "ce:GetReservationPurchaseRecommendation",
        "ce:GetReservationCoverage",
        "ce:GetReservationUtilization",
        "ce:GetSavingsPlansPurchaseRecommendationDetails",
        "ce:GetSavingsPlansUtilizationDetails",
        "ce:GetSavingsPlansCoverage",
        "ce:GetSavingsPlansDetails"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CostOptimizationHub",
      "Effect": "Allow",
      "Action": [
        "cost-optimization-hub:GetRecommendation",
        "cost-optimization-hub:ListRecommendations",
        "cost-optimization-hub:ListRecommendationSummaries"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ComputeOptimizer",
      "Effect": "Allow",
      "Action": [
        "compute-optimizer:GetEC2InstanceRecommendations",
        "compute-optimizer:GetEBSVolumeRecommendations",
        "compute-optimizer:GetLambdaFunctionRecommendations",
        "compute-optimizer:GetAutoScalingGroupRecommendations",
        "compute-optimizer:GetECSServiceRecommendations",
        "compute-optimizer:GetRDSDBInstanceRecommendations",
        "compute-optimizer:GetIdleRecommendations",
        "compute-optimizer:GetEnrollmentStatus"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Budgets",
      "Effect": "Allow",
      "Action": [
        "budgets:ViewBudget"
      ],
      "Resource": "*"
    },
    {
      "Sid": "FreeTier",
      "Effect": "Allow",
      "Action": [
        "freetier:GetFreeTierUsage"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Pricing",
      "Effect": "Allow",
      "Action": [
        "pricing:DescribeServices",
        "pricing:GetAttributeValues",
        "pricing:GetProducts"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PricingCalculator",
      "Effect": "Allow",
      "Action": [
        "bcm-pricing-calculator:GetPreferences",
        "bcm-pricing-calculator:GetWorkloadEstimate",
        "bcm-pricing-calculator:ListWorkloadEstimateUsage",
        "bcm-pricing-calculator:ListWorkloadEstimates"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BillingConductor",
      "Effect": "Allow",
      "Action": [
        "billingconductor:ListBillingGroups",
        "billingconductor:ListBillingGroupCostReports",
        "billingconductor:GetBillingGroupCostReport",
        "billingconductor:ListAccountAssociations",
        "billingconductor:ListPricingPlans",
        "billingconductor:ListPricingRules",
        "billingconductor:ListCustomLineItems"
      ],
      "Resource": "*"
    }
  ]
}
```

If CUR Athena data is in the payer account (Scenario B), also add:

```json
{
  "Sid": "AthenaQueryExecution",
  "Effect": "Allow",
  "Action": [
    "athena:StartQueryExecution",
    "athena:GetQueryExecution",
    "athena:GetQueryResults",
    "athena:StopQueryExecution"
  ],
  "Resource": "arn:aws:athena:*:*:workgroup/*"
},
{
  "Sid": "S3AthenaResults",
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"],
  "Resource": [
    "arn:aws:s3:::YOUR_ATHENA_RESULTS_BUCKET",
    "arn:aws:s3:::YOUR_ATHENA_RESULTS_BUCKET/*"
  ]
},
{
  "Sid": "S3CURData",
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::YOUR_CUR_DATA_BUCKET",
    "arn:aws:s3:::YOUR_CUR_DATA_BUCKET/*"
  ]
},
{
  "Sid": "GlueDataCatalog",
  "Effect": "Allow",
  "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"],
  "Resource": "*"
}
```

---

## 4. Member Account Role (`CostAnalyzerAgentMemberRole`)

This role lives in each member account that has VPC Flow Log data in Athena. The AgentCore execution role assumes it to run Athena queries against VPC Flow Logs.

### Trust policy

Allow the AgentCore execution role to assume this role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::AGENT_ACCOUNT_ID:role/AmazonBedrockAgentCoreSDKRuntime-REGION-HASH"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "YOUR_EXTERNAL_ID"
        }
      }
    }
  ]
}
```

Remove the `Condition` block if you are not using external IDs.

### Permissions policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AthenaQueryExecution",
      "Effect": "Allow",
      "Action": [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:StopQueryExecution"
      ],
      "Resource": "arn:aws:athena:*:*:workgroup/*"
    },
    {
      "Sid": "S3AthenaResults",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::YOUR_ATHENA_RESULTS_BUCKET",
        "arn:aws:s3:::YOUR_ATHENA_RESULTS_BUCKET/*"
      ]
    },
    {
      "Sid": "S3VPCFlowLogData",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::YOUR_VPC_FLOWLOG_DATA_BUCKET",
        "arn:aws:s3:::YOUR_VPC_FLOWLOG_DATA_BUCKET/*"
      ]
    },
    {
      "Sid": "GlueDataCatalog",
      "Effect": "Allow",
      "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"],
      "Resource": "*"
    }
  ]
}
```

---

## 5. Optional: Read-only permissions for the alternatives evidence tools

Only needed when `alternatives.enabled: true` (see [Configuration Guide](configuration.md#7-alternative-service-evidence-tools-alternatives)). Inventory and CloudWatch collection runs inside each configured account through that account's role, so add **Statement A** below to **every role whose account should be inspected** (the payer role and any member roles). Cost Explorer is different: it is only ever called through the payer account, so **Statement B** goes on the **payer role only** and must not be added to member roles. Accounts whose role lacks an action are not skipped silently: the tool reports the missing action under `missing_permissions`.

Together, the two statements below are exactly the output of `required_iam_actions()` in `agent/services/readonly_guard.py`: Statement A is every service except `ce`, and Statement B is the `ce` entry. That module is the source of truth: it is also the allowlist the code enforces, so the tools cannot call any other operation even if a role grants more. To regenerate the list after a code change:

```bash
python -c "import json; from agent.services.readonly_guard import required_iam_actions as r; print(json.dumps(r(), indent=2))"
```

**Statement A: every inspected role (payer and member)**

```json
{
  "Sid": "AlternativesEvidenceReadOnly",
  "Effect": "Allow",
  "Action": [
    "ec2:DescribeInstanceTypes",
    "ec2:DescribeInstances",
    "ec2:DescribeNatGateways",
    "ec2:DescribeVpcEndpoints",
    "lambda:ListEventSourceMappings",
    "lambda:ListFunctions",
    "ecs:DescribeClusters",
    "ecs:DescribeContainerInstances",
    "ecs:ListClusters",
    "ecs:ListContainerInstances",
    "rds:DescribeDBClusters",
    "rds:DescribeDBInstances",
    "cloudwatch:GetMetricData"
  ],
  "Resource": "*"
}
```

**Statement B: payer role only (do not add to member roles)**

```json
{
  "Sid": "AlternativesCostExplorerReadOnly",
  "Effect": "Allow",
  "Action": [
    "ce:GetCostAndUsage"
  ],
  "Resource": "*"
}
```

The payer permissions policy in section 3 (`CostExplorer` statement) already includes `ce:GetCostAndUsage`. If the payer role uses that policy, Statement B is already satisfied and does not need to be added again.

Notes:

- All actions are read-only `Describe*`, `List*` or `Get*` calls. No write permission is required or used.
- `ce:GetCostAndUsage` is used only by `identify_high_cost_services`, and the code calls it only through the payer account. Member roles never need it, so keep it out of their policies.
- Statement A covers inventory (EC2, Lambda, ECS, RDS) and CloudWatch metrics, which run in each inspected account. Statement B covers the cost screening step, which runs in the payer account.
- This document is guidance only; nothing here changes any IAM policy automatically.

---

## 6. Proposed (NOT ACTIVE): AWS Pricing API permissions for alternative pricing comparison

> **Status: documentation only. Pending approval.** Nothing in this section is granted, required or enforced today. The alternatives code does not call the Pricing API yet: `agent/services/readonly_guard.py` has no `pricing` entry, so `ReadOnlySessionManager` would refuse any Pricing call. No IAM role or policy is changed by this document, and no permission should be added to AWS until the approval below is given.

The pricing comparison step (existing service versus proposed alternative, same workload) needs to read public list prices from the AWS Price List Query API. Required actions, all read-only:

| Action | Used for |
|--------|----------|
| `pricing:GetProducts` | Retrieve price list entries for a service code (for example `AmazonEC2`, `AWSLambda`, `AmazonECS`, `AmazonRDS`) filtered by attributes such as region and instance type |
| `pricing:GetAttributeValues` | Discover valid filter values (for example the location and usage type strings) so filters are not guessed |
| `pricing:DescribeServices` | Confirm a service code and list its filterable attributes |

```json
{
  "Sid": "AlternativesPricingReadOnly",
  "Effect": "Allow",
  "Action": [
    "pricing:GetProducts",
    "pricing:GetAttributeValues",
    "pricing:DescribeServices"
  ],
  "Resource": "*"
}
```

Notes:

- **Which role:** the payer role only (the same role that already serves Cost Explorer for this feature). Pricing data is public list pricing and is not account specific, so member roles do not need it.
- **Already covered if you use section 3:** the payer permissions policy in section 3 already contains a `Pricing` statement with exactly these three actions. If the payer role uses that policy, no IAM change is needed; the only change would be in code (adding `pricing` to the read-only allowlist), which is not done yet.
- The Price List Query API is served from a small set of Regions only, independent of the Region being priced. The client must be created in a supported Region (for example `us-east-1`); this is a code detail for the implementation step, not an IAM setting.
- `Resource` must be `*`; these actions do not support resource-level restrictions.
- List prices are On-Demand public prices. They do not include your Savings Plans, Reserved Instances, EDP or other private discounts, so any estimate built from them must say so.
- Not requested: no write action, and no `bcm-pricing-calculator` or `billingconductor` actions.

**Approval required before:** (1) adding this statement to any role, and (2) adding `pricing` to `readonly_guard.py` `ALLOWED_OPERATIONS` / `IAM_ACTIONS`. When approved, regenerate the permission report with the command in section 5 so this document and the code allowlist stay identical.

---

## Quick Reference: Which role needs what

| Permission | Client | Execution Role | Payer Role | Member Role |
|------------|--------|----------------|------------|-------------|
| Deploy agent | ✅ | | | |
| Invoke agent | ✅ | | | |
| Assume cross-account roles | | ✅ | | |
| Bedrock model invocation | | ✅ | | |
| Cost Explorer / Budgets / Pricing | | | ✅ | |
| Cost Optimization Hub | | | ✅ | |
| Compute Optimizer | | | ✅ | |
| Billing Conductor | | | ✅ | |
| Athena (CUR) | | ✅* | ✅* | |
| Athena (VPC Flow Logs) | | ✅* | | ✅ |
| S3 (Athena results + data) | | ✅* | ✅* | ✅ |
| Glue Data Catalog | | ✅* | ✅* | ✅ |
| Alternatives evidence tools: inventory and CloudWatch (Statement A, section 5) | | | ✅† | ✅† |
| Alternatives evidence tools: Cost Explorer `ce:GetCostAndUsage` (Statement B, section 5) | | | ✅† | |

*Only needed if Athena data is in the same account as the agent (Scenario A). See [Configuration Guide](configuration.md) for scenario details.

†Only needed when `alternatives.enabled` is true. Statement A applies in every account that should be inspected; Statement B applies in the payer account only.

## Security Best Practices

- Follow least privilege — scope resource ARNs to specific buckets, workgroups, and accounts
- Use external IDs on cross-account trust policies to prevent confused deputy attacks
- Never commit `config.yaml` with real account IDs or role ARNs (use `config.yaml.example` as template)
- Enable CloudTrail logging in all accounts to audit cross-account access
- Rotate credentials and review trust policies regularly
- Consider VPC endpoints for Bedrock, Athena, and S3 to keep traffic off the public internet
