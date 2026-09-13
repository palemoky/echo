from __future__ import annotations

import os
from abc import ABC, abstractmethod

import httpx

from echo_voc.errors import ConfigError, EchoError


class Embedder(ABC):
    name: str
    model: str
    dim: int = 1024

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        return None


class OllamaEmbedder(Embedder):
    name = "ollama"

    def __init__(self, model: str = "bge-m3", base_url: str | None = None, batch_size: int = 32):
        self.model = model
        self.base_url = (
            base_url or os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
        ).rstrip("/")
        self.batch_size = batch_size
        self._client = httpx.AsyncClient(timeout=120)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = [t[:4000] for t in texts[start : start + self.batch_size]]
            try:
                resp = await self._client.post(
                    f"{self.base_url}/api/embed", json={"model": self.model, "input": chunk}
                )
            except httpx.ConnectError as e:
                raise ConfigError(
                    f"Cannot reach Ollama at {self.base_url}; start it with `ollama serve`"
                ) from e
            if resp.status_code != 200:
                raise EchoError(f"Ollama embed failed ({resp.status_code}): {resp.text[:200]}")
            vectors.extend(resp.json()["embeddings"])
        return vectors

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAIEmbedder(Embedder):
    """Any OpenAI-compatible /embeddings endpoint; requests 1024 dims to match the schema."""

    name = "openai"

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        from openai import AsyncOpenAI

        self.model = model
        key = api_key or os.getenv("ECHO_EMBED_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not key:
            raise ConfigError("Set OPENAI_API_KEY or ECHO_EMBED_API_KEY for OpenAI embeddings")
        self._client = AsyncOpenAI(
            api_key=key, base_url=base_url or os.getenv("ECHO_EMBED_BASE_URL")
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 256):
            resp = await self._client.embeddings.create(
                model=self.model, input=texts[start : start + 256], dimensions=self.dim
            )
            vectors.extend(item.embedding for item in resp.data)
        return vectors

    async def aclose(self) -> None:
        await self._client.close()


def create_embedder(provider: str | None = None, model: str | None = None) -> Embedder:
    provider = (provider or os.getenv("ECHO_EMBEDDER") or "ollama").lower()
    model = model or os.getenv("ECHO_EMBED_MODEL")
    if provider == "ollama":
        return OllamaEmbedder(model=model or "bge-m3")
    if provider == "openai":
        return OpenAIEmbedder(model=model or "text-embedding-3-small")
    raise ConfigError(f"Unknown embedder '{provider}'. Use ollama or openai")


__all__ = ["Embedder", "OllamaEmbedder", "OpenAIEmbedder", "create_embedder"]
