"""mem0 semantic memory sharing echo's PostgreSQL (pgvector) and embedder."""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path

from echo_voc.errors import ConfigError
from echo_voc.llm import PROVIDERS
from echo_voc.store.db import database_url

log = logging.getLogger(__name__)

COLLECTION = "echo_memories"


def mem0_config() -> dict:
    """Map echo's settings onto a mem0 config (LLM = ECHO_PROVIDER, embedder = Ollama bge-m3)."""
    name = (os.getenv("ECHO_PROVIDER") or "deepseek").lower()
    preset = PROVIDERS.get(name)
    if preset is None:
        raise ConfigError(f"Unknown provider '{name}' for memory")
    model = os.getenv("ECHO_MODEL") or preset.default_model
    key = (os.getenv(preset.api_key_env) if preset.api_key_env else None) or os.getenv(
        "ECHO_API_KEY"
    )
    base_url = os.getenv("ECHO_BASE_URL") or preset.base_url
    if name == "deepseek":
        llm = {
            "provider": "deepseek",
            "config": {"model": model, "api_key": key, "deepseek_base_url": base_url},
        }
    elif name == "anthropic":
        llm = {"provider": "anthropic", "config": {"model": model, "api_key": key}}
    elif name == "ollama":
        llm = {"provider": "ollama", "config": {"model": model, "ollama_base_url": _ollama_url()}}
    else:  # every other preset speaks the OpenAI protocol
        llm = {
            "provider": "openai",
            "config": {"model": model, "api_key": key, "openai_base_url": base_url},
        }

    embedder_name = (os.getenv("ECHO_EMBEDDER") or "ollama").lower()
    embed_model = os.getenv("ECHO_EMBED_MODEL") or (
        "bge-m3" if embedder_name == "ollama" else "text-embedding-3-small"
    )
    embedder = {"provider": embedder_name, "config": {"model": embed_model, "embedding_dims": 1024}}
    if embedder_name == "ollama":
        embedder["config"]["ollama_base_url"] = _ollama_url()

    home = Path(os.getenv("ECHO_HOME", Path.home() / ".echo"))
    home.mkdir(parents=True, exist_ok=True)
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "connection_string": database_url().replace(
                    "postgresql+psycopg://", "postgresql://"
                ),
                "collection_name": COLLECTION,
                "embedding_model_dims": 1024,
                "hnsw": True,
            },
        },
        "llm": llm,
        "embedder": embedder,
        "history_db_path": str(home / "mem0_history.db"),
        "custom_instructions": MEMORY_INSTRUCTIONS,
    }


MEMORY_INSTRUCTIONS = """These messages are analyses of user feedback about a product, app or \
content creator, not personal chat. Extract durable product-history facts: the period or version \
they refer to, sentiment levels, the main problems / requests / praise with their mention counts, \
and changes versus earlier periods. Always keep the period label and numbers in each memory."""


def infer_enabled() -> bool:
    return os.getenv("ECHO_MEMORY_INFER", "false").lower() in {"1", "true", "yes", "on"}


def _ollama_url() -> str:
    return os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"


class Mem0Memory:
    def __init__(self, memory) -> None:
        self._memory = memory

    @staticmethod
    def user_id(account_id: int) -> str:
        return f"account:{account_id}"

    async def remember(self, account_id: int, text: str, metadata: dict | None = None) -> list[str]:
        """Store a conclusion.

        By default the text is stored verbatim (it is already an LLM-written summary with period
        and numbers). With ECHO_MEMORY_INFER=true, mem0 extracts facts and merges them with
        existing memories instead.
        """
        result = await asyncio.to_thread(
            self._memory.add,
            [{"role": "user", "content": text}],
            user_id=self.user_id(account_id),
            metadata=metadata or {},
            infer=infer_enabled(),
        )
        return [r.get("memory", "") for r in (result or {}).get("results", [])]

    async def recall(self, query: str, account_ids: list[int], limit: int = 6) -> list[str]:
        if not account_ids:
            account_ids = await _all_account_ids()
        found: list[tuple[float, str]] = []
        for account_id in account_ids:
            result = await asyncio.to_thread(
                self._memory.search,
                query,
                filters={"user_id": self.user_id(account_id)},
                top_k=limit,
            )
            for item in (result or {}).get("results", []):
                meta = item.get("metadata") or {}
                prefix = f"[{meta['period']}] " if meta.get("period") else ""
                found.append((item.get("score") or 0.0, prefix + item.get("memory", "")))
        found.sort(key=lambda x: x[0], reverse=True)
        return [text for _, text in found[:limit]]

    async def all(self, account_id: int, limit: int = 100) -> list[dict]:
        result = await asyncio.to_thread(
            self._memory.get_all, filters={"user_id": self.user_id(account_id)}, top_k=limit
        )
        return (result or {}).get("results", [])


async def _all_account_ids() -> list[int]:
    from sqlalchemy import select

    from echo_voc.store.db import get_sessionmaker
    from echo_voc.store.models import Account

    async with get_sessionmaker()() as session:
        return list((await session.execute(select(Account.id))).scalars())


@lru_cache(maxsize=1)
def get_memory() -> Mem0Memory | None:
    """mem0 instance, or None when disabled (ECHO_MEMORY=off) or not installed."""
    if os.getenv("ECHO_MEMORY", "on").lower() in {"off", "0", "false"}:
        return None
    os.environ.setdefault("MEM0_TELEMETRY", "False")
    logging.getLogger("mem0.utils.spacy_models").setLevel(logging.ERROR)  # optional NLP extra
    try:
        from mem0 import Memory
    except ImportError:
        log.info("mem0 not installed; semantic memory disabled (pip install 'echo-voc[memory]')")
        return None
    return Mem0Memory(Memory.from_config(mem0_config()))
