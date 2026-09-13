"""CLI commands backed by the local database (sync, RAG, memory)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from echo_voc.timeutil import parse_since

console = Console()
err = Console(stderr=True)

AccountRef = Annotated[
    Optional[str], typer.Option("--account", "-a", help="Account id, external id or name.")
]
WorkRef = Annotated[Optional[str], typer.Option("--work", "-w", help="Work id or external id.")]
Since = Annotated[Optional[str], typer.Option(help="Time window, e.g. 30d, 12h, 2026-08-01.")]


def _since(value: str | None):
    try:
        return parse_since(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None


def register(app: typer.Typer, run) -> None:
    db_app = typer.Typer(help="Database management.", no_args_is_help=True)
    app.add_typer(db_app, name="db")

    @db_app.command("init")
    def db_init() -> None:
        """Create or upgrade the database schema (run `docker compose up -d` first)."""
        from echo_voc.errors import EchoError
        from echo_voc.store.db import database_url, migrate

        try:
            migrate()
        except EchoError as e:
            err.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1) from None
        err.print(f"[green]Database ready:[/green] {database_url().split('@')[-1]}")

    @app.command()
    def add(url: Annotated[str, typer.Argument(help="Account URL or single work URL.")]) -> None:
        """Track an account (all its works) or a single work."""
        from echo_voc.store.db import get_sessionmaker
        from echo_voc.sync import Syncer

        async def go():
            syncer = Syncer(get_sessionmaker())
            try:
                return await syncer.add(url)
            finally:
                await syncer.aclose()

        account, work = run(go())
        if work is None:
            err.print(
                f"[green]Tracking account[/green] #{account.id} {account.name or ''} "
                f"({account.platform}:{account.external_id})"
            )
        else:
            err.print(
                f"[green]Tracking work[/green] #{work.id} {work.title} "
                f"(account #{account.id} {account.name or ''})"
            )
        err.print(
            "Next: echo-voc sync"
            + (f" --account {account.id}" if work is None else f" --work {work.id}")
        )

    @app.command()
    def accounts() -> None:
        """List tracked accounts."""
        from sqlalchemy import func, select

        from echo_voc.store.db import get_sessionmaker
        from echo_voc.store.models import Account, Feedback, Work

        async def go():
            async with get_sessionmaker()() as session:
                q = (
                    select(Account, func.count(func.distinct(Work.id)), func.count(Feedback.id))
                    .outerjoin(Work, Work.account_id == Account.id)
                    .outerjoin(Feedback, Feedback.work_id == Work.id)
                    .group_by(Account.id)
                    .order_by(Account.id)
                )
                return (await session.execute(q)).all()

        table = Table("id", "platform", "name", "scope", "works", "feedback", "last sync")
        for account, works, feedback in run(go()):
            table.add_row(
                str(account.id),
                account.platform,
                account.name or account.external_id,
                "all works" if account.track_all else "selected",
                str(works),
                str(feedback),
                f"{account.last_synced_at:%Y-%m-%d %H:%M}" if account.last_synced_at else "-",
            )
        console.print(table)

    @app.command()
    def works(account: AccountRef = None, limit: int = 50) -> None:
        """List tracked works with comment / danmaku counts."""
        from sqlalchemy import func, select

        from echo_voc.store import repo
        from echo_voc.store.db import get_sessionmaker
        from echo_voc.store.models import Feedback, Work

        async def go():
            async with get_sessionmaker()() as session:
                q = (
                    select(
                        Work,
                        func.count(Feedback.id).filter(Feedback.kind != "danmaku"),
                        func.count(Feedback.id).filter(Feedback.kind == "danmaku"),
                    )
                    .outerjoin(Feedback, Feedback.work_id == Work.id)
                    .group_by(Work.id)
                    .order_by(Work.published_at.desc().nulls_last())
                    .limit(limit)
                )
                if account:
                    q = q.where(Work.account_id == (await repo.find_account(session, account)).id)
                return (await session.execute(q)).all()

        table = Table("id", "published", "title", "comments", "danmaku", "synced")
        for work, comments, danmaku in run(go()):
            table.add_row(
                str(work.id),
                f"{work.published_at:%Y-%m-%d}" if work.published_at else "-",
                (work.title or work.external_id)[:48]
                + (" (partial)" if (work.meta or {}).get("partial") else ""),
                str(comments),
                str(danmaku),
                f"{work.comments_synced_at:%m-%d %H:%M}" if work.comments_synced_at else "-",
            )
        console.print(table)

    @app.command()
    def sync(
        account: AccountRef = None,
        work: WorkRef = None,
        since: Since = None,
        danmaku: Annotated[bool, typer.Option(help="Also sync danmaku (Bilibili).")] = False,
        comments: Annotated[bool, typer.Option(help="Sync comments / reviews.")] = True,
        replies: Annotated[bool, typer.Option(help="Also fetch nested replies.")] = False,
        full: Annotated[
            bool, typer.Option(help="Ignore watermarks and re-walk everything.")
        ] = False,
        max_per_work: Annotated[
            Optional[int], typer.Option(help="Cap comments per work (sampling; marks partial).")
        ] = None,
    ) -> None:
        """Incrementally sync tracked accounts / works into the local database."""
        from echo_voc.store import repo
        from echo_voc.store.db import get_sessionmaker
        from echo_voc.sync import Syncer, SyncOptions, SyncStats

        options = SyncOptions(
            since=_since(since),
            replies=replies,
            danmaku=danmaku,
            comments=comments,
            full=full,
            max_per_work=max_per_work,
        )

        async def go():
            sessionmaker = get_sessionmaker()
            with err.status("Syncing…") as status:
                syncer = Syncer(sessionmaker, on_progress=lambda m: status.update(m))
                try:
                    if work:
                        async with sessionmaker() as session:
                            work_id = (await repo.find_work(session, work)).id
                        stats = SyncStats()
                        await syncer.sync_work(work_id, options, stats)
                        return stats
                    if account:
                        async with sessionmaker() as session:
                            account_id = (await repo.find_account(session, account)).id
                        return await syncer.sync_account(account_id, options)
                    return await syncer.sync_all(options)
                finally:
                    await syncer.aclose()

        stats = run(go())
        err.print(
            f"[green]Done.[/green] works synced {stats.works_synced} (new {stats.works_new}), "
            f"new comments {stats.comments_new}, new danmaku {stats.danmaku_new}"
        )
        for e in stats.errors:
            err.print(f"[yellow]warning:[/yellow] {e}")

    @app.command()
    def export(
        output: Annotated[Path, typer.Option("--output", "-o", help="JSONL file to write.")],
        account: AccountRef = None,
        work: WorkRef = None,
        since: Since = None,
        kind: Annotated[
            Optional[list[str]], typer.Option(help="comment / reply / review / danmaku")
        ] = None,
    ) -> None:
        """Export stored feedback as JSON Lines for offline analysis."""
        from echo_voc.store.db import get_sessionmaker
        from echo_voc.store.query import FeedbackFilter, iter_feedback

        async def go():
            count = 0
            async with get_sessionmaker()() as session:
                flt = await FeedbackFilter.build(
                    session, account=account, work=work, since=_since(since), kinds=kind
                )
                with output.open("w", encoding="utf-8") as fh:
                    async for fb, w in iter_feedback(session, flt):
                        fh.write(
                            json.dumps(
                                {
                                    "id": fb.id,
                                    "platform": fb.platform,
                                    "kind": fb.kind,
                                    "work": w.external_id,
                                    "work_title": w.title,
                                    "content": fb.content,
                                    "title": fb.title,
                                    "rating": fb.rating,
                                    "likes": fb.likes,
                                    "app_version": fb.app_version,
                                    "progress_ms": fb.progress_ms,
                                    "author": fb.author_hash,
                                    "sentiment": fb.sentiment,
                                    "created_at": fb.created_at.isoformat()
                                    if fb.created_at
                                    else None,
                                    "url": fb.url,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        count += 1
            return count

        err.print(f"[green]Exported {run(go())} rows to {output}[/green]")

    @app.command()
    def report(
        account: AccountRef = None,
        work: WorkRef = None,
        since: Since = None,
        until: Annotated[Optional[str], typer.Option(help="End of window (exclusive).")] = None,
        danmaku: Annotated[
            bool, typer.Option(help="Danmaku timeline report (needs --work).")
        ] = False,
        kind: Annotated[Optional[list[str]], typer.Option(help="comment / reply / review")] = None,
        limit: Annotated[int, typer.Option(help="Max feedback items to analyze.")] = 500,
        order: Annotated[str, typer.Option(help="Pick items by: likes | newest")] = "likes",
        bucket: Annotated[int, typer.Option(help="Danmaku bucket size in seconds.")] = 30,
        provider: Annotated[Optional[str], typer.Option()] = None,
        model: Annotated[Optional[str], typer.Option()] = None,
        language: Annotated[Optional[str], typer.Option("--lang")] = None,
        fmt: Annotated[str, typer.Option("--format", "-f", help="markdown | json")] = "markdown",
        output: Annotated[Optional[Path], typer.Option("--output", "-o")] = None,
    ) -> None:
        """Generate a VOC report from stored feedback (no re-fetching)."""
        import os

        from echo_voc import offline
        from echo_voc.report import render_danmaku_markdown, render_markdown

        language = language or os.getenv("ECHO_LANGUAGE")

        async def go():
            with err.status("Analyzing…") as status:
                progress = lambda m: status.update(m + "…")  # noqa: E731
                if danmaku:
                    if not work:
                        raise typer.BadParameter("--danmaku needs --work")
                    return await offline.danmaku_report_from_db(
                        work=work,
                        since=_since(since),
                        bucket_s=bucket,
                        provider=provider,
                        model=model,
                        language=language,
                        on_progress=progress,
                    )
                return await offline.report_from_db(
                    account=account,
                    work=work,
                    since=_since(since),
                    until=_since(until),
                    kinds=kind,
                    limit=limit,
                    order=order,
                    provider=provider,
                    model=model,
                    language=language,
                    on_progress=progress,
                )

        result = run(go())
        if fmt == "json":
            text = result.model_dump_json(indent=2)
        else:
            text = render_danmaku_markdown(result) if danmaku else render_markdown(result)
        _emit(text, output, markdown=fmt != "json")

    @app.command()
    def embed(
        account: AccountRef = None,
        work: WorkRef = None,
        since: Since = None,
        kind: Annotated[
            Optional[list[str]], typer.Option(help="Kinds to embed (default: all but danmaku).")
        ] = None,
        embedder: Annotated[Optional[str], typer.Option(help="ollama | openai")] = None,
    ) -> None:
        """Compute embeddings (default: Ollama bge-m3) for stored feedback."""
        from echo_voc.embeddings import create_embedder
        from echo_voc.label import embed_pending
        from echo_voc.offline import COMMENT_KINDS
        from echo_voc.store.db import get_sessionmaker
        from echo_voc.store.query import FeedbackFilter

        async def go():
            sm = get_sessionmaker()
            async with sm() as session:
                flt = await FeedbackFilter.build(
                    session,
                    account=account,
                    work=work,
                    since=_since(since),
                    kinds=kind or COMMENT_KINDS,
                )
            emb = create_embedder(embedder)
            try:
                with err.status("Embedding…") as status:
                    return await embed_pending(sm, emb, flt, on_progress=status.update)
            finally:
                await emb.aclose()

        err.print(f"[green]Embedded {run(go())} items[/green]")

    @app.command()
    def label(
        account: AccountRef = None,
        work: WorkRef = None,
        since: Since = None,
        kind: Annotated[
            Optional[list[str]], typer.Option(help="Kinds to label (default: all but danmaku).")
        ] = None,
        limit: Annotated[Optional[int], typer.Option(help="Max items to label this run.")] = None,
        provider: Annotated[Optional[str], typer.Option()] = None,
        model: Annotated[Optional[str], typer.Option()] = None,
        language: Annotated[Optional[str], typer.Option("--lang")] = None,
        concurrency: int = 4,
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Skip the cost confirmation.")
        ] = False,
    ) -> None:
        """Label sentiment and assign topics for unlabeled feedback (incremental)."""
        import os

        from sqlalchemy import func, select

        from echo_voc.embeddings import create_embedder
        from echo_voc.label import Labeler, estimate_tokens
        from echo_voc.llm import create_llm
        from echo_voc.offline import COMMENT_KINDS
        from echo_voc.store.db import get_sessionmaker

        sm = get_sessionmaker()

        async def pending():
            from echo_voc.store.query import FeedbackFilter

            async with sm() as session:
                flt = await FeedbackFilter.build(
                    session,
                    account=account,
                    work=work,
                    since=_since(since),
                    kinds=kind or COMMENT_KINDS,
                )
                sub = (
                    Labeler.pending_query(flt).limit(limit).subquery()
                    if limit
                    else Labeler.pending_query(flt).subquery()
                )
                count, chars = (
                    await session.execute(
                        select(func.count(), func.coalesce(func.sum(func.length(sub.c.content)), 0))
                    )
                ).one()
            return flt, count, chars

        flt, count, chars = run(pending())
        if not count:
            err.print("Nothing to label.")
            return
        err.print(
            f"{count} items to label, ~{estimate_tokens(chars):,} tokens (map step, excluding "
            "topic matching)."
        )
        if not yes and not typer.confirm("Continue?", default=True):
            raise typer.Exit()

        async def go():
            llm = create_llm(provider=provider, model=model)
            emb = create_embedder()
            try:
                with err.status("Labeling…") as status:
                    labeler = Labeler(
                        sm,
                        llm,
                        emb,
                        language=language
                        or os.getenv("ECHO_LANGUAGE")
                        or "the same language as the majority of the comments",
                        concurrency=concurrency,
                        on_progress=status.update,
                    )
                    return await labeler.run(flt, limit=limit)
            finally:
                await llm.aclose()
                await emb.aclose()

        stats = run(go())
        err.print(
            f"[green]Labeled {stats.labeled}[/green] (skipped {stats.skipped}); topics: "
            f"{stats.topics_new} new, {stats.topics_matched} matched existing"
        )

    topics_app = typer.Typer(help="Browse the per-account topic catalog.", no_args_is_help=True)
    app.add_typer(topics_app, name="topics")

    @topics_app.command("list")
    def topics_list(
        account: AccountRef = None,
        since: Since = None,
        category: Annotated[Optional[str], typer.Option()] = None,
        limit: int = 30,
    ) -> None:
        """Topics ranked by mentions within a time window."""
        from echo_voc import workspace

        rows = run(
            workspace.topics(account=account, since=_since(since), category=category, limit=limit)
        )
        table = Table("id", "category", "title", "mentions", "severity", "last seen")
        for t in rows:
            table.add_row(
                str(t.id),
                t.category,
                t.title,
                str(t.mentions),
                str(t.severity or "-"),
                f"{t.last_seen:%Y-%m-%d}" if t.last_seen else "-",
            )
        console.print(table)

    @app.command()
    def ask(
        question: Annotated[str, typer.Argument(help="e.g. 最近1个月用户反馈较多的问题是什么")],
        account: AccountRef = None,
        provider: Annotated[Optional[str], typer.Option()] = None,
        model: Annotated[Optional[str], typer.Option()] = None,
        memory: Annotated[bool, typer.Option(help="Include mem0 memories as context.")] = True,
        fmt: Annotated[str, typer.Option("--format", "-f", help="markdown | json")] = "markdown",
        output: Annotated[Optional[Path], typer.Option("--output", "-o")] = None,
    ) -> None:
        """Ask a question about stored feedback (RAG over your database)."""
        from echo_voc import workspace
        from echo_voc.rag import render_answer

        async def go():
            with err.status("Thinking…"):
                return await workspace.ask(
                    question, account=account, provider=provider, model=model, memory=memory
                )

        result = run(go())
        if fmt == "json":
            _emit(result.model_dump_json(indent=2), output, markdown=False)
        else:
            _emit(render_answer(result), output, markdown=True)

    @app.command()
    def snapshot(
        account: Annotated[str, typer.Option("--account", "-a", help="Account id / name.")],
        period: Annotated[
            Optional[list[str]], typer.Option(help="2026-08, 2026, or 2026-08-01..2026-08-15")
        ] = None,
        monthly: Annotated[
            Optional[str], typer.Option(help="One snapshot per month since, e.g. 6m / 2026-01-01")
        ] = None,
        by_version: Annotated[bool, typer.Option(help="One snapshot per app version.")] = False,
        min_count: Annotated[int, typer.Option(help="Min reviews per version.")] = 10,
        summary: Annotated[bool, typer.Option(help="LLM-written period summary.")] = True,
        remember: Annotated[bool, typer.Option(help="Store the summary in mem0.")] = True,
        provider: Annotated[Optional[str], typer.Option()] = None,
        model: Annotated[Optional[str], typer.Option()] = None,
        language: Annotated[Optional[str], typer.Option("--lang")] = None,
    ) -> None:
        """Save period / version snapshots of feedback stats (structured product memory)."""
        from echo_voc import workspace

        if not (period or monthly or by_version):
            raise typer.BadParameter("Give --period, --monthly or --by-version")

        async def go():
            with err.status("Snapshotting…") as status:
                return await workspace.snapshots(
                    account=account,
                    periods=period,
                    monthly_since=_since(monthly),
                    by_version=by_version,
                    min_count=min_count,
                    summary=summary,
                    remember=remember,
                    provider=provider,
                    model=model,
                    lang=language,
                    on_progress=status.update,
                )

        saved, warnings = run(go())
        table = Table("label", "from", "to", "items", "labeled", "👍", "👎", "top topic")
        for snap in saved:
            st = snap.stats
            sent = st.get("sentiment", {})
            n = sum(sent.values()) or 1
            top = st["topics"][0]["title"] if st.get("topics") else "-"
            table.add_row(
                snap.label,
                f"{snap.period_start:%Y-%m-%d}" if snap.period_start else "-",
                f"{snap.period_end:%Y-%m-%d}" if snap.period_end else "-",
                str(st["total"]),
                str(st["labeled"]),
                f"{sent.get('positive', 0) / n:.0%}",
                f"{sent.get('negative', 0) / n:.0%}",
                top,
            )
            if snap.summary:
                err.print(f"[bold]{snap.label}[/bold]: {snap.summary}")
        console.print(table)
        for w in warnings:
            err.print(f"[yellow]warning:[/yellow] {w}")

    @app.command()
    def trend(
        account: Annotated[str, typer.Option("--account", "-a", help="Account id / name.")],
        labels: Annotated[
            Optional[str], typer.Option(help="Comma-separated snapshot labels, in order.")
        ] = None,
        last: Annotated[int, typer.Option(help="Compare the last N snapshots.")] = 3,
        narrative: Annotated[bool, typer.Option(help="LLM-written trend review.")] = True,
        remember: Annotated[bool, typer.Option(help="Store the review in mem0.")] = True,
        provider: Annotated[Optional[str], typer.Option()] = None,
        model: Annotated[Optional[str], typer.Option()] = None,
        language: Annotated[Optional[str], typer.Option("--lang")] = None,
        output: Annotated[Optional[Path], typer.Option("--output", "-o")] = None,
    ) -> None:
        """Compare snapshots: sentiment shifts and new / rising / resolved topics."""
        from echo_voc import workspace

        wanted = [x.strip() for x in labels.split(",") if x.strip()] if labels else None
        text, warnings = run(
            workspace.trend(
                account=account,
                labels=wanted,
                last=last,
                narrative=narrative,
                remember=remember,
                provider=provider,
                model=model,
                lang=language,
            )
        )
        _emit(text, output, markdown=True)
        for w in warnings:
            err.print(f"[yellow]warning:[/yellow] {w}")

    @app.command()
    def recall(
        query: Annotated[str, typer.Argument(help="e.g. 发热问题的历史")],
        account: AccountRef = None,
        limit: int = 10,
    ) -> None:
        """Search product memory (mem0) built from snapshots and trend reviews."""
        from echo_voc import workspace

        memories = run(workspace.recall(query, account=account, limit=limit))
        if not memories:
            err.print("No memories yet. Create some with `echo-voc snapshot` / `echo-voc trend`.")
        for m in memories:
            console.print(f"• {m}")


def _emit(text: str, output: Path | None, *, markdown: bool) -> None:
    from rich.markdown import Markdown

    if output:
        output.write_text(text, encoding="utf-8")
        err.print(f"[green]Written to {output}[/green]")
    elif markdown and console.is_terminal:
        console.print(Markdown(text))
    else:
        typer.echo(text)
