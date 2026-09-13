from __future__ import annotations

from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from echo_voc import service
from echo_voc.llm import PROVIDERS
from echo_voc.report import render_markdown
from echo_voc.sources import SOURCES

load_dotenv(Path.cwd() / ".env")

server = MCPServer(
    name="echo",
    instructions=(
        "echo fetches user comments/reviews from platforms such as the App Store, YouTube and "
        "Bilibili. Use `fetch_comments` to read raw comments and analyze them yourself, or "
        "`analyze_comments` to get a full insight report produced by echo's configured LLM. "
        "For tracked accounts stored locally, use `track`, `sync`, `ask_feedback`, `list_topics`, "
        "`get_trend` and `recall_memory`."
    ),
)


@server.tool()
def list_platforms() -> list[dict]:
    """List supported comment platforms with an example URL for each."""
    return [
        {"platform": s.name, "description": s.description, "example": s.example}
        for s in SOURCES.values()
    ]


@server.tool()
async def fetch_comments(url: str, limit: int = 100, platform: str | None = None) -> dict:
    """Fetch user comments for an app or video.

    Args:
        url: App Store app URL, YouTube video URL or Bilibili video URL.
        limit: Maximum number of comments to return (1-500).
        platform: Optional platform name to skip auto-detection.
    """
    result = await service.fetch_comments(url, max(1, min(limit, 500)), platform)
    return {
        "platform": result.platform,
        "title": result.title,
        "count": len(result.comments),
        "comments": [
            c.model_dump(
                mode="json",
                include={
                    "id",
                    "author",
                    "title",
                    "content",
                    "rating",
                    "likes",
                    "created_at",
                    "version",
                },
                exclude_none=True,
            )
            for c in result.comments
        ],
    }


@server.tool()
async def analyze_comments(
    url: str,
    limit: int = 200,
    provider: str | None = None,
    model: str | None = None,
    language: str | None = None,
    output: Literal["markdown", "json"] = "markdown",
) -> str:
    """Fetch comments and generate a Voice-of-Customer report (pain points, feature requests,
    praise, suggestions) using echo's configured LLM provider.

    Args:
        url: App Store app URL, YouTube video URL or Bilibili video URL.
        limit: Maximum number of comments to analyze (1-500).
        provider: LLM provider preset, e.g. deepseek, openai, anthropic, qwen, ollama.
        model: Model name override.
        language: Report language, e.g. "Chinese" or "English".
        output: "markdown" for a readable report, "json" for structured data.
    """
    data = await service.fetch_comments(url, max(1, min(limit, 500)))
    report = await service.analyze(data, provider=provider, model=model, language=language)
    return render_markdown(report) if output == "markdown" else report.model_dump_json(indent=2)


# ---- local workspace (needs PostgreSQL: docker compose up -d && echo-voc db init) ----------


@server.tool()
async def track(url: str) -> dict:
    """Start tracking an account (all its works) or a single work in the local database.

    Args:
        url: Account URL (Bilibili space, YouTube channel, App Store developer) or a work URL.
    """
    from echo_voc import workspace

    account, work = await workspace.add(url)
    return {
        "account_id": account.id,
        "account": account.name,
        "platform": account.platform,
        "tracks_all_works": account.track_all,
        "work_id": work.id if work else None,
        "work": work.title if work else None,
    }


@server.tool()
async def list_accounts() -> list[dict]:
    """List tracked accounts with their ids."""
    from echo_voc import workspace

    return [
        {
            "id": a.id,
            "platform": a.platform,
            "name": a.name,
            "tracks_all_works": a.track_all,
            "works": a.works,
            "comments": a.feedback,
            "danmaku": a.danmaku,
            "last_synced_at": a.last_synced_at.isoformat() if a.last_synced_at else None,
        }
        for a in await workspace.accounts_overview()
    ]


@server.tool()
async def sync(
    account: str | None = None,
    work: str | None = None,
    since: str | None = None,
    danmaku: bool = False,
    max_per_work: int | None = 500,
) -> dict:
    """Incrementally fetch new comments (and optionally danmaku) into the local database.

    Args:
        account: Account id / name to sync (all accounts if both account and work are empty).
        work: Work id or external id (e.g. a BV id) to sync instead of an account.
        since: Only works/comments newer than this, e.g. "30d" or "2026-08-01".
        danmaku: Also sync Bilibili danmaku.
        max_per_work: Safety cap per work for interactive use; null for no cap.
    """
    from echo_voc import workspace
    from echo_voc.sync import SyncOptions
    from echo_voc.timeutil import parse_since

    options = SyncOptions(since=parse_since(since), danmaku=danmaku, max_per_work=max_per_work)
    stats = await workspace.sync(account=account, work=work, options=options)
    return stats.__dict__


@server.tool()
async def ask_feedback(question: str, account: str | None = None) -> str:
    """Answer a question from stored feedback, e.g. "最近1个月用户反馈较多的问题是什么" or
    "what do people say about battery life?". Rankings use labeled topic counts; lookups use
    semantic search. Run `echo-voc label` / `echo-voc embed` beforehand for complete results.

    Args:
        question: Natural-language question.
        account: Optional account id / name to scope the question.
    """
    from echo_voc import workspace
    from echo_voc.rag import render_answer

    return render_answer(await workspace.ask(question, account=account))


@server.tool()
async def list_topics(
    account: str | None = None,
    since: str | None = "30d",
    category: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Recurring feedback topics ranked by mentions in a time window.

    Args:
        account: Optional account id / name.
        since: Window start, e.g. "30d", "2026-08-01"; null for all time.
        category: pain_point | feature_request | praise | question.
        limit: Max topics.
    """
    from echo_voc import workspace
    from echo_voc.timeutil import parse_since

    rows = await workspace.topics(
        account=account, since=parse_since(since), category=category, limit=limit
    )
    return [r.model_dump(mode="json") for r in rows]


@server.tool()
async def get_trend(account: str, labels: list[str] | None = None, last: int = 3) -> str:
    """Compare saved snapshots of an account (sentiment shifts; new / rising / resolved topics).

    Args:
        account: Account id / name.
        labels: Snapshot labels in chronological order, e.g. ["2026-07", "2026-08"].
        last: When labels are omitted, compare the most recent N snapshots.
    """
    from echo_voc import workspace

    text, _ = await workspace.trend(account=account, labels=labels, last=last, remember=False)
    return text


@server.tool()
async def recall_memory(query: str, account: str | None = None, limit: int = 8) -> list[str]:
    """Search long-term product memory (period summaries and trend reviews stored in mem0).

    Args:
        query: What to recall, e.g. "history of crash complaints".
        account: Optional account id / name.
        limit: Max memories.
    """
    from echo_voc import workspace

    return await workspace.recall(query, account=account, limit=limit)


@server.resource("echo://providers")
def providers() -> str:
    """Built-in LLM provider presets."""
    return "\n".join(
        f"{p.name}: model={p.default_model or '-'} key={p.api_key_env or '-'}"
        for p in PROVIDERS.values()
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
