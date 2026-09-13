"""High-level operations on the local workspace, shared by the CLI, web UI and MCP server."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select

from echo_voc import prompts
from echo_voc.embeddings import create_embedder
from echo_voc.errors import EchoError
from echo_voc.llm import create_llm
from echo_voc.memory.snapshots import (
    Comparison,
    compare,
    comparison_text,
    render_trend,
    save_snapshot,
    versions,
)
from echo_voc.rag import Asker, AskResult
from echo_voc.store import repo
from echo_voc.store.db import get_sessionmaker
from echo_voc.store.models import Feedback, FeedbackTopic, Snapshot, Topic, Work
from echo_voc.store.query import FeedbackFilter
from echo_voc.timeutil import parse_period, utcnow

log = logging.getLogger(__name__)

COMMENT_KINDS = ["comment", "reply", "review"]
Progress = Callable[[str], None] | None


def language(value: str | None, fallback: str = "the language of the topics") -> str:
    return value or os.getenv("ECHO_LANGUAGE") or fallback


# ---- tracking & sync --------------------------------------------------------------------------


async def add(url: str):
    """Track an account or a single work. Returns (Account, Work | None)."""
    from echo_voc.sync import Syncer

    syncer = Syncer(get_sessionmaker())
    try:
        return await syncer.add(url)
    finally:
        await syncer.aclose()


async def sync(
    *,
    account: str | None = None,
    work: str | None = None,
    options=None,
    on_progress: Progress = None,
):
    """Sync one work, one account, or everything. Returns SyncStats."""
    from echo_voc.sync import Syncer, SyncOptions, SyncStats

    options = options or SyncOptions()
    sm = get_sessionmaker()
    syncer = Syncer(sm, on_progress=on_progress)
    try:
        if work:
            async with sm() as session:
                work_id = (await repo.find_work(session, work)).id
            stats = SyncStats()
            await syncer.sync_work(work_id, options, stats)
            return stats
        if account:
            async with sm() as session:
                account_id = (await repo.find_account(session, account)).id
            return await syncer.sync_account(account_id, options)
        return await syncer.sync_all(options)
    finally:
        await syncer.aclose()


class AccountRow(BaseModel):
    id: int
    platform: str
    external_id: str
    name: str | None
    url: str | None
    track_all: bool
    works: int
    feedback: int
    danmaku: int
    labeled: int
    last_synced_at: datetime | None


async def accounts_overview() -> list[AccountRow]:
    from echo_voc.store.models import Account

    async with get_sessionmaker()() as session:
        q = (
            select(
                Account,
                func.count(func.distinct(Work.id)),
                func.count(Feedback.id).filter(Feedback.kind != "danmaku"),
                func.count(Feedback.id).filter(Feedback.kind == "danmaku"),
                func.count(Feedback.labeled_at),
            )
            .outerjoin(Work, Work.account_id == Account.id)
            .outerjoin(Feedback, Feedback.work_id == Work.id)
            .group_by(Account.id)
            .order_by(Account.id)
        )
        return [
            AccountRow(
                id=a.id,
                platform=a.platform,
                external_id=a.external_id,
                name=a.name,
                url=a.url,
                track_all=a.track_all,
                works=w,
                feedback=f,
                danmaku=d,
                labeled=lab,
                last_synced_at=a.last_synced_at,
            )
            for a, w, f, d, lab in (await session.execute(q)).all()
        ]


class WorkRow(BaseModel):
    id: int
    account_id: int
    platform: str
    external_id: str
    title: str | None
    url: str | None
    published_at: datetime | None
    comments: int
    danmaku: int
    partial: bool
    comments_synced_at: datetime | None
    danmaku_synced_at: datetime | None


async def works_overview(*, account: str | None = None, limit: int = 200) -> list[WorkRow]:
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
        return [
            WorkRow(
                id=w.id,
                account_id=w.account_id,
                platform=w.platform,
                external_id=w.external_id,
                title=w.title,
                url=w.url,
                published_at=w.published_at,
                comments=c,
                danmaku=d,
                partial=bool((w.meta or {}).get("partial")),
                comments_synced_at=w.comments_synced_at,
                danmaku_synced_at=w.danmaku_synced_at,
            )
            for w, c, d in (await session.execute(q)).all()
        ]


# ---- embeddings & labeling ---------------------------------------------------------------------


class LabelEstimate(BaseModel):
    count: int
    chars: int
    tokens: int


async def feedback_filter(
    session, *, account=None, work=None, since=None, kinds=None
) -> FeedbackFilter:
    return await FeedbackFilter.build(
        session, account=account, work=work, since=since, kinds=kinds or COMMENT_KINDS
    )


async def estimate_label(
    *, account=None, work=None, since=None, kinds=None, limit: int | None = None
) -> LabelEstimate:
    from echo_voc.label import Labeler, estimate_tokens

    async with get_sessionmaker()() as session:
        flt = await feedback_filter(session, account=account, work=work, since=since, kinds=kinds)
        query = Labeler.pending_query(flt)
        sub = (query.limit(limit) if limit else query).subquery()
        count, chars = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(func.length(sub.c.content)), 0))
            )
        ).one()
    return LabelEstimate(count=count, chars=chars, tokens=estimate_tokens(chars))


async def label(
    *,
    account=None,
    work=None,
    since=None,
    kinds=None,
    limit: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    lang: str | None = None,
    concurrency: int = 4,
    on_progress: Progress = None,
):
    """Label pending feedback. Returns LabelStats."""
    from echo_voc.label import Labeler

    sm = get_sessionmaker()
    async with sm() as session:
        flt = await feedback_filter(session, account=account, work=work, since=since, kinds=kinds)
    llm = create_llm(provider=provider, model=model)
    embedder = create_embedder()
    try:
        labeler = Labeler(
            sm,
            llm,
            embedder,
            language=language(lang, "the same language as the majority of the comments"),
            concurrency=concurrency,
            on_progress=on_progress,
        )
        return await labeler.run(flt, limit=limit)
    finally:
        await llm.aclose()
        await embedder.aclose()


async def embed(
    *,
    account=None,
    work=None,
    since=None,
    kinds=None,
    embedder_name: str | None = None,
    on_progress: Progress = None,
) -> int:
    from echo_voc.label import embed_pending

    sm = get_sessionmaker()
    async with sm() as session:
        flt = await feedback_filter(session, account=account, work=work, since=since, kinds=kinds)
    embedder = create_embedder(embedder_name)
    try:
        return await embed_pending(sm, embedder, flt, on_progress=on_progress)
    finally:
        await embedder.aclose()


# ---- questions & topics ------------------------------------------------------------------------


async def ask(
    question: str,
    *,
    account: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    memory: bool = True,
) -> AskResult:
    sm = get_sessionmaker()
    account_ids = None
    if account:
        async with sm() as session:
            account_ids = [(await repo.find_account(session, account)).id]
    llm = create_llm(provider=provider, model=model)
    embedder = create_embedder()
    recall = None
    if memory:
        from echo_voc.memory import recall_for_accounts

        recall = recall_for_accounts
    try:
        return await Asker(sm, llm, embedder, recall=recall).ask(question, account_ids=account_ids)
    finally:
        await llm.aclose()
        await embedder.aclose()


class TopicRow(BaseModel):
    id: int
    category: str
    title: str
    description: str
    mentions: int
    severity: int | None
    last_seen: datetime | None


async def topics(
    *,
    account: str | None = None,
    since: datetime | None = None,
    category: str | None = None,
    limit: int = 30,
) -> list[TopicRow]:
    async with get_sessionmaker()() as session:
        flt = await FeedbackFilter.build(session, account=account, since=since)
        n = func.count(func.distinct(Feedback.id))
        q = (
            flt.apply(
                select(Topic, n, func.max(Feedback.created_at))
                .join(FeedbackTopic, FeedbackTopic.topic_id == Topic.id)
                .join(Feedback, Feedback.id == FeedbackTopic.feedback_id)
                .join(Work, Work.id == Feedback.work_id)
                .group_by(Topic.id)
            )
            .order_by(n.desc())
            .limit(limit)
        )
        if category:
            q = q.where(Topic.category == category)
        return [
            TopicRow(
                id=t.id,
                category=t.category,
                title=t.title,
                description=t.description,
                mentions=m,
                severity=t.severity,
                last_seen=last,
            )
            for t, m, last in (await session.execute(q)).all()
        ]


def month_labels(start: datetime) -> list[str]:
    labels, cur, now = [], start.replace(day=1), utcnow()
    while cur <= now:
        labels.append(f"{cur:%Y-%m}")
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return labels


async def snapshots(
    *,
    account: str,
    periods: list[str] | None = None,
    monthly_since: datetime | None = None,
    by_version: bool = False,
    min_count: int = 10,
    summary: bool = True,
    remember: bool = True,
    provider: str | None = None,
    model: str | None = None,
    lang: str | None = None,
    on_progress=None,
) -> tuple[list[Snapshot], list[str]]:
    """Create/refresh snapshots. Returns (snapshots, warnings)."""
    progress = on_progress or (lambda _msg: None)
    llm = create_llm(provider=provider, model=model) if summary else None
    saved: list[Snapshot] = []
    warnings: list[str] = []
    try:
        async with get_sessionmaker()() as session:
            acc = await repo.find_account(session, account)
            jobs: list[tuple[str, dict]] = []
            for label in (periods or []) + (month_labels(monthly_since) if monthly_since else []):
                start, end = parse_period(label)
                jobs.append((label, {"start": start, "end": end}))
            if by_version:
                for v in await versions(session, acc.id, min_count):
                    jobs.append((f"v{v}", {"start": None, "end": None, "app_version": v}))
            if not jobs:
                raise EchoError("Nothing to snapshot: give periods, monthly_since or by_version")
            for label, kw in jobs:
                progress(f"Snapshot {label}")
                snap = await save_snapshot(
                    session, acc, label, llm=llm, language=language(lang), **kw
                )
                await session.commit()
                saved.append(snap)
            name, account_id = acc.name or acc.external_id, acc.id
    finally:
        if llm:
            await llm.aclose()
    if remember:
        warnings += await _remember(
            account_id,
            [
                (f"{name} {s.label}: {s.summary}", {"period": s.label, "type": "snapshot"})
                for s in saved
                if s.summary
            ],
        )
    return saved, warnings


class TrendData(BaseModel):
    name: str
    labels: list[str]
    snapshots: list[dict]  # label, period_start, period_end, stats, summary
    comparisons: list[Comparison]
    review: str | None = None
    warnings: list[str] = []


async def trend_data(
    *,
    account: str,
    labels: list[str] | None = None,
    last: int = 3,
    narrative: bool = True,
    remember: bool = True,
    provider: str | None = None,
    model: str | None = None,
    lang: str | None = None,
) -> TrendData:
    """Compare snapshots (chronological) and optionally write an LLM review."""
    async with get_sessionmaker()() as session:
        acc = await repo.find_account(session, account)
        q = select(Snapshot).where(Snapshot.account_id == acc.id, Snapshot.work_id.is_(None))
        if labels:
            found = {
                s.label: s
                for s in (await session.execute(q.where(Snapshot.label.in_(labels)))).scalars()
            }
            missing = [w for w in labels if w not in found]
            if missing:
                raise EchoError(f"Snapshots not found: {missing}")
            snaps = [found[w] for w in labels]
        else:
            snaps = list(
                (
                    await session.execute(
                        q.order_by(Snapshot.period_start.desc().nulls_last()).limit(last)
                    )
                ).scalars()
            )[::-1]
        name = acc.name or acc.external_id
    if len(snaps) < 2:
        raise EchoError("Need at least two snapshots (run `echo-voc snapshot` first)")
    comparisons = [compare(a, b) for a, b in zip(snaps, snaps[1:], strict=False)]
    review, warnings = None, []
    if narrative:
        llm = create_llm(provider=provider, model=model)
        try:
            review = (
                await llm.complete(
                    prompts.TREND_SYSTEM.format(language=language(lang)),
                    prompts.TREND_USER.format(
                        name=name, comparisons="\n\n".join(comparison_text(c) for c in comparisons)
                    ),
                )
            ).strip()
        finally:
            await llm.aclose()
        if remember and review:
            span = f"{snaps[0].label} → {snaps[-1].label}"
            warnings += await _remember(
                acc.id, [(f"{name} trend {span}: {review}", {"period": span, "type": "trend"})]
            )
    return TrendData(
        name=name,
        labels=[s.label for s in snaps],
        snapshots=[
            {
                "label": s.label,
                "period_start": s.period_start,
                "period_end": s.period_end,
                "stats": s.stats,
                "summary": s.summary,
            }
            for s in snaps
        ],
        comparisons=comparisons,
        review=review,
        warnings=warnings,
    )


async def trend(**kwargs) -> tuple[str, list[str]]:
    """Compare snapshots; returns (markdown, warnings)."""
    data = await trend_data(**kwargs)
    return render_trend(data.name, data.comparisons, data.review), data.warnings


async def recall(query: str, *, account: str | None = None, limit: int = 10) -> list[str]:
    from echo_voc.memory.mem0_store import get_memory

    memory = get_memory()
    if memory is None:
        raise EchoError("mem0 is disabled or not installed (uv sync --extra memory)")
    ids = []
    if account:
        async with get_sessionmaker()() as session:
            ids = [(await repo.find_account(session, account)).id]
    return await memory.recall(query, ids, limit=limit)


async def _remember(account_id: int, items: list[tuple[str, dict]]) -> list[str]:
    if not items:
        return []
    try:
        from echo_voc.memory.mem0_store import get_memory

        memory = get_memory()
        if memory is None:
            return []
        for text, meta in items:
            await memory.remember(account_id, text, meta)
    except Exception as e:  # data is saved in Postgres; memory is best-effort  # noqa: BLE001
        log.warning("mem0 write failed: %s", e)
        return [f"could not write mem0 memory: {e}"]
    return []
