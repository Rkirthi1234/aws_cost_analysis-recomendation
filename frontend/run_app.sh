#!/bin/bash
# Launcher for Cost Analyzer Agent Streamlit App

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Parse command line arguments
DEBUG_MODE=false
if [ "$1" == "--debug" ] || [ "$1" == "-d" ]; then
    DEBUG_MODE=true
    export DEBUG_MODE=true
    echo -e "${YELLOW}🔍 Debug mode enabled${NC}\n"
fi

echo -e "${BLUE}🚀 Starting Cost Analyzer Agent Web App...${NC}\n"

# Get the project root directory (parent of frontend)
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo -e "${GREEN}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install/update frontend dependencies
echo -e "${GREEN}Checking dependencies...${NC}"
pip install -q -r frontend/requirements.txt

# Run Streamlit app from frontend directory
echo -e "${GREEN}Launching web interface...${NC}"
if [ "$DEBUG_MODE" == "true" ]; then
    echo -e "${YELLOW}Debug logs will appear below...${NC}\n"
fi

streamlit run frontend/app.py

# Deactivate on exit
deactivate
