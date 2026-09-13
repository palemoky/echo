"""High-level entry points shared by the CLI and the MCP server."""

from __future__ import annotations

from collections.abc import Callable

from echo_voc.analyzer import Analyzer
from echo_voc.llm import create_llm
from echo_voc.models import FetchResult, Report
from echo_voc.sources import resolve_source


async def fetch_comments(target: str, limit: int = 200, platform: str | None = None) -> FetchResult:
    source = resolve_source(target, platform)
    try:
        return await source.fetch(target, limit=limit)
    finally:
        await source.aclose()


async def analyze(
    data: FetchResult,
    provider: str | None = None,
    model: str | None = None,
    language: str | None = None,
    base_url: str | None = None,
    concurrency: int = 4,
    on_progress: Callable[[str], None] | None = None,
) -> Report:
    llm = create_llm(provider=provider, model=model, base_url=base_url)
    kwargs = {"language": language} if language else {}
    try:
        analyzer = Analyzer(llm, concurrency=concurrency, on_progress=on_progress, **kwargs)
        return await analyzer.analyze(data)
    finally:
        await llm.aclose()
