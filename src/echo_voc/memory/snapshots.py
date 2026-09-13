"""Structured memory: per-period feedback statistics and period-over-period comparison."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from echo_voc import prompts
from echo_voc.analyzer import DEFAULT_LANGUAGE
from echo_voc.llm.base import LLM
from echo_voc.store.models import Account, Feedback, FeedbackTopic, Snapshot, Topic, Work
from echo_voc.store.query import FeedbackFilter

TOP_TOPICS = 30
COMMENT_KINDS = ["comment", "reply", "review"]


class TopicStat(BaseModel):
    id: int
    category: str
    title: str
    mentions: int
    share: float  # mentions / labeled items


class SnapshotStats(BaseModel):
    total: int
    labeled: int
    sentiment: dict[str, int] = Field(default_factory=dict)
    average_rating: float | None = None
    topics: list[TopicStat] = Field(default_factory=list)

    def sentiment_share(self) -> dict[str, float]:
        n = sum(self.sentiment.values())
        return {k: (v / n if n else 0.0) for k, v in self.sentiment.items()}


async def compute_stats(session, flt: FeedbackFilter) -> SnapshotStats:
    joined = lambda q: flt.apply(q.join(Work, Work.id == Feedback.work_id))  # noqa: E731
    total, labeled, rating = (
        await session.execute(
            joined(
                select(
                    func.count(Feedback.id),
                    func.count(Feedback.labeled_at),
                    func.avg(Feedback.rating),
                ).select_from(Feedback)
            )
        )
    ).one()
    sentiment = dict(
        (
            await session.execute(
                joined(
                    select(Feedback.sentiment, func.count())
                    .select_from(Feedback)
                    .where(Feedback.sentiment.is_not(None))
                    .group_by(Feedback.sentiment)
                )
            )
        ).all()
    )
    n = func.count(func.distinct(Feedback.id))
    rows = (
        await session.execute(
            joined(
                select(Topic.id, Topic.category, Topic.title, n)
                .select_from(Feedback)
                .join(FeedbackTopic, FeedbackTopic.feedback_id == Feedback.id)
                .join(Topic, Topic.id == FeedbackTopic.topic_id)
                .group_by(Topic.id)
            )
            .order_by(n.desc())
            .limit(TOP_TOPICS)
        )
    ).all()
    return SnapshotStats(
        total=total,
        labeled=labeled,
        sentiment={k: sentiment.get(k, 0) for k in ("positive", "neutral", "negative")},
        average_rating=round(float(rating), 2) if rating is not None else None,
        topics=[
            TopicStat(
                id=i,
                category=c,
                title=t,
                mentions=m,
                share=round(m / labeled, 4) if labeled else 0.0,
            )
            for i, c, t, m in rows
        ],
    )


async def save_snapshot(
    session,
    account: Account,
    label: str,
    *,
    start: datetime | None,
    end: datetime | None,
    app_version: str | None = None,
    work_id: int | None = None,
    llm: LLM | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> Snapshot:
    flt = FeedbackFilter(
        account_ids=[account.id],
        since=start,
        until=end,
        kinds=COMMENT_KINDS,
        app_version=app_version,
        work_ids=[work_id] if work_id else None,
    )
    stats = await compute_stats(session, flt)
    if app_version and stats.total:  # version snapshots: record the real time span
        span = (
            await session.execute(
                flt.apply(
                    select(func.min(Feedback.created_at), func.max(Feedback.created_at))
                    .select_from(Feedback)
                    .join(Work, Work.id == Feedback.work_id)
                )
            )
        ).one()
        start, end = span
    summary = None
    if llm is not None and stats.labeled:
        summary = (
            await llm.complete(
                prompts.SNAPSHOT_SYSTEM.format(language=language),
                prompts.SNAPSHOT_USER.format(
                    name=account.name or account.external_id,
                    platform=account.platform,
                    label=label,
                    start=f"{start:%Y-%m-%d}" if start else "-",
                    end=f"{end:%Y-%m-%d}" if end else "-",
                    total=stats.total,
                    sentiment=stats.sentiment,
                    rating=stats.average_rating or "-",
                    topics="\n".join(
                        f"{t.category} | {t.title} | {t.mentions} | {t.share:.0%}"
                        for t in stats.topics[:15]
                    )
                    or "(no labeled topics)",
                ),
            )
        ).strip()
    values = dict(
        account_id=account.id,
        work_id=work_id,
        label=label,
        period_start=start,
        period_end=end,
        stats=stats.model_dump(),
        summary=summary,
    )
    stmt = insert(Snapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="snapshots_account_id_work_id_label_key",
        set_={
            **{k: stmt.excluded[k] for k in ("period_start", "period_end", "stats")},
            "summary": func.coalesce(stmt.excluded.summary, Snapshot.summary),
        },
    ).returning(Snapshot.id)
    snapshot_id = (await session.execute(stmt)).scalar_one()
    return await session.get(Snapshot, snapshot_id, populate_existing=True)


async def versions(session, account_id: int, min_count: int = 10) -> list[str]:
    """App versions with enough reviews to be worth a snapshot, oldest first."""
    rows = (
        await session.execute(
            select(Feedback.app_version, func.count(), func.min(Feedback.created_at))
            .join(Work, Work.id == Feedback.work_id)
            .where(Work.account_id == account_id, Feedback.app_version.is_not(None))
            .group_by(Feedback.app_version)
            .having(func.count() >= min_count)
            .order_by(func.min(Feedback.created_at))
        )
    ).all()
    return [v for v, _, _ in rows]


# ---- comparison -----------------------------------------------------------------------------


class TopicChange(BaseModel):
    id: int
    category: str
    title: str
    before: int
    after: int
    share_before: float
    share_after: float
    status: str  # new | gone | rising | falling | stable


class Comparison(BaseModel):
    before: str
    after: str
    total_before: int
    total_after: int
    sentiment_delta: dict[str, float]  # percentage points
    rating_before: float | None
    rating_after: float | None
    changes: list[TopicChange]


def compare(a: Snapshot, b: Snapshot, min_mentions: int = 2) -> Comparison:
    sa, sb = SnapshotStats.model_validate(a.stats), SnapshotStats.model_validate(b.stats)
    ta, tb = {t.id: t for t in sa.topics}, {t.id: t for t in sb.topics}
    changes = []
    for tid in ta.keys() | tb.keys():
        x, y = ta.get(tid), tb.get(tid)
        before, after = (x.mentions if x else 0), (y.mentions if y else 0)
        if max(before, after) < min_mentions:
            continue
        share_a, share_b = (x.share if x else 0.0), (y.share if y else 0.0)
        if not x:
            status = "new"
        elif not y:
            status = "gone"
        elif share_b - share_a >= 0.05 or (share_a and share_b / share_a >= 1.5):
            status = "rising"
        elif share_a - share_b >= 0.05 or (share_b and share_a / share_b >= 1.5):
            status = "falling"
        else:
            status = "stable"
        ref = y or x
        changes.append(
            TopicChange(
                id=tid,
                category=ref.category,
                title=ref.title,
                before=before,
                after=after,
                share_before=share_a,
                share_after=share_b,
                status=status,
            )
        )
    order = {"new": 0, "rising": 1, "falling": 2, "gone": 3, "stable": 4}
    changes.sort(key=lambda c: (order[c.status], -abs(c.share_after - c.share_before)))
    pa, pb = sa.sentiment_share(), sb.sentiment_share()
    return Comparison(
        before=a.label,
        after=b.label,
        total_before=sa.total,
        total_after=sb.total,
        sentiment_delta={k: round((pb.get(k, 0) - pa.get(k, 0)) * 100, 1) for k in pa | pb},
        rating_before=sa.average_rating,
        rating_after=sb.average_rating,
        changes=changes,
    )


def comparison_text(c: Comparison) -> str:
    lines = [
        f"{c.before} → {c.after}: items {c.total_before} → {c.total_after}; sentiment change "
        f"(pp) {c.sentiment_delta}; rating {c.rating_before} → {c.rating_after}"
    ]
    for ch in c.changes[:20]:
        lines.append(
            f"  [{ch.status}] {ch.category} | {ch.title}: {ch.before} ({ch.share_before:.0%})"
            f" → {ch.after} ({ch.share_after:.0%})"
        )
    return "\n".join(lines)


def render_trend(name: str, comparisons: list[Comparison], narrative: str | None) -> str:
    icons = {"new": "🆕", "rising": "📈", "falling": "📉", "gone": "✅", "stable": "➖"}
    lines = [f"# Feedback trend: {name}", ""]
    if narrative:
        lines += ["## Review", "", narrative, ""]
    for c in comparisons:
        d = c.sentiment_delta
        lines += [
            f"## {c.before} → {c.after}",
            "",
            f"- Items: {c.total_before} → {c.total_after}",
            f"- Sentiment: 👍 {d.get('positive', 0):+.1f}pp · 👎 {d.get('negative', 0):+.1f}pp",
        ]
        if c.rating_before is not None or c.rating_after is not None:
            lines.append(f"- Rating: {c.rating_before or '-'} → {c.rating_after or '-'}")
        lines += ["", "| | Topic | Category | Before | After |", "| --- | --- | --- | --- | --- |"]
        for ch in c.changes:
            lines.append(
                f"| {icons[ch.status]} {ch.status} | {ch.title} | {ch.category} | "
                f"{ch.before} ({ch.share_before:.0%}) | {ch.after} ({ch.share_after:.0%}) |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
