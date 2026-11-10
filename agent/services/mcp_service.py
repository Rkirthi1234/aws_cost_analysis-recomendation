"""MCP service for initializing MCP servers."""
import os
import asyncio
import logging
from mcp import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from strands.tools.mcp import MCPClient
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class MCPService:
    """Handles MCP server initialization for multiple servers (stdio and HTTP)."""
    
    def __init__(self, servers_config: Dict):
        """Initialize MCP service with server configurations.
        
        Args:
            servers_config: Dictionary of server configurations
                Example: {
                    'billing': {
                        'type': 'stdio',
                        'package': '...',
                        'log_level': 'ERROR',
                        'enabled': True
                    },
                    'aws_knowledge': {
                        'type': 'http',
                        'url': 'https://knowledge-mcp.global.api.aws',
                        'enabled': True
                    }
                }
        """
        self.servers_config = servers_config
        self.clients: Dict[str, Optional[MCPClient]] = {}
    
    def initialize(self):
        """Initialize all enabled MCP servers (synchronous - kept for backward compatibility)."""
        for name, config in self.servers_config.items():
            if config.get('enabled', True):
                server_type = config.get('type', 'stdio')
                print(f"🔌 Initializing {name} MCP Server ({server_type})...")
                
                if server_type == 'stdio':
                    client = self._create_stdio_client(
                        name=name,
                        package=config['package'],
                        log_level=config.get('log_level', 'ERROR')
                    )
                elif server_type == 'http':
                    client = self._create_http_client(
                        name=name,
                        url=config['url']
                    )
                else:
                    print(f"   ⚠️  Unknown server type: {server_type}")
                    client = None
                
                self.clients[name] = client
    
    async def initialize_async(self):
        """Initialize all enabled MCP servers concurrently (async - recommended)."""
        tasks = []
        server_names = []
        
        for name, config in self.servers_config.items():
            if config.get('enabled', True):
                server_type = config.get('type', 'stdio')
                logger.info(f"🔌 Preparing {name} MCP Server ({server_type})...")
                
                if server_type == 'stdio':
                    task = self._create_stdio_client_async(
                        name=name,
                        package=config['package'],
                        log_level=config.get('log_level', 'ERROR')
                    )
                elif server_type == 'http':
                    task = self._create_http_client_async(
                        name=name,
                        url=config['url']
                    )
                else:
                    logger.warning(f"   ⚠️  Unknown server type: {server_type}")
                    continue
                
                tasks.append(task)
                server_names.append(name)
        
        # Execute all initialization tasks concurrently
        if tasks:
            logger.info(f"Initializing {len(tasks)} MCP servers in parallel...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            for name, result in zip(server_names, results):
                if isinstance(result, Exception):
                    logger.error(f"   ⚠️  Failed to initialize {name}: {result}")
                    self.clients[name] = None
                else:
                    self.clients[name] = result
                    if result:
                        logger.info(f"   ✅ {name} server initialized!")
        else:
            logger.info("No MCP servers to initialize")
    
    async def _create_stdio_client_async(self, name: str, package: str, log_level: str = "ERROR") -> Optional[MCPClient]:
        """Create a stdio-based MCP client asynchronously."""
        try:
            # Run the synchronous client creation in a thread pool
            loop = asyncio.get_event_loop()
            client = await loop.run_in_executor(
                None,
                lambda: MCPClient(
                    lambda: stdio_client(
                        StdioServerParameters(
                            command="python",
                            args=["-m", "uv", "tool", "run", package],
                            env={
                                **os.environ,
                                "FASTMCP_LOG_LEVEL": log_level,
                                "UV_NO_PROGRESS": "1"
                            }
                        )
                    )
                )
            )
            return client
            
        except Exception as e:
            logger.error(f"Failed to initialize {name} server: {e}")
            return None
    
    async def _create_http_client_async(self, name: str, url: str) -> Optional[MCPClient]:
        """Create an HTTP-based MCP client asynchronously."""
        try:
            # Run the synchronous client creation in a thread pool
            loop = asyncio.get_event_loop()
            client = await loop.run_in_executor(
                None,
                lambda: MCPClient(
                    lambda: streamable_http_client(url)
                )
            )
            return client
            
        except Exception as e:
            logger.error(f"Failed to initialize {name} server: {e}")
            logger.info(f"Remote server may be unavailable: {url}")
            logger.info(f"Agent will continue without {name} tools")
            return None
    
    def _create_stdio_client(self, name: str, package: str, log_level: str = "ERROR") -> Optional[MCPClient]:
        """Create a stdio-based MCP client (local package via uvx)."""
        try:
            # In AgentCore runtime, use python -m uv tool run instead of uvx
            # This works because uv is installed as a Python package
            client = MCPClient(
                lambda: stdio_client(
                    StdioServerParameters(
                        command="python",
                        args=["-m", "uv", "tool", "run", package],
                        env={
                            **os.environ,
                            "FASTMCP_LOG_LEVEL": log_level,
                            "UV_NO_PROGRESS": "1"
                        }
                    )
                )
            )
            
            print(f"   ✅ {name} server initialized!")
            return client
            
        except Exception as e:
            print(f"   ⚠️  Failed to initialize {name} server: {e}")
            return None
    
    def _create_http_client(self, name: str, url: str) -> Optional[MCPClient]:
        """Create an HTTP-based MCP client (remote server using Streamable HTTP)."""
        try:
            client = MCPClient(
                lambda: streamable_http_client(url)
            )
            
            print(f"   ✅ {name} server initialized (remote)!")
            return client
            
        except Exception as e:
            print(f"   ⚠️  Failed to initialize {name} server: {e}")
            print(f"      Remote server may be unavailable: {url}")
            print(f"      Agent will continue without {name} tools")
            return None
    
    def get_client(self, name: str) -> Optional[MCPClient]:
        """Get a specific MCP client by name."""
        return self.clients.get(name)
    
    def get_all_clients(self) -> list:
        """Get all initialized MCP clients as a list."""
        return [client for client in self.clients.values() if client is not None]
