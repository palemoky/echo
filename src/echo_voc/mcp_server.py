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
        "`analyze_comments` to get a full insight report produced by echo's configured LLM."
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
