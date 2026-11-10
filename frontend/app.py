"""
Cost Analyzer Agent - Streamlit Frontend
Based on AWS AgentCore official Streamlit example
"""
import json
import logging
import os
import re
import sys
import time
import uuid
import yaml
from typing import Iterator
import boto3
import streamlit as st

# Configure logging for local debugging
# Set DEBUG_MODE=true in environment to enable detailed logging
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("StreamlitApp")

if DEBUG_MODE:
    logger.info("🔍 DEBUG MODE ENABLED - Detailed logging active")
else:
    logger.info("ℹ️  Normal mode - Set DEBUG_MODE=true for detailed logging")

# Page config
st.set_page_config(
    page_title="Cost Analyzer Agent",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Remove Streamlit branding
st.markdown(
    """
    <style>
        .stAppDeployButton {display:none;}
        #MainMenu {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)


def clean_response_text(text: str) -> str:
    """Clean and format response text"""
    if not text:
        return text
    
    if DEBUG_MODE:
        logger.debug(f"Cleaning text (length: {len(text)})")
        logger.debug(f"First 200 chars: {text[:200]}")
    
    # Handle consecutive quoted chunks
    text = re.sub(r'"\s*"', "", text)
    text = re.sub(r'^"', "", text)
    text = re.sub(r'"$', "", text)
    
    # Replace literal \n with actual newlines
    text = text.replace("\\n", "\n")
    text = text.replace("\\t", "\t")
    
    # Clean up multiple spaces
    text = re.sub(r" {3,}", " ", text)
    
    # Clean up multiple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    
    cleaned = text.strip()
    
    if DEBUG_MODE:
        logger.debug(f"Cleaned text (length: {len(cleaned)})")
    
    return cleaned


def extract_text_from_response(data) -> str:
    """Extract text content from response data"""
    if DEBUG_MODE:
        logger.debug(f"Extracting text from response type: {type(data)}")
        if isinstance(data, dict):
            logger.debug(f"Response keys: {list(data.keys())}")
    
    # Handle string representation of AgentResult object
    if isinstance(data, str):
        if DEBUG_MODE:
            logger.debug("Data is string, checking if it's AgentResult representation")
        
        # Check if it's a string representation of AgentResult
        if "AgentResult(" in data:
            if DEBUG_MODE:
                logger.debug("Detected AgentResult string representation, extracting text")
            
            # Extract text content using regex - look for 'text': "..." pattern
            import re
            # Try multiple patterns to extract the text
            patterns = [
                r"'text':\s*\"([^\"]+)\"",  # 'text': "content"
                r"'text':\s*'([^']+)'",      # 'text': 'content'
                r'"text":\s*"([^"]+)"',      # "text": "content"
                r"'text':\s*['\"](.+?)['\"](?=\})",  # More flexible pattern
            ]
            
            for pattern in patterns:
                text_match = re.search(pattern, data, re.DOTALL)
                if text_match:
                    text = text_match.group(1)
                    # Unescape the text
                    text = text.replace('\\n', '\n').replace('\\t', '\t')
                    text = text.replace("\\'", "'").replace('\\"', '"')
                    if DEBUG_MODE:
                        logger.debug(f"Extracted text using pattern {pattern} (length: {len(text)})")
                    return text
            
            # If no pattern matched, try to find content between 'text': and the next }
            # This is a fallback for complex nested structures
            text_start = data.find("'text':")
            if text_start == -1:
                text_start = data.find('"text":')
            
            if text_start != -1:
                # Find the opening quote
                quote_start = data.find('"', text_start)
                if quote_start == -1:
                    quote_start = data.find("'", text_start)
                
                if quote_start != -1:
                    # Find the closing quote (accounting for escaped quotes)
                    quote_char = data[quote_start]
                    text_content_start = quote_start + 1
                    
                    # Simple extraction - get everything until we find }] pattern
                    # This is a heuristic that works for the AgentResult format
                    end_marker = data.find("'}]", text_content_start)
                    if end_marker == -1:
                        end_marker = data.find('"}]', text_content_start)
                    
                    if end_marker != -1:
                        text = data[text_content_start:end_marker]
                        # Clean up the text
                        text = text.replace('\\n', '\n').replace('\\t', '\t')
                        text = text.replace("\\'", "'").replace('\\"', '"')
                        if DEBUG_MODE:
                            logger.debug(f"Extracted text using fallback method (length: {len(text)})")
                        return text
        
        # If it's just a regular string, return it
        return data
    
    if isinstance(data, dict):
        # Handle nested result wrapper
        if "result" in data:
            if DEBUG_MODE:
                logger.debug("Found 'result' key in response")
            result_data = data["result"]
            
            # Check if result is a string representation of AgentResult
            if isinstance(result_data, str) and "AgentResult(" in result_data:
                if DEBUG_MODE:
                    logger.debug("Result is AgentResult string, extracting text")
                return extract_text_from_response(result_data)
            
            # If result is a dict, recurse
            if isinstance(result_data, dict):
                return extract_text_from_response(result_data)
            
            # If result is a string, return it
            return str(result_data)
        
        # Handle message.content structure
        if "role" in data and "content" in data:
            if DEBUG_MODE:
                logger.debug("Found message structure with role and content")
            content = data["content"]
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    text = str(content[0]["text"])
                    if DEBUG_MODE:
                        logger.debug(f"Extracted text from content[0].text (length: {len(text)})")
                    return text
                return str(content[0])
            elif isinstance(content, str):
                return content
            return str(content)
        
        # Handle message wrapper
        if "message" in data:
            if DEBUG_MODE:
                logger.debug("Found 'message' wrapper")
            message_data = data["message"]
            if isinstance(message_data, dict):
                return extract_text_from_response(message_data)
            return str(message_data)
        
        # Direct text field
        if "text" in data:
            if DEBUG_MODE:
                logger.debug("Found direct 'text' field")
            return str(data["text"])
        elif "content" in data:
            if DEBUG_MODE:
                logger.debug("Found direct 'content' field")
            return str(data["content"])
    
    if DEBUG_MODE:
        logger.debug(f"Returning stringified data: {str(data)[:100]}")
    return str(data)


def parse_streaming_chunk(chunk: str) -> str:
    """Parse individual streaming chunk"""
    if DEBUG_MODE:
        logger.debug(f"Parsing chunk (length: {len(chunk)}): {chunk[:100]}")
    
    try:
        if chunk.strip().startswith("{"):
            data = json.loads(chunk)
            
            if DEBUG_MODE:
                logger.debug(f"Parsed JSON chunk with keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
            
            if isinstance(data, dict) and "role" in data and "content" in data:
                content = data["content"]
                if isinstance(content, list) and len(content) > 0:
                    first_item = content[0]
                    if isinstance(first_item, dict) and "text" in first_item:
                        return first_item["text"]
                    return str(first_item)
                return str(content)
            
            return extract_text_from_response(data)
        
        return chunk
    except json.JSONDecodeError as e:
        if DEBUG_MODE:
            logger.debug(f"JSON decode error: {e}")
        
        # Try to handle Python dict string
        if chunk.strip().startswith("{") and "'" in chunk:
            try:
                json_chunk = chunk.replace("'", '"')
                data = json.loads(json_chunk)
                return extract_text_from_response(data)
            except json.JSONDecodeError:
                pass
        
        return chunk


def invoke_agent_streaming(
    prompt: str,
    agent_arn: str,
    runtime_session_id: str,
    region: str = "us-east-1",
) -> Iterator[str]:
    """Invoke agent and yield streaming response chunks"""
    try:
        logger.info(f"Invoking agent with prompt: {prompt[:100]}...")
        logger.info(f"Agent ARN: {agent_arn}")
        logger.info(f"Session ID: {runtime_session_id}")
        logger.info(f"Region: {region}")
        
        # Create boto3 client with custom retry configuration
        # Disable automatic retries to prevent concurrent invocations during long-running operations
        from botocore.config import Config
        config = Config(
            retries={
                'max_attempts': 1,  # No retries - prevents concurrent invocations
                'mode': 'standard'
            },
            read_timeout=300,  # 5 minutes - enough for long agent operations
            connect_timeout=60  # 1 minute
        )
        
        client = boto3.client("bedrock-agentcore", region_name=region, config=config)
        
        payload = {"prompt": prompt}
        if DEBUG_MODE:
            logger.debug(f"Request payload: {json.dumps(payload)}")
        
        response = client.invoke_agent_runtime(
            agentRuntimeArn=agent_arn,
            qualifier="DEFAULT",
            runtimeSessionId=runtime_session_id,
            payload=json.dumps(payload),
        )
        
        if DEBUG_MODE:
            logger.debug(f"Response keys: {list(response.keys())}")
            logger.debug(f"Content type: {response.get('contentType')}")
        
        if "text/event-stream" in response.get("contentType", ""):
            logger.info("Handling streaming response")
            chunk_count = 0
            
            # Handle streaming response
            for line in response["response"].iter_lines(chunk_size=1):
                if line:
                    chunk_count += 1
                    line = line.decode("utf-8")
                    
                    if DEBUG_MODE and chunk_count <= 5:
                        logger.debug(f"Stream chunk #{chunk_count}: {line[:200]}")
                    
                    if line.startswith("data: "):
                        line = line[6:]
                        parsed_chunk = parse_streaming_chunk(line)
                        # Filter out block markers used by React frontend
                        parsed_chunk = parsed_chunk.replace("[BLOCK_END]", "")
                        if parsed_chunk.strip():
                            yield parsed_chunk
            
            logger.info(f"Streaming complete: {chunk_count} chunks received")
        else:
            logger.info("Handling non-streaming JSON response")
            
            # Handle non-streaming JSON response
            response_obj = response.get("response")
            
            if DEBUG_MODE:
                logger.debug(f"Response object type: {type(response_obj)}")
                logger.debug(f"Has read method: {hasattr(response_obj, 'read')}")
            
            if hasattr(response_obj, "read"):
                content = response_obj.read()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                
                if DEBUG_MODE:
                    logger.debug(f"Raw content (first 500 chars): {content[:500]}")
                
                try:
                    response_data = json.loads(content)
                    
                    if DEBUG_MODE:
                        logger.debug(f"Parsed JSON response type: {type(response_data)}")
                        if isinstance(response_data, dict):
                            logger.debug(f"Response keys: {list(response_data.keys())}")
                    
                    # Extract text using the improved extraction function
                    text = extract_text_from_response(response_data)
                    
                    if DEBUG_MODE:
                        logger.debug(f"Extracted text (length: {len(text)})")
                        logger.debug(f"First 200 chars: {text[:200]}")
                    
                    yield text
                
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error: {e}")
                    if DEBUG_MODE:
                        logger.debug(f"Failed content: {content[:1000]}")
                    
                    # If JSON parsing fails, try to extract text with regex
                    import re
                    # Look for text content in the string
                    text_match = re.search(r"'text':\s*['\"](.+?)['\"]", content, re.DOTALL)
                    if text_match:
                        yield text_match.group(1).replace('\\n', '\n')
                    else:
                        yield content
            else:
                logger.warning("No response content available")
                yield "No response content"
    
    except Exception as e:
        error_msg = f"❌ Error: {str(e)}"
        logger.error(f"Error invoking agent: {str(e)}", exc_info=True)
        yield error_msg


def load_agent_config():
    """Load agent configuration from shared/client.yaml"""
    try:
        import yaml
        import os
        
        # Load from shared directory
        config_path = os.path.join(os.path.dirname(__file__), '..', 'shared', 'client.yaml')
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            
            # Get agent ID and construct ARN
            agent_id = config.get('agentcore', {}).get('agent_id')
            region = config.get('aws', {}).get('region', 'us-east-1')
            
            if agent_id:
                # Get account ID from STS
                import boto3
                try:
                    sts = boto3.client('sts')
                    account_id = sts.get_caller_identity()['Account']
                    agent_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{agent_id}"
                    return agent_arn, region
                except Exception as e:
                    logger.warning(f"Could not get account ID: {e}")
                    return None, region
            
            return None, region
    except Exception as e:
        logger.warning(f"Could not load client.yaml: {e}")
        return None, 'us-east-1'


def initialize_session_state():
    """Initialize session state variables"""
    if "messages" not in st.session_state:
        st.session_state.messages = []
        logger.info("Initialized empty message history")
    
    if "runtime_session_id" not in st.session_state:
        # Generate a NEW session ID each time the app starts
        # This prevents the agent from continuing old conversations
        st.session_state.runtime_session_id = str(uuid.uuid4())
        # Mark this as a fresh session
        st.session_state.is_fresh_start = True
        logger.info(f"Generated new session ID: {st.session_state.runtime_session_id}")
    
    if "agent_arn" not in st.session_state:
        # Try to load from config.yaml
        default_arn, default_region = load_agent_config()
        
        if default_arn:
            st.session_state.agent_arn = default_arn
            logger.info(f"Loaded agent ARN from client.yaml: {st.session_state.agent_arn}")
        else:
            # Fallback to empty (user must configure)
            st.session_state.agent_arn = ""
            logger.info("No agent ARN in client.yaml, user must configure manually")
    
    # Initialize pending_query if not exists
    if "pending_query" not in st.session_state:
        st.session_state.pending_query = None


def render_sidebar():
    """Render sidebar with configuration"""
    with st.sidebar:
        st.header("⚙️ Settings")
        
        # Load default region from config if not in session state
        if "default_region" not in st.session_state:
            _, default_region = load_agent_config()
            st.session_state.default_region = default_region
        
        # Region selection
        regions = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]
        default_index = 0
        if st.session_state.default_region in regions:
            default_index = regions.index(st.session_state.default_region)
        
        region = st.selectbox(
            "AWS Region",
            regions,
            index=default_index,
        )
        
        # Agent ARN input
        st.subheader("Agent Configuration")
        
        # Show info if loaded from config
        if st.session_state.agent_arn and "client.yaml" not in st.session_state.get("arn_source", ""):
            st.session_state.arn_source = "client.yaml"
            st.info("✅ Agent ARN loaded from client.yaml")
        
        agent_arn = st.text_input(
            "Agent ARN",
            value=st.session_state.agent_arn,
            help="Your AgentCore agent ARN (auto-loaded from client.yaml)",
        )
        
        if agent_arn != st.session_state.agent_arn:
            st.session_state.agent_arn = agent_arn
        
        with st.expander("View ARN Details"):
            if agent_arn:
                try:
                    parts = agent_arn.split(":")
                    st.caption(f"Region: {parts[3]}")
                    st.caption(f"Account: {parts[4]}")
                    st.caption(f"Runtime: {parts[5].split('/')[1]}")
                except:
                    st.caption("Invalid ARN format")
        
        st.divider()
        
        # Session configuration
        st.subheader("📊 Session")
        st.caption(f"ID: {st.session_state.runtime_session_id[:16]}...")
        st.caption(f"Messages: {len(st.session_state.messages)}")
        
        # Show fresh start indicator
        if st.session_state.get("is_fresh_start") and len(st.session_state.messages) == 0:
            st.info("✨ Fresh session - no conversation history")
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 New Session", use_container_width=True):
                st.session_state.runtime_session_id = str(uuid.uuid4())
                st.session_state.messages = []
                st.session_state.is_fresh_start = True
                st.rerun()
        
        with col2:
            if st.button("🗑️ Clear Chat", use_container_width=True):
                st.session_state.messages = []
                st.rerun()
        
        st.divider()
        
        # Display options
        st.subheader("🎨 Display Options")
        auto_format = st.checkbox("Auto-format responses", value=True)
        show_raw = st.checkbox("Show raw response", value=False)
        
        # Debug panel (only show if DEBUG_MODE is enabled)
        if DEBUG_MODE:
            st.divider()
            st.subheader("🔍 Debug Info")
            st.caption(f"Debug mode: ENABLED")
            st.caption(f"Session ID: {st.session_state.runtime_session_id}")
            st.caption(f"Message count: {len(st.session_state.messages)}")
            st.caption(f"Fresh start: {st.session_state.get('is_fresh_start', False)}")
            
            if st.button("📋 Copy Session ID", use_container_width=True):
                st.code(st.session_state.runtime_session_id)
            
            with st.expander("View Session State"):
                st.json({
                    "runtime_session_id": st.session_state.runtime_session_id,
                    "agent_arn": st.session_state.agent_arn,
                    "message_count": len(st.session_state.messages),
                    "is_fresh_start": st.session_state.get("is_fresh_start", False),
                    "pending_query": st.session_state.get("pending_query")
                })
        
        st.divider()
        
        # Prompt Library
        st.subheader("📚 Prompt Library")
        
        # Load prompts from YAML
        prompts_file = os.path.join(os.path.dirname(__file__), '..', 'shared', 'prompts.yaml')
        
        try:
            with open(prompts_file, 'r', encoding='utf-8') as f:
                prompts_data = yaml.safe_load(f)
            categories = prompts_data.get('categories', {})
        except FileNotFoundError:
            st.error("❌ prompts.yaml not found")
            categories = {}
        except yaml.YAMLError as e:
            st.error(f"❌ Error parsing prompts.yaml: {e}")
            categories = {}
        
        # Category icons (fallback if not in YAML)
        category_icons = {
            "cost_overview": "💰",
            "service_deep_dive": "🔍",
            "optimization": "✨",
            "resource_identification": "📦",
            "budget_monitoring": "📊",
            "data_transfer": "🌐",
            "pricing": "💵"
        }
        
        # Display categories with expandable sections
        for category_key, category_data in categories.items():
            icon = category_data.get('icon', category_icons.get(category_key, "📋"))
            category_name = category_data.get('name', category_key)
            prompts = category_data.get('prompts', [])
            
            with st.expander(f"{icon} {category_name}"):
                # Show all prompts in the category
                for idx, prompt_info in enumerate(prompts):
                    title = prompt_info.get('title', 'Untitled')
                    prompt_text = prompt_info.get('prompt', '')
                    
                    if st.button(
                        title,
                        key=f"prompt_{category_key}_{idx}",
                        use_container_width=True,
                        help=prompt_text[:100] + "..." if len(prompt_text) > 100 else prompt_text
                    ):
                        # Only set if no pending query exists
                        if not st.session_state.get("pending_query"):
                            st.session_state.pending_query = prompt_text
                            logger.info(f"Prompt button clicked: {title}")
        
        st.divider()
        
        # Status
        if agent_arn:
            st.success("✅ Agent ready")
        else:
            st.error("❌ No agent selected")
        
        return region, auto_format, show_raw


def main():
    """Main application"""
    initialize_session_state()
    
    # Header
    st.title("💰 Cost Analyzer Agent")
    st.caption("AI-powered AWS cost optimization agent")
    
    # Show debug mode indicator
    if DEBUG_MODE:
        st.info("🔍 Debug mode enabled - Check console/logs for detailed output")
    
    # Sidebar
    region, auto_format, show_raw = render_sidebar()
    
    logger.info(f"Rendering app - Messages: {len(st.session_state.messages)}, Session: {st.session_state.runtime_session_id[:16]}...")
    
    # Display chat messages
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    
    # Check if there's a pending query from prompt library
    pending_query = st.session_state.get("pending_query")
    
    # Always show chat input
    user_input = st.chat_input("Ask me about your AWS costs...")
    
    # Determine which prompt to process
    prompt = None
    if pending_query:
        logger.info(f"Processing pending query from library: {pending_query[:50]}...")
        # Use the pending query
        prompt = pending_query
        # Clear it IMMEDIATELY to prevent double processing
        st.session_state.pending_query = None
        logger.info("Cleared pending_query to prevent reprocessing")
    elif user_input:
        # Use the user's typed input
        prompt = user_input
        logger.info(f"Processing user input: {user_input[:50]}...")
    
    # Process the prompt
    if prompt:
        if not st.session_state.agent_arn:
            st.error("Please configure an agent ARN in the sidebar first.")
            return
        
        try:
            # Add user message
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
            
            # Generate assistant response
            with st.chat_message("assistant"):
                message_placeholder = st.empty()
                chunk_buffer = ""
                blocks = []
                current_block = ""
                
                try:
                    # Show thinking status while streaming
                    with st.status("🤔 Thinking...", expanded=False) as status:
                        for chunk in invoke_agent_streaming(
                            prompt,
                            st.session_state.agent_arn,
                            st.session_state.runtime_session_id,
                            region,
                        ):
                            text = str(chunk)
                            
                            if "[BLOCK_END]" in text:
                                parts = text.split("[BLOCK_END]")
                                current_block += parts[0]
                                block_text = current_block.strip()
                                if block_text:
                                    blocks.append(block_text)
                                    first_line = block_text.split('.')[0].split('\n')[0]
                                    if len(first_line) > 80:
                                        first_line = first_line[:80] + "..."
                                    status.update(label=f"🤔 {first_line}", state="running")
                                current_block = parts[-1] if len(parts) > 1 else ""
                            else:
                                current_block += text
                            
                            chunk_buffer += text
                        
                        status.update(label="✅ Done", state="complete")
                    
                    # Save the last block
                    last_block = current_block.strip()
                    if last_block:
                        blocks.append(last_block)
                    
                    # If no [BLOCK_END] markers (old agent), strip thinking text
                    # by finding where the formatted response starts
                    if len(blocks) <= 1 and blocks:
                        import re
                        full_text = blocks[0]
                        # Find the first markdown header (## or #) or table start
                        match = re.search(r'(#{1,3} |\| .*\|)', full_text)
                        if match and match.start() > 10:
                            thinking = full_text[:match.start()].strip()
                            formatted = full_text[match.start():].strip()
                            if thinking and formatted:
                                blocks = [thinking, formatted]
                    
                    # Display only the final block
                    final_response = blocks[-1] if blocks else chunk_buffer
                    if auto_format:
                        final_response = clean_response_text(final_response)
                    
                    message_placeholder.markdown(final_response)
                    
                    if show_raw:
                        with st.expander("View raw response"):
                            st.text(chunk_buffer)
                    
                    if len(blocks) > 1:
                        with st.expander("💭 View agent thinking"):
                            for i, block in enumerate(blocks[:-1]):
                                st.caption(f"Step {i + 1}: {block}")
                
                except Exception as e:
                    error_msg = f"❌ Error: {str(e)}"
                    message_placeholder.markdown(error_msg)
                    final_response = error_msg
            
            # Add to history
            st.session_state.messages.append({"role": "assistant", "content": final_response})
        
        except Exception as e:
            st.error(f"Error: {str(e)}")
            logger.error(f"Error processing prompt: {str(e)}", exc_info=True)
    
    # Footer
    st.divider()
    col1, col2, col3 = st.columns(3)
    with col1:
        st.caption("**🔧 Tools:** Cost Explorer, Billing, AWS Knowledge, Athena")
    with col2:
        st.caption("**📊 Data:** CUR, VPC Flow Logs, AWS Pricing")
    with col3:
        st.caption("**🎯 Features:** Analysis, Optimization, Network Insights")


if __name__ == "__main__":
    main()
