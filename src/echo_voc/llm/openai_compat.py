from __future__ import annotations

from openai import AsyncOpenAI, OpenAIError

from echo_voc.errors import LLMError
from echo_voc.llm.base import LLM


class OpenAICompatLLM(LLM):
    """Any OpenAI-compatible Chat Completions endpoint (OpenAI, DeepSeek, Qwen, Ollama, ...)."""

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float | None = 0.2,
        supports_json_mode: bool = True,
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.supports_json_mode = supports_json_mode
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=3)

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        kwargs = {}
        if json_mode and self.supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **kwargs,
            )
        except OpenAIError as e:
            raise LLMError(f"{self.provider} request failed: {e}") from e
        return resp.choices[0].message.content or ""

    async def aclose(self) -> None:
        await self._client.close()
