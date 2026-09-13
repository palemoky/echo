from __future__ import annotations

from anthropic import AnthropicError, AsyncAnthropic

from echo_voc.errors import LLMError
from echo_voc.llm.base import LLM


class AnthropicLLM(LLM):
    def __init__(
        self, model: str, api_key: str, base_url: str | None = None, max_tokens: int = 8192
    ) -> None:
        self.provider = "anthropic"
        self.model = model
        self.max_tokens = max_tokens
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=3)

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if json_mode:
            system += "\n\nRespond with a single valid JSON object only, no prose or code fences."
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except AnthropicError as e:
            raise LLMError(f"anthropic request failed: {e}") from e
        return "".join(block.text for block in resp.content if block.type == "text")

    async def aclose(self) -> None:
        await self._client.close()
