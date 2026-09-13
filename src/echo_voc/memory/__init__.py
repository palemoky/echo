"""Product memory: structured snapshots (always) + mem0 semantic memory (optional)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def recall_for_accounts(question: str, account_ids: list[int]) -> list[str]:
    """Best-effort mem0 recall; returns [] when mem0 is not installed or configured."""
    from echo_voc.memory.mem0_store import get_memory

    memory = get_memory()
    if memory is None:
        return []
    return await memory.recall(question, account_ids)
