#!/bin/bash

# Cost Analyzer Agent - AgentCore CLI Interface
# Invokes the deployed Bedrock AgentCore agent

set -e

# Colors for better UX
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PATH="${PROJECT_ROOT}/venv"
AGENT_SCRIPT="${SCRIPT_DIR}/cli.py"

# Check if virtual environment exists
if [ ! -d "$VENV_PATH" ]; then
    echo -e "${RED}Error: Virtual environment not found at $VENV_PATH${NC}"
    echo "Please run: python3 -m venv venv && source venv/bin/activate && pip install boto3 pyyaml"
    exit 1
fi

# Activate virtual environment
source "${VENV_PATH}/bin/activate"

# Check if Python agent script exists
if [ ! -f "$AGENT_SCRIPT" ]; then
    echo -e "${RED}Error: Agent script not found at $AGENT_SCRIPT${NC}"
    exit 1
fi

# Function to display help
show_help() {
    cat << EOF
${GREEN}Cost Analyzer Agent - AgentCore CLI Interface${NC}

This CLI invokes your deployed Bedrock AgentCore agent via AWS SDK.

Usage:
  $0 [OPTIONS] [QUERY]

Options:
  -h, --help              Show this help message
  -i, --interactive       Start interactive chat mode (default if no query provided)
  -q, --query "text"      Send a single query and exit
  -a, --agent-id ID       AgentCore agent ID (default: from client.yaml or .bedrock_agentcore.yaml)
  -r, --region REGION     AWS region (default: us-east-1)
  -v, --verbose           Enable verbose logging

Examples:
  # Interactive mode (uses agent ID from client.yaml)
  $0 -i
  $0

  # Single query
  $0 -q "What were my top 5 services by cost last month?"

  # With specific agent ID
  $0 -a cost_analyzer_agent-5gTQmv5pqK -q "Show me EC2 costs"

  # With verbose logging
  $0 -v -q "Analyze my S3 costs"

  # Different region
  $0 -r us-west-2 -q "What are my costs?"

Interactive Commands:
  /prompts                View prompt library and select a prompt
  exit, quit, q           Exit the interactive session

Configuration:
  Add agent_id to shared/client.yaml:
    agentcore:
      agent_id: cost_analyzer_agent-5gTQmv5pqK

Requirements:
  - AWS credentials configured (aws configure)
  - Deployed AgentCore agent
  - IAM permissions for bedrock-agent-runtime:InvokeAgent

EOF
}

# Parse command line arguments
INTERACTIVE=false
QUERY=""
AGENT_ID=""
REGION="us-east-1"
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -i|--interactive)
            INTERACTIVE=true
            shift
            ;;
        -q|--query)
            QUERY="$2"
            shift 2
            ;;
        -a|--agent-id)
            AGENT_ID="$2"
            shift 2
            ;;
        -r|--region)
            REGION="$2"
            shift 2
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        *)
            # Treat remaining args as query
            QUERY="$*"
            break
            ;;
    esac
done

# If no query provided, default to interactive mode
if [ -z "$QUERY" ]; then
    INTERACTIVE=true
fi

# Build Python command
PYTHON_CMD="python3 ${AGENT_SCRIPT} --region ${REGION}"

if [ -n "$AGENT_ID" ]; then
    PYTHON_CMD="$PYTHON_CMD --agent-id $AGENT_ID"
fi

if [ "$VERBOSE" = true ]; then
    PYTHON_CMD="$PYTHON_CMD --verbose"
fi

# Execute based on mode
if [ "$INTERACTIVE" = true ]; then
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║   Cost Analyzer Agent - AgentCore Interactive CLI Mode            ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${BLUE}Connecting to your deployed AgentCore agent...${NC}"
    echo ""
    
    $PYTHON_CMD --interactive
else
    # Single query mode
    echo -e "${BLUE}Query:${NC} $QUERY"
    echo ""
    $PYTHON_CMD --query "$QUERY"
fi
