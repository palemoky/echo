"""Reports built from the local database instead of a live fetch."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select

from echo_voc.analyzer import DEFAULT_LANGUAGE, Analyzer
from echo_voc.analyzer_danmaku import DanmakuAnalyzer, DanmakuReport
from echo_voc.errors import EchoError
from echo_voc.llm import create_llm
from echo_voc.models import Comment, FetchResult, Report
from echo_voc.store import repo
from echo_voc.store.db import get_sessionmaker
from echo_voc.store.models import Account, Feedback, Work
from echo_voc.store.query import FeedbackFilter, feedback_query

COMMENT_KINDS = ["comment", "reply", "review"]


def to_comment(fb: Feedback) -> Comment:
    return Comment(
        id=str(fb.id),
        platform=fb.platform,
        kind=fb.kind,
        content=fb.content,
        title=fb.title,
        rating=fb.rating,
        likes=fb.likes,
        created_at=fb.created_at,
        version=fb.app_version,
        progress_ms=fb.progress_ms,
        url=fb.url,
    )


async def load_feedback(
    session, flt: FeedbackFilter, *, limit: int | None = None, order: str = "newest"
) -> list[Comment]:
    ordering = {
        "newest": Feedback.created_at.desc().nulls_last(),
        "likes": Feedback.likes.desc().nulls_last(),
    }[order]
    query = feedback_query(flt).order_by(ordering)
    if limit:
        query = query.limit(limit)
    return [to_comment(fb) for fb, _ in (await session.execute(query)).all()]


async def _scope_title(session, flt: FeedbackFilter, since: datetime | None) -> tuple[str, str]:
    if flt.work_ids:
        work = await session.get(Work, flt.work_ids[0])
        return work.platform, work.title or work.external_id
    if flt.account_ids:
        account = await session.get(Account, flt.account_ids[0])
        label = account.name or account.external_id
        return account.platform, label + (f" (since {since:%Y-%m-%d})" if since else "")
    return "multi", "All tracked feedback"


async def report_from_db(
    *,
    account: str | None = None,
    work: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    kinds: list[str] | None = None,
    limit: int = 500,
    order: str = "likes",
    provider: str | None = None,
    model: str | None = None,
    language: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Report:
    async with get_sessionmaker()() as session:
        flt = await FeedbackFilter.build(
            session,
            account=account,
            work=work,
            since=since,
            until=until,
            kinds=kinds or COMMENT_KINDS,
        )
        comments = await load_feedback(session, flt, limit=limit, order=order)
        platform, title = await _scope_title(session, flt, since)
    if not comments:
        raise EchoError("No stored feedback matches; run `echo-voc sync` first or widen --since")
    llm = create_llm(provider=provider, model=model)
    try:
        return await Analyzer(
            llm, language=language or DEFAULT_LANGUAGE, on_progress=on_progress
        ).analyze(FetchResult(platform=platform, target=title, title=title, comments=comments))
    finally:
        await llm.aclose()


async def danmaku_report_from_db(
    *,
    work: str,
    since: datetime | None = None,
    bucket_s: int = 30,
    overall: bool = True,
    provider: str | None = None,
    model: str | None = None,
    language: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> DanmakuReport:
    async with get_sessionmaker()() as session:
        work_row = await repo.find_work(session, work)
        flt = FeedbackFilter(work_ids=[work_row.id], since=since, kinds=["danmaku"])
        danmaku = await load_feedback(session, flt)
        if not danmaku:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Feedback)
                    .where(Feedback.work_id == work_row.id)
                )
            ).scalar()
            raise EchoError(
                "No stored danmaku for this work; run `echo-voc sync --work "
                f"{work_row.id} --danmaku` first"
                + (f" ({count} other feedback rows)" if count else "")
            )
    llm = create_llm(provider=provider, model=model)
    try:
        return await DanmakuAnalyzer(
            llm,
            language=language or DEFAULT_LANGUAGE,
            bucket_s=bucket_s,
            overall=overall,
            on_progress=on_progress,
        ).analyze(
            danmaku,
            title=work_row.title,
            url=work_row.url,
            duration_s=(work_row.meta or {}).get("duration"),
        )
    finally:
        await llm.aclose()
