from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class LLMProviderError(RuntimeError):
    """Safe provider failure without private response bodies."""


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError
