"""
Cost Analyzer Agent for Bedrock AgentCore
Deployed via AgentCore with Strands Agents SDK

Features:
- Prompt caching for 90% cost reduction and 85% latency improvement
- Tool caching for reusable tool definitions
- Concurrent tool execution for parallel operations
"""
import logging
import re
import time
import hashlib
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.executors.concurrent import ConcurrentToolExecutor
from strands.types.content import SystemContentBlock
from agent.services.config_service import ConfigService
from agent.services.athena_service import AthenaService
from agent.services.discovery_service import DiscoveryService
from agent.services.mcp_service import MCPService
from agent.tools.date_tools import DateTools
from agent.tools.athena_tools import AthenaTools
from agent.tools.analysis_tools import AnalysisTools
from agent.tools.billing_tools import BillingTools
from agent.prompts.system_prompt import get_system_prompt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("CostAnalyzerAgent")

# Initialize AgentCore app
app = BedrockAgentCoreApp()

# Global agent instance (initialized once)
_agent = None


def _initialize_agent():
    """Initialize the agent with all services and tools."""
    global _agent
    
    if _agent is not None:
        logger.info("Agent already initialized, reusing existing instance")
        return _agent
    
    start_time = time.time()
    logger.info("Initializing Cost Analyzer Agent...")
    
    # Load configuration
    config = ConfigService("agent/config.yaml")
    logger.info(f"Configuration loaded: model={config.model_id}, region={config.aws_region}")
    logger.info(f"Config integrity hash: {config.config_hash[:16]}...")
    
    # Initialize cross-account access from account registry
    account_registry = config.account_registry

    from agent.services.session_manager import SessionManager
    from agent.services.multi_account_executor import MultiAccountQueryExecutor

    session_manager = SessionManager(account_registry)
    logger.info(f"SessionManager initialized with {len(account_registry.entries)} accounts")

    # CUR Athena — from account with athena.cur configured
    cur_account = account_registry.get_cur_account()
    cur_athena_service = None
    if cur_account:
        cur_session = session_manager.get_session(cur_account.account_id)
        cur_athena_service = AthenaService(
            region=cur_account.region or config.aws_region,
            database=cur_account.athena_cur.database,
            table=cur_account.athena_cur.table,
            session=cur_session,
        )
        logger.info(f"CUR AthenaService created for account {cur_account.account_id}")
    else:
        logger.warning("No account with athena.cur configured — CUR Athena queries will be unavailable")

    # VPC Flow Logs — from accounts with athena.vpc_flowlogs configured
    vpc_athena_services = {}
    for acct in account_registry.get_vpc_flowlogs_accounts():
        sess = session_manager.get_session(acct.account_id)
        vpc_athena_services[acct.account_id] = AthenaService(
            region=acct.region or config.aws_region,
            database=acct.athena_vpc_flowlogs.database,
            table=acct.athena_vpc_flowlogs.table,
            session=sess,
        )
        logger.info(f"VPC AthenaService created for account {acct.account_id}")

    multi_account_executor = MultiAccountQueryExecutor(session_manager, account_registry)
    logger.info("MultiAccountQueryExecutor initialized")

    # Initialize MCP service (if enabled)
    mcp_clients = []
    if config.mcp_enabled:
        logger.info("Initializing MCP services...")
        mcp_start = time.time()
        mcp_service = MCPService(config.mcp_servers)
        
        # Use synchronous initialization (more reliable for MCP clients)
        mcp_service.initialize()
        
        mcp_clients = mcp_service.get_all_clients()
        mcp_time = time.time() - mcp_start
        logger.info(f"MCP services initialized: {len(mcp_clients)} clients in {mcp_time:.2f}s")
    
    # Collect tools
    tools = []
    
    # Date tools
    date_tools = DateTools()
    tools.extend(date_tools.get_tools())
    logger.info(f"Date tools added: {len(date_tools.get_tools())} tools")
    
    # Athena tools — conditionally include CUR and VPC tools based on config
    # Create DiscoveryService for auto-discovery when table is not configured
    cur_discovery_service = None
    if cur_account:
        cur_session = session_manager.get_session(cur_account.account_id)
        cur_discovery_service = DiscoveryService(
            session=cur_session,
            region=cur_account.region or config.aws_region,
        )

    athena_tools = AthenaTools(
        athena_service=cur_athena_service,  # may be None
        vpc_flowlog_config={'enabled': bool(vpc_athena_services)},
        member_athena_services=vpc_athena_services,
        multi_account_executor=multi_account_executor if vpc_athena_services else None,
        discovery_service=cur_discovery_service,
    )
    tools.extend(athena_tools.get_tools())
    logger.info(f"Athena tools added: {len(athena_tools.get_tools())} tools")
    
    # Analysis tools
    analysis_tools = AnalysisTools()
    tools.extend(analysis_tools.get_tools())
    logger.info(f"Analysis tools added: {len(analysis_tools.get_tools())} tools")
    
    # Billing tools
    billing_tools = BillingTools(session_manager, account_registry)
    tools.extend(billing_tools.get_tools())
    logger.info(f"Billing tools added: {len(billing_tools.get_tools())} tools")
    
    # Add MCP clients as tools
    tools.extend(mcp_clients)
    logger.info(f"Total tools available: {len(tools)}")
    
    # Create model
    model = BedrockModel(
        model_id=config.model_id,
        temperature=config.model_temperature,
        max_tokens=config.model_max_tokens,
        cache_tools="default" if config.model_cache_tools else None  # Enable tool caching
    )
    logger.info(f"Model created: {config.model_id}")
    logger.info(f"Tool caching: {'enabled' if config.model_cache_tools else 'disabled'}")
    
    # Get system prompt text
    system_prompt_text = get_system_prompt()
    logger.info(f"System prompt loaded: {len(system_prompt_text)} characters")
    
    # Wrap system prompt with cache point for prompt caching
    # This reduces costs by 90% and latency by 85% for repeated queries
    # Cache TTL is configurable: "5m" (5 minutes) or "1h" (1 hour)
    cache_ttl = config.model_cache_ttl
    system_content = [
        SystemContentBlock(text=system_prompt_text),
        SystemContentBlock(cachePoint={"type": "default", "ttl": cache_ttl})
    ]
    logger.info(f"System prompt caching enabled with {cache_ttl} TTL")
    
    # Create agent with concurrent tool executor for parallel tool execution
    _agent = Agent(
        model=model,
        tools=tools,
        system_prompt=system_content,  # Use cached system prompt
        tool_executor=ConcurrentToolExecutor()  # Enable concurrent tool execution
    )
    
    init_time = time.time() - start_time
    logger.info(f"✅ Agent initialization complete in {init_time:.2f}s")
    return _agent


# --- T3 Mitigation: Prompt injection protection ---
# Maximum prompt length to prevent resource exhaustion
MAX_PROMPT_LENGTH = 10000

# Patterns that indicate prompt injection attempts
_INJECTION_PATTERNS = [
    r'(?i)ignore\s+(all\s+)?previous\s+instructions',
    r'(?i)disregard\s+(all\s+)?prior\s+(instructions|context)',
    r'(?i)you\s+are\s+now\s+a\s+',
    r'(?i)system\s*:\s*',
    r'(?i)<\s*system\s*>',
    r'(?i)\[INST\]',
    r'(?i)\\n\\nHuman:',
    r'(?i)\\n\\nAssistant:',
]


def _sanitize_prompt(prompt: str) -> str:
    """Sanitize user prompt to mitigate prompt injection attacks.

    - Truncates excessively long prompts
    - Logs warnings for detected injection patterns (does not block)
    - Strips control characters

    This is a defense-in-depth measure. The primary protection is the
    system prompt and model's instruction-following behavior.
    """
    # Truncate excessively long prompts
    if len(prompt) > MAX_PROMPT_LENGTH:
        logger.warning(f"Prompt truncated from {len(prompt)} to {MAX_PROMPT_LENGTH} chars")
        prompt = prompt[:MAX_PROMPT_LENGTH]

    # Log warnings for injection patterns (don't block — could be false positives)
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, prompt):
            logger.warning(f"Potential prompt injection detected: pattern={pattern}")
            break

    # Strip control characters (keep newlines and tabs)
    prompt = ''.join(
        c for c in prompt
        if c in ('\n', '\t', '\r') or (ord(c) >= 32)
    )

    return prompt


@app.entrypoint
async def invoke(payload, context):
    """
    AgentCore entrypoint function with streaming support.

    Uses agent.stream_async() to yield text chunks as they are generated,
    enabling real-time streaming to all clients (React frontend, Streamlit, CLI).
    Emits [BLOCK_END] markers between content blocks so clients can optionally
    separate intermediate narration from the final response.

    Security mitigations:
    - T3: Input sanitization against prompt injection
    - T11: Streaming marker injection prevention

    Args:
        payload: Request payload with 'prompt' field
        context: AgentCore context object (RequestContext)

    Yields:
        Text chunks as the agent generates them
    """
    try:
        start_time = time.time()
        session_id = getattr(context, "sessionId", "unknown")

        logger.info(f"Received invocation request for session {session_id}")
        logger.info(f"Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'not a dict'}")

        # Handle both dict and string payloads
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)

        # Extract user message from payload
        # Support AWS SDK format {"input": {"prompt": "..."}} and direct {"prompt": "..."}
        user_message = None
        if isinstance(payload, dict):
            if "input" in payload and isinstance(payload["input"], dict):
                user_message = payload["input"].get("prompt")
            else:
                user_message = payload.get("prompt") or payload.get("user_input")

        if not user_message:
            error_msg = "No 'prompt' or 'user_input' found in payload"
            logger.error(f"{error_msg}. Payload keys: {list(payload.keys())}")
            yield f"❌ Error: {error_msg}"
            return

        # T3 Mitigation: Sanitize user input against prompt injection
        user_message = _sanitize_prompt(user_message)

        # T11 Mitigation: Strip streaming control markers from user input
        # to prevent injection of [BLOCK_END] markers that could manipulate
        # the frontend's thinking/response separation logic
        user_message = user_message.replace("[BLOCK_END]", "")

        logger.info(f"User message: {user_message[:100]}..." if len(user_message) > 100 else f"User message: {user_message}")

        # Initialize agent (only happens once)
        agent = _initialize_agent()

        # Stream the agent response — yields text chunks as they are generated
        # Emits [BLOCK_END] markers between content blocks so the frontend
        # can separate intermediate narration from the final response.
        logger.info("Streaming agent response...")
        stream = agent.stream_async(user_message)
        in_tool_use = False
        has_text_in_block = False

        async for event in stream:
            evt = event.get('event', {})

            # Track tool use blocks — don't emit their content
            if evt.get('contentBlockStart', {}).get('start', {}).get('toolUse'):
                in_tool_use = True
                continue

            if evt.get('contentBlockStop') is not None:
                if has_text_in_block and not in_tool_use:
                    yield "[BLOCK_END]"
                    has_text_in_block = False
                in_tool_use = False
                continue

            text = (evt.get('contentBlockDelta', {})
                       .get('delta', {})
                       .get('text'))
            if text and not in_tool_use:
                has_text_in_block = True
                yield text

        total_time = time.time() - start_time
        logger.info(f"✅ Total invocation time: {total_time:.2f}s")

    except Exception as e:
        logger.error(f"Error in invoke function: {str(e)}", exc_info=True)
        yield f"❌ Error: {str(e)}"


# For AgentCore runtime
if __name__ == "__main__":
    app.run()
