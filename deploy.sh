#!/bin/bash

# Cost Analyzer Agent Deployment Script
# Deploys the agent to Amazon Bedrock AgentCore

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
AGENT_NAME="cost_analyzer_agent"
ENTRYPOINT="agent/agentcore_agent.py"
REQUIREMENTS_FILE="requirements.txt"

echo -e "${BLUE}💰 Cost Analyzer Agent Deployment Script${NC}"
echo "=================================================="

# Function to print status messages
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check prerequisites
print_status "Checking prerequisites..."

if ! command_exists aws; then
    print_error "AWS CLI not found. Please install AWS CLI first."
    exit 1
fi

# Check AWS credentials
if ! aws sts get-caller-identity >/dev/null 2>&1; then
    print_error "AWS credentials not configured. Please run 'aws configure' first."
    exit 1
fi

print_success "Prerequisites check passed"

# Get AWS account ID and region
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region)

if [ -z "$REGION" ]; then
    REGION="us-east-1"
    print_warning "No default region configured, using us-east-1"
fi

print_status "AWS Account ID: $ACCOUNT_ID"
print_status "AWS Region: $REGION"

# Validate agent files exist
print_status "Validating agent files..."

required_files=(
    "$ENTRYPOINT"
    "$REQUIREMENTS_FILE"
    "agent/config.yaml"
    "agent/services/config_service.py"
    "agent/services/athena_service.py"
    "agent/services/mcp_service.py"
    "agent/tools/date_tools.py"
    "agent/tools/athena_tools.py"
    "agent/tools/analysis_tools.py"
    "agent/prompts/system_prompt.py"
    "agent/services/account_registry.py"
    "agent/services/session_manager.py"
    "agent/services/multi_account_executor.py"
)

for file in "${required_files[@]}"; do
    if [ ! -f "$file" ]; then
        print_error "Required file not found: $file"
        exit 1
    fi
done

print_success "All required agent files found"

# Create and activate virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    print_status "Creating virtual environment..."
    python3 -m venv venv
    print_success "Virtual environment created"
fi

print_status "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
print_status "Installing dependencies..."
if command_exists uv; then
    print_status "Using uv for faster dependency installation..."
    uv pip install -r requirements.txt
else
    print_status "Using pip for dependency installation..."
    pip install -r requirements.txt
fi

print_success "Dependencies installed successfully"

# Read execution role from agent/config.yaml if specified (after venv activation)
print_status "Reading configuration from agent/config.yaml..."

EXECUTION_ROLE_ARN=""
if command_exists python3; then
    EXECUTION_ROLE_ARN=$(python3 -c "
import yaml
try:
    with open('agent/config.yaml', 'r') as f:
        config = yaml.safe_load(f)
        role = config.get('agentcore', {}).get('execution_role_arn', '')
        print(role if role else '')
except:
    print('')
" 2>/dev/null || echo "")
fi

if [ -n "$EXECUTION_ROLE_ARN" ]; then
    print_status "Using execution role from config: $EXECUTION_ROLE_ARN"
else
    print_status "No execution role specified - AgentCore will auto-create one"
    print_status "Auto-created role name: AmazonBedrockAgentCoreSDKRuntime-${REGION}-{hash}"
fi

# Check for agentcore after installation
print_status "Checking for AgentCore CLI..."
if ! command_exists agentcore; then
    print_error "AgentCore CLI not found after installation."
    print_status "Installing bedrock-agentcore-starter-toolkit..."
    pip install bedrock-agentcore-starter-toolkit
fi

print_success "AgentCore CLI is available"

# Configure AgentCore
print_status "Configuring AgentCore agent..."

# Build configure command
CONFIGURE_CMD="agentcore configure \
    --entrypoint \"$ENTRYPOINT\" \
    --name \"$AGENT_NAME\" \
    --runtime PYTHON_3_12 \
    --requirements-file \"$REQUIREMENTS_FILE\" \
    --region \"$REGION\" \
    --disable-memory \
    --non-interactive"

# Add execution role if specified
if [ -n "$EXECUTION_ROLE_ARN" ]; then
    CONFIGURE_CMD="$CONFIGURE_CMD --execution-role \"$EXECUTION_ROLE_ARN\""
    print_status "Using custom execution role"
else
    print_status "AgentCore will auto-create execution role with required permissions:"
    print_status "  • ECR image access"
    print_status "  • CloudWatch logging"
    print_status "  • Bedrock model invocation"
    print_status "  • X-Ray tracing"
fi

# Execute configuration
eval $CONFIGURE_CMD

if [ $? -eq 0 ]; then
    print_success "AgentCore configuration completed"
else
    print_error "AgentCore configuration failed"
    exit 1
fi

# Deploy the agent
print_status "Deploying Cost Analyzer Agent..."

agentcore launch

if [ $? -eq 0 ]; then
    print_success "Agent deployment completed successfully!"
else
    print_error "Agent deployment failed"
    exit 1
fi

# Get agent information
print_status "Retrieving agent information..."

AGENT_INFO=$(agentcore status --verbose 2>/dev/null || echo "Could not retrieve agent info")
if [ "$AGENT_INFO" != "Could not retrieve agent info" ]; then
    # Extract agent ID from status output
    AGENT_ID=$(echo "$AGENT_INFO" | grep -o '"agent_id": "[^"]*"' | cut -d'"' -f4 | head -1)
    
    if [ -n "$AGENT_ID" ]; then
        print_success "Agent ID: $AGENT_ID"
        
        # Auto-create client config for CLI and Frontend
        print_status "Creating client configuration..."
        cat > shared/client.yaml << EOF
# Cost Analyzer Agent - Client Configuration
# Auto-generated by deploy.sh
# Used by both CLI and Frontend

# AWS Configuration
aws:
  region: $REGION

# AgentCore Configuration
agentcore:
  agent_id: $AGENT_ID
EOF
        print_success "Client configuration created: shared/client.yaml"
        print_status "Both CLI and Frontend will use this configuration"
    fi
fi

# Display deployment summary
echo ""
echo "=================================================="
print_success "🎉 Cost Analyzer Agent Deployment Complete!"
echo "=================================================="
echo ""
echo "📋 Deployment Summary:"
echo "   • Agent Name: $AGENT_NAME"
echo "   • Entrypoint: $ENTRYPOINT"
echo "   • Region: $REGION"
if [ -n "$AGENT_ID" ]; then
echo "   • Agent ID: $AGENT_ID"
fi
if [ -n "$EXECUTION_ROLE_ARN" ]; then
echo "   • Execution Role: $EXECUTION_ROLE_ARN"
else
echo "   • Execution Role: Auto-created by AgentCore"
fi
echo ""
echo "🧪 Testing Commands:"
echo ""
echo "   # Test with AgentCore CLI"
echo "   agentcore invoke '{\"prompt\": \"What is the current date?\"}'"
echo ""
echo "   # Test with custom CLI"
echo "   ./cli.sh -q \"What are my top 5 services by cost last month?\""
echo ""
echo "   # Interactive mode"
echo "   ./cli.sh"
echo ""
echo "🌐 Web Interface:"
echo "   cd frontend && streamlit run app.py"
echo ""
echo "🔧 Management Commands:"
echo "   • View agent: agentcore status"
echo "   • View logs: agentcore logs"
echo "   • Update agent: agentcore launch (after code changes)"
echo "   • Delete agent: agentcore destroy"
echo ""
print_success "Ready to optimize AWS costs! 💰"
