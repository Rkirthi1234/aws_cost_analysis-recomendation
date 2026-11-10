"""Base class for tool plugins."""
from abc import ABC, abstractmethod
from typing import List, Callable


class BaseTool(ABC):
    """Abstract base class for tool plugins."""
    
    @abstractmethod
    def get_tools(self) -> List[Callable]:
        """Return list of tool functions to register with the agent."""
        pass
