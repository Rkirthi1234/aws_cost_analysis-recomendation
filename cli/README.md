# Cost Analyzer Agent - CLI Interface

Command-line interface for interacting with your deployed Cost Analyzer Agent.

## Quick Start

```bash
# Interactive mode (recommended)
./cli/cli.sh

# Single query
./cli/cli.sh -q "What are my top 5 services by cost last month?"
```

## Features

- **Interactive Mode**: Chat with your agent in a conversational interface
- **Single Query Mode**: Perfect for scripting and automation
- **Prompt Library**: Access pre-built queries with `/prompts` command
- **Animated Thinking**: Visual feedback while agent processes your query
- **Auto-Configuration**: Reads agent ID from `shared/client.yaml`

## Usage

### Interactive Mode

Start a conversation with your agent:

```bash
./cli/cli.sh
```

or

```bash
./cli/cli.sh -i
```

**Interactive Commands:**
- `/prompts` - View and select from prompt library
- `exit`, `quit`, `q` - Exit the session

### Single Query Mode

Execute a single query and exit:

```bash
./cli/cli.sh -q "Show me EC2 costs for last week"
```

### Options

```bash
./cli/cli.sh [OPTIONS]

Options:
  -h, --help              Show help message
  -i, --interactive       Start interactive mode
  -q, --query "text"      Send single query
  -a, --agent-id ID       Specify agent ID (overrides config)
  -r, --region REGION     AWS region (default: us-east-1)
  -v, --verbose           Enable verbose logging
```

## Configuration

The CLI reads the agent ID from `shared/client.yaml`, which is automatically created by `./deploy.sh`:

```yaml
agentcore:
  agent_id: cost_analyzer_agent-5gTQmv5pqK  # Auto-populated by deploy.sh
```

This configuration is shared with the frontend, so both interfaces use the same agent.

If you need to update it manually:

```bash
nano shared/client.yaml
# Update agent_id
```

You can also specify the agent ID directly:

```bash
./cli/cli.sh -a cost_analyzer_agent-5gTQmv5pqK -q "What are my costs?"
```

## Requirements

- Python 3.10+
- AWS credentials configured (`aws configure`)
- Deployed AgentCore agent
- IAM permissions for `bedrock-agentcore:InvokeAgentRuntime`

## Installation

The CLI dependencies are installed automatically when you run `deploy.sh`. If you need to install them manually:

```bash
# From project root
pip install boto3 pyyaml
```

## Examples

### Cost Analysis

```bash
./cli/cli.sh -q "What were my top 5 services by cost last month?"
./cli/cli.sh -q "Show me cost trends for the last 3 months"
./cli/cli.sh -q "Compare costs between last month and this month"
```

### Optimization

```bash
./cli/cli.sh -q "What are my top cost optimization opportunities?"
./cli/cli.sh -q "Show me idle resources"
./cli/cli.sh -q "What rightsizing recommendations do you have?"
```

### Service-Specific

```bash
./cli/cli.sh -q "Show me EC2 costs for the last week"
./cli/cli.sh -q "What are my S3 storage costs?"
./cli/cli.sh -q "Analyze Lambda function costs"
```

### Network Analysis (if VPC Flow Logs enabled)

```bash
./cli/cli.sh -q "Show me top 10 network talkers by data transfer"
./cli/cli.sh -q "What are the most active network connections?"
```

## Troubleshooting

### "Agent ID not found"

The `shared/client.yaml` should be auto-created by `./deploy.sh`. If it's missing:

```bash
# Check deployment status
agentcore status

# Manually create client config
cp shared/client.yaml.example shared/client.yaml
nano shared/client.yaml
# Add: agent_id: YOUR_AGENT_ID

# Or specify agent ID directly
./cli/cli.sh -a YOUR_AGENT_ID -q "test"
```

### "Access denied"

Verify your AWS credentials and IAM permissions:

```bash
# Check credentials
aws sts get-caller-identity

# Ensure you have bedrock-agentcore:InvokeAgentRuntime permission
```

### "No response"

Enable verbose mode to see detailed logs:

```bash
./cli/cli.sh -v -q "test query"
```

## Scripting

The CLI is perfect for automation:

```bash
#!/bin/bash
# Daily cost report script

REPORT=$(./cli/cli.sh -q "What were my costs yesterday?")
echo "$REPORT" | mail -s "Daily AWS Cost Report" team@example.com
```

## Advanced Usage

### Different Regions

```bash
./cli/cli.sh -r us-west-2 -q "What are my costs?"
```

### Multiple Queries

```bash
# Process multiple queries
for query in "EC2 costs" "S3 costs" "Lambda costs"; do
    echo "Query: $query"
    ./cli/cli.sh -q "$query"
    echo "---"
done
```

## Support

For issues or questions:
1. Check the main [README.md](../README.md)
2. Review CloudWatch logs: `agentcore logs`
3. Open an issue in the repository
