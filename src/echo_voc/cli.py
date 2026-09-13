from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from echo_voc import __version__, service
from echo_voc.errors import EchoError
from echo_voc.llm import PROVIDERS
from echo_voc.models import FetchResult
from echo_voc.report import render_markdown
from echo_voc.sources import SOURCES

app = typer.Typer(
    help="echo — turn user comments into product insights.",
    no_args_is_help=True,
    add_completion=False,
)
err = Console(stderr=True)

Limit = Annotated[int, typer.Option("--limit", "-n", help="Max comments to fetch.")]
Platform = Annotated[
    Optional[str], typer.Option("--platform", "-p", help="Force a platform instead of auto-detect.")
]


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    version: Annotated[bool, typer.Option("--version", is_eager=True)] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    load_dotenv(Path.cwd() / ".env")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)


def _run(coro):
    try:
        return asyncio.run(coro)
    except EchoError as e:
        err.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None


@app.command()
def fetch(
    target: Annotated[str, typer.Argument(help="URL of an app / video.")],
    limit: Limit = 200,
    platform: Platform = None,
    output: Annotated[
        Optional[Path], typer.Option("--output", "-o", help="Write JSON here.")
    ] = None,
) -> None:
    """Fetch comments and save them as JSON (no LLM needed)."""
    with err.status("Fetching comments…"):
        result = _run(service.fetch_comments(target, limit, platform))
    text = result.model_dump_json(indent=2)
    if output:
        output.write_text(text, encoding="utf-8")
        err.print(f"[green]Saved {len(result.comments)} comments to {output}[/green]")
    else:
        typer.echo(text)


@app.command()
def analyze(
    target: Annotated[
        str, typer.Argument(help="URL of an app / video, or a JSON file produced by `fetch`.")
    ],
    limit: Limit = 200,
    platform: Platform = None,
    provider: Annotated[
        Optional[str], typer.Option(help="LLM provider (default: $ECHO_PROVIDER or deepseek).")
    ] = None,
    model: Annotated[
        Optional[str], typer.Option(help="Model name (default: provider preset).")
    ] = None,
    base_url: Annotated[Optional[str], typer.Option(help="Override API base URL.")] = None,
    language: Annotated[
        Optional[str], typer.Option("--lang", help="Report language, e.g. Chinese, English.")
    ] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="markdown | json")] = "markdown",
    output: Annotated[
        Optional[Path], typer.Option("--output", "-o", help="Write report here.")
    ] = None,
    concurrency: Annotated[int, typer.Option(help="Parallel LLM requests.")] = 4,
) -> None:
    """Fetch comments and generate an insight report with an LLM."""
    language = language or os.getenv("ECHO_LANGUAGE")

    async def go():
        path = Path(target)
        if path.suffix == ".json" and path.is_file():
            data = FetchResult.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            with err.status("Fetching comments…"):
                data = await service.fetch_comments(target, limit, platform)
        err.print(
            f"Fetched [bold]{len(data.comments)}[/bold] comments from {data.platform}"
            + (f": {data.title}" if data.title else "")
        )
        with err.status("Analyzing…") as status:
            return await service.analyze(
                data,
                provider=provider,
                model=model,
                language=language,
                base_url=base_url,
                concurrency=concurrency,
                on_progress=lambda msg: status.update(msg + "…"),
            )

    report = _run(go())
    if fmt == "json":
        text = report.model_dump_json(indent=2)
    elif fmt == "markdown":
        text = render_markdown(report)
    else:
        err.print(f"[red]Unknown format '{fmt}'[/red]")
        raise typer.Exit(2)

    if output:
        output.write_text(text, encoding="utf-8")
        err.print(f"[green]Report written to {output}[/green]")
    elif fmt == "markdown" and Console().is_terminal:
        Console().print(Markdown(text))
    else:
        typer.echo(text)


@app.command()
def sources() -> None:
    """List supported platforms."""
    table = Table("platform", "description", "example")
    for s in SOURCES.values():
        table.add_row(s.name, s.description, s.example)
    Console().print(table)


@app.command()
def providers() -> None:
    """List built-in LLM provider presets."""
    table = Table("provider", "default model", "API key env", "base URL")
    for p in PROVIDERS.values():
        table.add_row(
            p.name,
            p.default_model or "(set ECHO_MODEL)",
            p.api_key_env or "-",
            p.base_url or ("(set ECHO_BASE_URL)" if p.name == "custom" else "default"),
        )
    Console().print(table)


@app.command()
def mcp(
    transport: Annotated[str, typer.Option(help="stdio | streamable-http")] = "stdio",
) -> None:
    """Run as an MCP server (for Claude Desktop, Cursor, Cherry Studio, ...)."""
    from echo_voc.mcp_server import server

    server.run(transport=transport)


if __name__ == "__main__":
    app()
