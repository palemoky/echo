"""Shared feedback filters used by export, report, RAG and snapshots."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from echo_voc.store import repo
from echo_voc.store.models import Feedback, Work


@dataclass
class FeedbackFilter:
    account_ids: Sequence[int] | None = None
    work_ids: Sequence[int] | None = None
    since: datetime | None = None
    until: datetime | None = None
    kinds: Sequence[str] | None = None
    platform: str | None = None
    sentiment: str | None = None
    app_version: str | None = None

    @classmethod
    async def build(
        cls, session: AsyncSession, *, account: str | None = None, work: str | None = None, **kw
    ) -> FeedbackFilter:
        flt = cls(**kw)
        if account:
            flt.account_ids = [(await repo.find_account(session, account)).id]
        if work:
            flt.work_ids = [(await repo.find_work(session, work)).id]
        return flt

    def apply(self, query: Select) -> Select:
        """Apply to a query that already joins Feedback and Work."""
        if self.account_ids:
            query = query.where(Work.account_id.in_(self.account_ids))
        if self.work_ids:
            query = query.where(Feedback.work_id.in_(self.work_ids))
        if self.since:
            query = query.where(Feedback.created_at >= self.since)
        if self.until:
            query = query.where(Feedback.created_at < self.until)
        if self.kinds:
            query = query.where(Feedback.kind.in_(self.kinds))
        if self.platform:
            query = query.where(Feedback.platform == self.platform)
        if self.sentiment:
            query = query.where(Feedback.sentiment == self.sentiment)
        if self.app_version:
            query = query.where(Feedback.app_version == self.app_version)
        return query


def feedback_query(flt: FeedbackFilter) -> Select:
    return flt.apply(select(Feedback, Work).join(Work, Work.id == Feedback.work_id))


async def iter_feedback(
    session: AsyncSession, flt: FeedbackFilter, *, order_newest: bool = True
) -> AsyncIterator[tuple[Feedback, Work]]:
    query = feedback_query(flt).order_by(
        Feedback.created_at.desc().nulls_last() if order_newest else Feedback.created_at
    )
    result = await session.stream(query.execution_options(yield_per=1000))
    async for fb, work in result:
        yield fb, work
