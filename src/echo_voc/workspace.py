"""High-level operations on the local workspace, shared by the CLI and the MCP server."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select

from echo_voc import prompts
from echo_voc.embeddings import create_embedder
from echo_voc.errors import EchoError
from echo_voc.llm import create_llm
from echo_voc.memory.snapshots import (
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


def language(value: str | None, fallback: str = "the language of the topics") -> str:
    return value or os.getenv("ECHO_LANGUAGE") or fallback


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


async def trend(
    *,
    account: str,
    labels: list[str] | None = None,
    last: int = 3,
    narrative: bool = True,
    remember: bool = True,
    provider: str | None = None,
    model: str | None = None,
    lang: str | None = None,
) -> tuple[str, list[str]]:
    """Compare snapshots; returns (markdown, warnings)."""
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
    return render_trend(name, comparisons, review), warnings


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
