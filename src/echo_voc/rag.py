"""Question answering over stored feedback: SQL aggregation for rankings, vectors for lookups."""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, select

from echo_voc import prompts
from echo_voc.analyzer import ask_json
from echo_voc.embeddings import Embedder
from echo_voc.errors import LLMError
from echo_voc.llm.base import LLM
from echo_voc.store.models import Account, Feedback, FeedbackTopic, Topic, Work
from echo_voc.store.query import FeedbackFilter
from echo_voc.timeutil import utcnow

DEFAULT_WINDOW = timedelta(days=30)
TOP_TOPICS = 10
EVIDENCE = 3
LOOKUP_K = 20


class QueryPlan(BaseModel):
    intent: Literal["ranking", "lookup"] = "lookup"
    since: datetime | None = None
    until: datetime | None = None
    account_ids: list[int] = Field(default_factory=list)
    category: Literal["pain_point", "feature_request", "praise", "question"] | None = None
    sentiment: Literal["positive", "neutral", "negative"] | None = None
    kinds: list[Literal["comment", "reply", "review", "danmaku"]] = Field(default_factory=list)
    search: str = ""

    @field_validator("since", "until", mode="after")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return v.replace(tzinfo=timezone.utc) if v and v.tzinfo is None else v


class Source(BaseModel):
    id: int
    content: str
    work_title: str | None = None
    url: str | None = None
    likes: int | None = None
    rating: float | None = None
    created_at: datetime | None = None
    kind: str


class TopicCount(BaseModel):
    id: int
    category: str
    title: str
    description: str
    mentions: int
    previous: int
    severity: int | None = None
    evidence: list[Source] = Field(default_factory=list)

    @property
    def trend(self) -> str:
        if self.previous == 0:
            return "new"
        change = (self.mentions - self.previous) / self.previous
        return f"{change:+.0%}"


class AskResult(BaseModel):
    question: str
    answer: str
    plan: QueryPlan
    window: tuple[datetime | None, datetime | None]
    topics: list[TopicCount] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    memories: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


MemoryRecall = Callable[[str, list[int]], Awaitable[list[str]]]


class Asker:
    def __init__(
        self,
        sessionmaker,
        llm: LLM,
        embedder: Embedder | None = None,
        recall: MemoryRecall | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.llm = llm
        self.embedder = embedder
        self.recall = recall

    async def ask(
        self,
        question: str,
        *,
        account_ids: list[int] | None = None,
        default_window: timedelta = DEFAULT_WINDOW,
    ) -> AskResult:
        async with self.sessionmaker() as session:
            plan = await self._plan(session, question)
            if account_ids:
                plan.account_ids = account_ids
            warnings: list[str] = []
            if plan.intent == "ranking" and plan.since is None:
                plan.since = (plan.until or utcnow()) - default_window
            flt = FeedbackFilter(
                account_ids=plan.account_ids or None,
                since=plan.since,
                until=plan.until,
                # Danmaku are only included when the question is about them.
                kinds=plan.kinds or ["comment", "reply", "review"],
                sentiment=plan.sentiment,
            )
            if plan.intent == "ranking":
                topics, total, unlabeled = await self._ranking(session, flt, plan)
                sources = [s for t in topics for s in t.evidence]
                context = self._ranking_context(plan, topics, total, unlabeled)
                if unlabeled:
                    warnings.append(
                        f"{unlabeled} of {total} items in this window are not labeled "
                        "yet; run `echo-voc label` for complete counts."
                    )
            else:
                topics = []
                sources, missing = await self._lookup(session, flt, plan.search or question)
                context = self._lookup_context(sources)
                if missing:
                    warnings.append(
                        f"{missing} matching items have no embedding yet; run "
                        "`echo-voc embed` to include them."
                    )

        memories = []
        if self.recall:
            try:
                memories = await self.recall(question, plan.account_ids)
            except Exception as e:  # memory is best-effort context  # noqa: BLE001
                warnings.append(f"memory recall failed: {e}")
        if memories:
            context += "\n\nHistorical notes from earlier analyses:\n" + "\n".join(
                f"- {m}" for m in memories
            )

        if not sources and not topics:
            answer = "No matching feedback found for this question in the stored data."
        else:
            answer = await self.llm.complete(
                prompts.ASK_ANSWER_SYSTEM,
                prompts.ASK_ANSWER_USER.format(question=question, context=context),
            )
        cited = {int(i) for i in re.findall(r"\[#(\d+)\]", answer)}
        known = {s.id for s in sources}
        if cited - known:
            warnings.append(f"Answer cited unknown items {sorted(cited - known)}; ignore those.")
        ordered = [s for s in sources if s.id in cited] + [s for s in sources if s.id not in cited]
        return AskResult(
            question=question,
            answer=answer.strip(),
            plan=plan,
            window=(plan.since, plan.until),
            topics=topics,
            sources=ordered,
            memories=memories,
            warnings=warnings,
        )

    # ---- planning ---------------------------------------------------------------------------

    async def _plan(self, session, question: str) -> QueryPlan:
        accounts = (await session.execute(select(Account.id, Account.name, Account.platform))).all()
        listing = ", ".join(f"#{i} {n or '?'} ({p})" for i, n, p in accounts) or "none"
        try:
            plan = await ask_json(
                self.llm,
                prompts.ASK_PLAN_SYSTEM.format(today=f"{utcnow():%Y-%m-%d}", accounts=listing),
                prompts.ASK_PLAN_USER.format(question=question),
                QueryPlan,
            )
        except LLMError:
            plan = QueryPlan(search=question)
        valid = {i for i, _, _ in accounts}
        plan.account_ids = [i for i in plan.account_ids if i in valid]
        return plan

    # ---- ranking ----------------------------------------------------------------------------

    async def _ranking(self, session, flt: FeedbackFilter, plan: QueryPlan):
        base = flt.apply(
            select(func.count(Feedback.id), func.count(Feedback.labeled_at)).join(
                Work, Work.id == Feedback.work_id
            )
        )
        total, labeled = (await session.execute(base)).one()

        def counts(window: FeedbackFilter):
            q = (
                select(Topic, func.count(func.distinct(Feedback.id)).label("n"))
                .join(FeedbackTopic, FeedbackTopic.topic_id == Topic.id)
                .join(Feedback, Feedback.id == FeedbackTopic.feedback_id)
                .join(Work, Work.id == Feedback.work_id)
                .group_by(Topic.id)
            )
            if plan.category:
                q = q.where(Topic.category == plan.category)
            return window.apply(q)

        rows = (
            await session.execute(
                counts(flt)
                .order_by(func.count(func.distinct(Feedback.id)).desc())
                .limit(TOP_TOPICS)
            )
        ).all()
        previous: dict[int, int] = {}
        if rows and flt.since:
            end = flt.until or utcnow()
            prev = FeedbackFilter(
                **{**flt.__dict__, "since": flt.since - (end - flt.since), "until": flt.since}
            )
            ids = [t.id for t, _ in rows]
            previous = dict(
                (
                    await session.execute(
                        prev.apply(
                            select(FeedbackTopic.topic_id, func.count(func.distinct(Feedback.id)))
                            .join(Feedback, Feedback.id == FeedbackTopic.feedback_id)
                            .join(Work, Work.id == Feedback.work_id)
                            .where(FeedbackTopic.topic_id.in_(ids))
                            .group_by(FeedbackTopic.topic_id)
                        )
                    )
                ).all()
            )

        topics = []
        for topic, n in rows:
            evidence = (
                await session.execute(
                    flt.apply(
                        select(Feedback, Work)
                        .join(Work, Work.id == Feedback.work_id)
                        .join(
                            FeedbackTopic,
                            and_(
                                FeedbackTopic.feedback_id == Feedback.id,
                                FeedbackTopic.topic_id == topic.id,
                            ),
                        )
                    )
                    .order_by(Feedback.likes.desc().nulls_last(), Feedback.created_at.desc())
                    .limit(EVIDENCE)
                )
            ).all()
            topics.append(
                TopicCount(
                    id=topic.id,
                    category=topic.category,
                    title=topic.title,
                    description=topic.description,
                    mentions=n,
                    previous=previous.get(topic.id, 0),
                    severity=topic.severity,
                    evidence=[_source(fb, w) for fb, w in evidence],
                )
            )
        return topics, total, total - labeled

    @staticmethod
    def _ranking_context(plan: QueryPlan, topics: list[TopicCount], total: int, unlabeled: int):
        window = f"{plan.since:%Y-%m-%d} to {(plan.until or utcnow()):%Y-%m-%d}"
        lines = [
            f"Window: {window}. Feedback items in window: {total} ({unlabeled} unlabeled).",
            "Topics ranked by mentions (vs the previous window of equal length):",
        ]
        for i, t in enumerate(topics, 1):
            sev = f", severity {t.severity}/5" if t.severity else ""
            lines.append(
                f"{i}. [{t.category}] {t.title} — {t.description} | mentions {t.mentions}"
                f" (previous {t.previous}, {t.trend}){sev}"
            )
            lines += [f"   [#{s.id}] {_clip(s.content)}" for s in t.evidence]
        return "\n".join(lines)

    # ---- lookup -----------------------------------------------------------------------------

    async def _lookup(self, session, flt: FeedbackFilter, text: str) -> tuple[list[Source], int]:
        if self.embedder is None:
            raise LLMError("Lookup questions need an embedder (Ollama bge-m3)")
        [vector] = await self.embedder.embed([text])
        distance = Feedback.embedding.cosine_distance(vector)
        rows = (
            await session.execute(
                flt.apply(
                    select(Feedback, Work, distance)
                    .join(Work, Work.id == Feedback.work_id)
                    .where(Feedback.embedding.is_not(None))
                )
                .order_by(distance)
                .limit(LOOKUP_K * 3)
            )
        ).all()
        missing = (
            await session.execute(
                flt.apply(
                    select(func.count(Feedback.id))
                    .join(Work, Work.id == Feedback.work_id)
                    .where(Feedback.embedding.is_(None))
                )
            )
        ).scalar()
        # Similarity first, with a gentle boost for comments many users agreed with.
        scored = sorted(
            rows, key=lambda r: (1 - r[2]) + 0.02 * math.log1p(r[0].likes or 0), reverse=True
        )
        return [_source(fb, w) for fb, w, _ in scored[:LOOKUP_K]], missing

    @staticmethod
    def _lookup_context(sources: list[Source]) -> str:
        lines = ["Most relevant feedback items (most relevant first):"]
        for s in sources:
            meta = [
                s.kind,
                f"{s.created_at:%Y-%m-%d}" if s.created_at else "",
                f"{s.likes} likes" if s.likes else "",
                f"{s.rating:g}★" if s.rating else "",
                s.work_title or "",
            ]
            lines.append(f"[#{s.id}] ({', '.join(m for m in meta if m)}) {_clip(s.content)}")
        return "\n".join(lines)


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _source(fb: Feedback, work: Work) -> Source:
    return Source(
        id=fb.id,
        content=fb.content,
        work_title=work.title,
        url=fb.url,
        likes=fb.likes,
        rating=fb.rating,
        created_at=fb.created_at,
        kind=fb.kind,
    )


def render_answer(result: AskResult) -> str:
    lines = [f"## {result.question}", "", result.answer, ""]
    if result.topics:
        lines += [
            "| # | Topic | Category | Mentions | vs previous |",
            "| --- | --- | --- | --- | --- |",
        ]
        for i, t in enumerate(result.topics, 1):
            lines.append(f"| {i} | {t.title} | {t.category} | {t.mentions} | {t.trend} |")
        lines.append("")
    cited = [s for s in result.sources if f"[#{s.id}]" in result.answer]
    if cited:
        lines += ["### Sources", ""]
        for s in cited:
            when = f" · {s.created_at:%Y-%m-%d}" if s.created_at else ""
            link = f" · [link]({s.url})" if s.url else ""
            lines.append(
                f"- **#{s.id}** {_clip(s.content, 200)} — {s.work_title or ''}{when}{link}"
            )
        lines.append("")
    for w in result.warnings:
        lines.append(f"> ⚠️ {w}")
    return "\n".join(lines).rstrip() + "\n"
