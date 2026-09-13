from __future__ import annotations

from abc import ABC, abstractmethod


class LLM(ABC):
    """Minimal chat interface: a system prompt + a user prompt in, text out."""

    provider: str
    model: str

    @abstractmethod
    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        return None
