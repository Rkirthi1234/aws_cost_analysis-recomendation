"""Configuration service for loading and managing config.

Includes T6 mitigation: config file integrity validation via SHA-256 hash
to detect unauthorized modifications to account registry and other settings.
"""
import hashlib
import logging
import yaml
from typing import Dict, Any, Optional, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from agent.services.account_registry import AccountRegistry

logger = logging.getLogger("ConfigService")


class ConfigService:
    """Manages application configuration with integrity validation."""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self._config_hash: Optional[str] = None
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file and compute integrity hash."""
        config_file = Path(self.config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        
        raw_content = config_file.read_bytes()
        self._config_hash = hashlib.sha256(raw_content).hexdigest()
        logger.info(f"Config loaded: {self.config_path} (hash: {self._config_hash[:16]}...)")
        
        return yaml.safe_load(raw_content.decode('utf-8'))
    
    def verify_integrity(self) -> bool:
        """T6 Mitigation: Verify config file has not been modified since load.
        
        Returns True if the file matches the hash computed at load time.
        Returns False if the file has been modified or cannot be read.
        """
        try:
            config_file = Path(self.config_path)
            current_hash = hashlib.sha256(config_file.read_bytes()).hexdigest()
            if current_hash != self._config_hash:
                logger.warning(
                    f"Config integrity check FAILED: {self.config_path} "
                    f"(expected: {self._config_hash[:16]}..., got: {current_hash[:16]}...)"
                )
                return False
            return True
        except Exception as e:
            logger.error(f"Config integrity check error: {e}")
            return False
    
    @property
    def config_hash(self) -> Optional[str]:
        """Return the SHA-256 hash of the config file at load time."""
        return self._config_hash
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """Get config value using dot notation (e.g., 'aws.region')."""
        keys = key_path.split('.')
        value = self.config
        
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        
        return value
    
    @property
    def aws_region(self) -> str:
        return self.get('aws.region', 'us-east-1')
    
    @property
    def mcp_enabled(self) -> bool:
        return self.get('mcp.enabled', True)
    
    @property
    def mcp_servers(self) -> Dict[str, Any]:
        """Get all MCP server configurations."""
        return self.get('mcp.servers', {})
    
    @property
    def model_provider(self) -> str:
        return self.get('agent.model.provider', 'bedrock')
    
    @property
    def model_id(self) -> str:
        return self.get('agent.model.model_id')
    
    @property
    def model_temperature(self) -> float:
        return self.get('agent.model.temperature', 0.1)
    
    @property
    def model_max_tokens(self) -> int:
        return self.get('agent.model.max_tokens', 4096)
    
    @property
    def model_cache_tools(self) -> bool:
        """Get tool caching configuration."""
        return self.get('agent.model.cache_tools', False)
    
    @property
    def model_cache_ttl(self) -> str:
        """Get cache TTL configuration."""
        ttl = self.get('agent.model.cache_ttl', '5m')
        # Validate TTL value
        if ttl not in ['5m', '1h']:
            raise ValueError(f"Invalid cache_ttl value: {ttl}. Must be '5m' or '1h'")
        return ttl

    @property
    def account_registry(self) -> 'AccountRegistry':
        """Parse and return the account registry from config."""
        from agent.services.account_registry import AccountRegistry
        accounts_config = self.get('accounts', [])
        return AccountRegistry(accounts_config, self.aws_region)
