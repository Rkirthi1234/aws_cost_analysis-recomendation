#!/usr/bin/env python3
"""
Cost Analyzer Agent - CLI Interface for AgentCore
Invokes the deployed Bedrock AgentCore agent via AWS SDK
"""
import sys
import os
import argparse
import json
import uuid
import boto3
import threading
import time
import yaml
from botocore.exceptions import ClientError
from rich.console import Console
from rich.markdown import Markdown

rich_console = Console()


class ThinkingAnimation:
    """Animated thinking indicator with live timer"""
    
    def __init__(self):
        self.is_running = False
        self.thread = None
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.current_frame = 0
        self.start_time = 0
        self.messages = [
            "Thinking",
            "Analyzing",
            "Querying AWS",
            "Crunching numbers",
            "Processing",
        ]
    
    def _animate(self):
        """Animation loop with timer and rotating messages"""
        while self.is_running:
            elapsed = time.time() - self.start_time
            frame = self.frames[self.current_frame % len(self.frames)]
            msg_idx = int(elapsed / 4) % len(self.messages)
            msg = self.messages[msg_idx]
            dots = "." * (1 + int(elapsed * 2) % 3)
            timer = f"\033[90m({elapsed:.1f}s)\033[0m"
            sys.stdout.write(f"\r\033[K\033[1;33m{frame} {msg}{dots}\033[0m {timer}")
            sys.stdout.flush()
            self.current_frame += 1
            time.sleep(0.1)  # nosemgrep: arbitrary-sleep  # Animation frame rate for CLI spinner
    
    def start(self):
        """Start the animation"""
        if not self.is_running:
            self.is_running = True
            self.start_time = time.time()
            self.current_frame = 0
            self.thread = threading.Thread(target=self._animate, daemon=True)
            self.thread.start()
    
    def stop(self):
        """Stop the animation and show elapsed time"""
        if self.is_running:
            elapsed = time.time() - self.start_time
            self.is_running = False
            if self.thread:
                self.thread.join(timeout=0.5)
            # Clear line and show completion time
            sys.stdout.write(f"\r\033[K\033[90m✓ Response in {elapsed:.1f}s\033[0m\n")
            sys.stdout.flush()


class AgentCoreCLI:
    """CLI wrapper for invoking AgentCore deployed agent"""
    
    def __init__(self, agent_id, region='us-east-1', verbose=False):
        self.agent_id = agent_id
        self.region = region
        self.verbose = verbose
        
        # Use bedrock-agentcore client (not bedrock-agent-runtime)
        from botocore.config import Config
        config = Config(
            retries={
                'max_attempts': 1,
                'mode': 'standard'
            },
            read_timeout=300,
            connect_timeout=60
        )
        self.client = boto3.client('bedrock-agentcore', region_name=region, config=config)
        self.session_id = str(uuid.uuid4())
        
        # Get account ID dynamically
        try:
            sts = boto3.client('sts')
            self.account_id = sts.get_caller_identity()['Account']
            self._print_verbose(f"Account ID: {self.account_id}")
        except Exception as e:
            self._print_verbose(f"Could not get account ID: {e}")
            self.account_id = None
    
    def _print_verbose(self, message):
        """Print verbose messages"""
        if self.verbose:
            print(f"\n[DEBUG] {message}", file=sys.stderr)
    
    def _extract_text(self, data):
        """Extract text from response data"""
        if isinstance(data, str):
            return data
        
        if isinstance(data, dict):
            # Handle result wrapper
            if "result" in data:
                return self._extract_text(data["result"])
            
            # Handle message.content structure
            if "role" in data and "content" in data:
                content = data["content"]
                if isinstance(content, list) and len(content) > 0:
                    if isinstance(content[0], dict) and "text" in content[0]:
                        return str(content[0]["text"])
                    return str(content[0])
                return str(content)
            
            # Direct text field
            if "text" in data:
                return str(data["text"])
            elif "content" in data:
                return str(data["content"])
        
        return str(data)
    
    def invoke(self, user_input):
        """Invoke the AgentCore agent with user input.
        
        Shows animated thinking status from intermediate blocks, then renders
        the final response with rich markdown formatting.
        """
        try:
            if not self.account_id:
                return "❌ Error: Could not determine AWS account ID. Please check your credentials."
            
            agent_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account_id}:runtime/{self.agent_id}"
            
            self._print_verbose(f"Agent ID: {self.agent_id}")
            self._print_verbose(f"Agent ARN: {agent_arn}")
            self._print_verbose(f"Session ID: {self.session_id}")
            self._print_verbose(f"Region: {self.region}")
            self._print_verbose(f"User Input: {user_input}")
            
            import json
            payload = {"prompt": user_input}
            
            start_time = time.time()
            
            # Background spinner state — shared with animation thread
            spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            spinner_status = {"text": "Thinking", "running": True}
            
            def animate_spinner():
                """Background thread that keeps the spinner animated."""
                idx = 0
                while spinner_status["running"]:
                    elapsed = time.time() - start_time
                    frame = spinner_frames[idx % len(spinner_frames)]
                    text = spinner_status["text"]
                    dots = "." * (1 + int(elapsed * 2) % 3)
                    timer = f"\033[90m({elapsed:.1f}s)\033[0m"
                    sys.stdout.write(f"\r\033[K\033[1;33m{frame} {text}{dots}\033[0m {timer}")
                    sys.stdout.flush()
                    idx += 1
                    time.sleep(0.08)  # nosemgrep: arbitrary-sleep  # Animation frame rate
            
            # Start background spinner
            spinner_thread = None
            if not self.verbose:
                spinner_thread = threading.Thread(target=animate_spinner, daemon=True)
                spinner_thread.start()
            
            # Invoke agent
            response = self.client.invoke_agent_runtime(
                agentRuntimeArn=agent_arn,
                qualifier='DEFAULT',
                runtimeSessionId=self.session_id,
                payload=json.dumps(payload)
            )
            
            self._print_verbose("Response received, processing stream...")
            self._print_verbose(f"Content type: {response.get('contentType')}")
            
            # Collect blocks separated by [BLOCK_END] markers
            
            # Collect blocks separated by [BLOCK_END] markers
            # Intermediate blocks = thinking, last block = final response
            blocks = []
            current_block = []
            
            if "text/event-stream" in response.get("contentType", ""):
                for line in response["response"].iter_lines(chunk_size=1):
                    if line:
                        line = line.decode("utf-8")
                        
                        if line.startswith("data: "):
                            line = line[6:]
                            
                            try:
                                data = json.loads(line)
                                text = self._extract_text(data)
                            except json.JSONDecodeError:
                                text = line
                            
                            if not text:
                                continue
                            
                            if "[BLOCK_END]" in text:
                                parts = text.split("[BLOCK_END]")
                                current_block.append(parts[0])
                                block_text = ''.join(current_block).strip()
                                if block_text:
                                    blocks.append(block_text)
                                    # Update spinner with thinking status
                                    if not self.verbose:
                                        first_line = block_text.split('.')[0].split('\n')[0]
                                        if len(first_line) > 60:
                                            first_line = first_line[:60] + "..."
                                        spinner_status["text"] = first_line
                                current_block = [parts[-1]] if len(parts) > 1 and parts[-1] else []
                            else:
                                current_block.append(text)
            else:
                # Non-streaming JSON response
                response_obj = response.get("response")
                if hasattr(response_obj, "read"):
                    content = response_obj.read()
                    if isinstance(content, bytes):
                        content = content.decode("utf-8")
                    try:
                        response_data = json.loads(content)
                        text = self._extract_text(response_data)
                        current_block.append(text)
                    except json.JSONDecodeError:
                        current_block.append(content)
            
            # Save the last block
            last_block = ''.join(current_block).strip()
            if last_block:
                blocks.append(last_block)
            
            elapsed = time.time() - start_time
            
            # Stop the spinner
            spinner_status["running"] = False
            if spinner_thread:
                spinner_thread.join(timeout=0.5)
            
            # The last block is the final formatted response
            final_response = blocks[-1] if blocks else ''
            full_text = '\n\n'.join(blocks)
            
            if not self.verbose:
                sys.stdout.write(f"\r\033[K\033[90m✓ Response in {elapsed:.1f}s\033[0m\n")
                sys.stdout.flush()
                if final_response:
                    rich_console.print(Markdown(final_response))
            
            return full_text
            
        except ClientError as e:
            spinner_status["running"] = False
            if spinner_thread:
                spinner_thread.join(timeout=0.5)
            sys.stdout.write(f"\r\033[K")
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            
            if error_code == 'ResourceNotFoundException':
                return f"❌ Error: Agent not found. Please check your agent ID: {self.agent_id}"
            elif error_code == 'AccessDeniedException':
                return f"❌ Error: Access denied. Please check your AWS credentials and IAM permissions."
            else:
                return f"❌ Error ({error_code}): {error_msg}"
                
        except Exception as e:
            spinner_status["running"] = False
            if spinner_thread:
                spinner_thread.join(timeout=0.5)
            sys.stdout.write(f"\r\033[K")
            if self.verbose:
                import traceback
                traceback.print_exc()
            return f"❌ Unexpected error: {str(e)}"
    
    def interactive(self):
        """Start interactive chat mode"""
        print("\n💬 Interactive mode started. Type 'exit' or 'quit' to end.")
        print(f"📡 Connected to AgentCore agent: {self.agent_id}")
        print("\n💡 Tip: Type '/prompts' to see the prompt library\n")
        
        while True:
            try:
                # Get user input
                user_input = input("\n\033[1;36mYou:\033[0m ")
                
                # Check for exit commands
                if user_input.lower() in ['exit', 'quit', 'q']:
                    print("\n👋 Goodbye!\n")
                    break
                
                # Check for prompt library command
                if user_input.lower() in ['/prompts', '/prompt', '/library', '/help']:
                    self._show_prompt_library()
                    continue
                
                # Skip empty input
                if not user_input.strip():
                    continue
                
                # Process query
                print("\n\033[1;32mAgent:\033[0m ", end="", flush=True)
                self.invoke(user_input)
                
            except KeyboardInterrupt:
                print("\n\n👋 Goodbye!\n")
                break
            except EOFError:
                print("\n\n👋 Goodbye!\n")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}\n")
                if self.verbose:
                    import traceback
                    traceback.print_exc()
    
    def _show_prompt_library(self):
        """Display the prompt library"""
        # Load prompts from YAML
        prompts_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'shared', 'prompts.yaml')
        
        try:
            with open(prompts_file, 'r', encoding='utf-8') as f:
                prompts_data = yaml.safe_load(f)
        except FileNotFoundError:
            print("\n❌ Error: prompts.yaml not found")
            print("Please ensure shared/prompts.yaml exists")
            return
        except yaml.YAMLError as e:
            print(f"\n❌ Error parsing prompts.yaml: {e}")
            return
        
        categories = prompts_data.get('categories', {})
        
        print("\n\033[1;35m📚 Prompt Library\033[0m")
        print("\033[90m" + "="*60 + "\033[0m\n")
        
        category_icons = {
            "cost_overview": "💰",
            "service_deep_dive": "🔍",
            "optimization": "✨",
            "resource_identification": "📦",
            "budget_monitoring": "📊",
            "data_transfer": "🌐",
            "pricing": "💵"
        }
        
        # Display all categories and prompts
        for idx, (category_key, category_data) in enumerate(categories.items(), 1):
            icon = category_data.get('icon', category_icons.get(category_key, "📋"))
            category_name = category_data.get('name', category_key)
            prompts = category_data.get('prompts', [])
            
            print(f"\033[1;33m{icon} {category_name}\033[0m")
            
            for prompt_idx, prompt_info in enumerate(prompts, 1):
                title = prompt_info.get('title', 'Untitled')
                print(f"  \033[36m{idx}.{prompt_idx}\033[0m {title}")
            
            print()
        
        print("\033[90m" + "="*60 + "\033[0m")
        print("\n💡 To use a prompt, type the number (e.g., '1.1') or just type your own question\n")
        
        # Get user selection
        selection = input("\033[1;36mSelect a prompt (or press Enter to skip):\033[0m ")
        
        if selection.strip():
            # Parse selection (e.g., "1.1")
            try:
                parts = selection.split('.')
                if len(parts) == 2:
                    cat_idx = int(parts[0]) - 1
                    prompt_idx = int(parts[1]) - 1
                    
                    # Get the prompt
                    categories_list = list(categories.items())
                    if 0 <= cat_idx < len(categories_list):
                        category_key, category_data = categories_list[cat_idx]
                        prompts_list = category_data.get('prompts', [])
                        
                        if 0 <= prompt_idx < len(prompts_list):
                            prompt_info = prompts_list[prompt_idx]
                            selected_prompt = prompt_info.get('prompt', '')
                            title = prompt_info.get('title', 'Query')
                            
                            print(f"\n\033[1;36mYou:\033[0m {title}")
                            print(f"\033[90m({selected_prompt})\033[0m")
                            print("\n\033[1;32mAgent:\033[0m ", end="", flush=True)
                            self.invoke(selected_prompt)
                            return
                
                print("\n❌ Invalid selection. Please use format like '1.1'\n")
            except (ValueError, IndexError):
                print("\n❌ Invalid selection. Please use format like '1.1'\n")


def load_agent_config():
    """Load agent ID from shared/client.yaml or .bedrock_agentcore.yaml"""
    # Try shared/client.yaml first
    try:
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'shared', 'client.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            agent_id = config.get('agentcore', {}).get('agent_id')
            if agent_id:
                return agent_id
    except Exception:
        pass
    
    # Fallback to .bedrock_agentcore.yaml in root
    try:
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.bedrock_agentcore.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            return config.get('agent_id')
    except Exception:
        return None


def main():
    """Main entry point for CLI"""
    parser = argparse.ArgumentParser(
        description="Cost Analyzer Agent - AgentCore CLI Interface"
    )
    parser.add_argument(
        '-i', '--interactive',
        action='store_true',
        help='Start interactive chat mode'
    )
    parser.add_argument(
        '-q', '--query',
        type=str,
        help='Send a single query and exit'
    )
    parser.add_argument(
        '-a', '--agent-id',
        type=str,
        help='AgentCore agent ID (default: from .bedrock_agentcore.yaml)'
    )
    parser.add_argument(
        '-r', '--region',
        type=str,
        default='us-east-1',
        help='AWS region (default: us-east-1)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    
    args = parser.parse_args()
    
    # Get agent ID
    agent_id = args.agent_id or load_agent_config()
    
    if not agent_id:
        print("❌ Error: Agent ID not found.")
        print("\nPlease provide agent ID using one of these methods:")
        print("  1. Add to shared/client.yaml:")
        print("     agentcore:")
        print("       agent_id: cost_analyzer_agent-5gTQmv5pqK")
        print("  2. Use --agent-id flag: --agent-id cost_analyzer_agent-5gTQmv5pqK")
        print("  3. Add to .bedrock_agentcore.yaml with agent_id field")
        print("\nExample:")
        print("  ./cli/cli.sh -a cost_analyzer_agent-5gTQmv5pqK -q 'What are my costs?'")
        sys.exit(1)
    
    # Create CLI client
    cli = AgentCoreCLI(agent_id=agent_id, region=args.region, verbose=args.verbose)
    
    # Execute based on mode
    if args.interactive:
        cli.interactive()
    elif args.query:
        response = cli.invoke(args.query)
        if args.verbose:
            print(f"\n{response}")
    else:
        # Default to interactive if no mode specified
        cli.interactive()


if __name__ == "__main__":
    main()
