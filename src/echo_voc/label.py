"""Incremental labeling: sentiment per feedback item + assignment to a per-account topic catalog."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from echo_voc import prompts
from echo_voc.analyzer import DEFAULT_LANGUAGE, BatchExtraction, ask_json, extract_all
from echo_voc.embeddings import Embedder
from echo_voc.errors import LLMError
from echo_voc.llm.base import LLM
from echo_voc.models import Comment
from echo_voc.offline import to_comment
from echo_voc.store import repo
from echo_voc.store.models import Feedback, FeedbackTopic, Topic, Work
from echo_voc.store.query import FeedbackFilter

log = logging.getLogger(__name__)

AUTO_MATCH = 0.85  # bge-m3 cosine at/above which drafts merge without asking the LLM
CANDIDATE_MIN = 0.65  # below this a catalog topic is not even offered as a candidate
CANDIDATES = 4
CHUNK = 400  # feedback rows processed per round
MATCH_BATCH = 25


@dataclass
class LabelStats:
    labeled: int = 0
    skipped: int = 0
    topics_new: int = 0
    topics_matched: int = 0
    accounts: set[int] = field(default_factory=set)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def topic_text(category: str, title: str, description: str) -> str:
    return f"{category}: {title} — {description}"


def estimate_tokens(chars: int) -> int:
    """Rough prompt+completion estimate for the map step (CJK ≈ 1 token/char, plus output)."""
    return int(chars * 1.1) + chars // 2


class _Match(BaseModel):
    draft: int
    topic: int | None = None
    same_as_draft: int | None = None


class _MatchReply(BaseModel):
    matches: list[_Match] = Field(default_factory=list)


@dataclass
class _Draft:
    category: str
    title: str
    description: str
    severity: int | None
    refs: list[Comment]
    vector: list[float] | None = None
    topic_id: int | None = None


class Labeler:
    def __init__(
        self,
        sessionmaker,
        llm: LLM,
        embedder: Embedder,
        language: str = DEFAULT_LANGUAGE,
        concurrency: int = 4,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.llm = llm
        self.embedder = embedder
        self.language = language
        self.concurrency = concurrency
        self.progress = on_progress or (lambda _msg: None)

    @staticmethod
    def pending_query(flt: FeedbackFilter):
        return flt.apply(
            select(Feedback, Work.account_id)
            .join(Work, Work.id == Feedback.work_id)
            .where(Feedback.labeled_at.is_(None))
        )

    async def run(self, flt: FeedbackFilter, limit: int | None = None) -> LabelStats:
        stats = LabelStats()
        remaining = limit
        while remaining is None or remaining > 0:
            size = CHUNK if remaining is None else min(CHUNK, remaining)
            async with self.sessionmaker() as session:
                rows = (
                    await session.execute(
                        self.pending_query(flt)
                        .order_by(Feedback.created_at.desc().nulls_last())
                        .limit(size)
                    )
                ).all()
            if not rows:
                break
            by_account: dict[int, list[Feedback]] = defaultdict(list)
            for fb, account_id in rows:
                by_account[account_id].append(fb)
            progressed = 0
            for account_id, items in by_account.items():
                progressed += await self._label_account(account_id, items, stats)
            if remaining is not None:
                remaining -= len(rows)
            if progressed == 0:  # every batch failed; stop instead of looping forever
                raise LLMError("Labeling made no progress; check the LLM provider and key")
        return stats

    async def _label_account(
        self, account_id: int, items: list[Feedback], stats: LabelStats
    ) -> int:
        stats.accounts.add(account_id)
        comments = [to_comment(fb) for fb in items]
        self.progress(f"Labeling {len(comments)} items (account #{account_id})")
        extractions = await extract_all(
            self.llm, comments, self.language, self.concurrency, self.progress
        )
        drafts = [
            _Draft(t.category.value, t.title, t.description, t.severity, refs)
            for ex in extractions
            for t, refs in ex.drafts
        ]
        if drafts:
            vectors = await self.embedder.embed(
                [topic_text(d.category, d.title, d.description) for d in drafts]
            )
            for d, v in zip(drafts, vectors, strict=True):
                d.vector = v

        async with self.sessionmaker() as session:
            for start in range(0, len(drafts), MATCH_BATCH):
                await self._assign(session, account_id, drafts[start : start + MATCH_BATCH], stats)
            labeled = await self._write_labels(session, extractions, drafts)
            await session.commit()
        stats.labeled += labeled
        stats.skipped += len(items) - labeled
        return labeled

    async def _assign(self, session, account_id: int, drafts: list[_Draft], stats: LabelStats):
        candidates: list[list[tuple[Topic, float]]] = []
        for d in drafts:
            distance = Topic.embedding.cosine_distance(d.vector)
            found = (
                await session.execute(
                    select(Topic, distance)
                    .where(Topic.account_id == account_id, Topic.category == d.category)
                    .order_by(distance)
                    .limit(CANDIDATES)
                )
            ).all()
            candidates.append([(t, 1 - dist) for t, dist in found if 1 - dist >= CANDIDATE_MIN])

        undecided = []
        same_as: dict[int, int] = {}
        for i, (d, cands) in enumerate(zip(drafts, candidates, strict=True)):
            if cands and cands[0][1] >= AUTO_MATCH:
                d.topic_id = cands[0][0].id
                continue
            # Near-identical drafts from different batches in this chunk become one topic.
            twin = next(
                (
                    j
                    for j in undecided
                    if j not in same_as
                    and drafts[j].category == d.category
                    and _cosine(drafts[j].vector, d.vector) >= AUTO_MATCH
                ),
                None,
            )
            if twin is not None:
                same_as[i] = twin
            else:
                undecided.append(i)

        if any(candidates[i] for i in undecided) or len(undecided) > 1:
            lines = []
            for i in undecided:
                d = drafts[i]
                lines.append(f"[draft {i}] {d.category} | {d.title} — {d.description}")
                for t, sim in candidates[i]:
                    lines.append(
                        f"    candidate {t.id}: {t.title} — {t.description} (sim {sim:.2f})"
                    )
                if not candidates[i]:
                    lines.append("    (no candidates)")
            try:
                reply: _MatchReply = await ask_json(
                    self.llm,
                    prompts.TOPIC_MATCH_SYSTEM,
                    prompts.TOPIC_MATCH_USER.format(items="\n".join(lines)),
                    _MatchReply,
                )
                for m in reply.matches:
                    if m.draft not in undecided:
                        continue
                    valid = {t.id for t, _ in candidates[m.draft]}
                    if m.topic in valid:
                        drafts[m.draft].topic_id = m.topic
                    elif (
                        m.same_as_draft is not None
                        and m.same_as_draft in undecided
                        and m.same_as_draft < m.draft
                        and drafts[m.same_as_draft].category == drafts[m.draft].category
                    ):
                        same_as[m.draft] = m.same_as_draft
            except LLMError as e:
                log.warning("Topic matching failed, creating new topics: %s", e)

        for i, d in enumerate(drafts):
            if d.topic_id is not None:
                stats.topics_matched += 1
                continue
            if i in same_as:
                root = same_as[i]
                while root in same_as:
                    root = same_as[root]
                if drafts[root].topic_id is None:  # create the shared topic first
                    await self._create_topic(session, account_id, drafts[root], stats)
                d.topic_id = drafts[root].topic_id
                stats.topics_matched += 1
                continue
            await self._create_topic(session, account_id, d, stats)

    @staticmethod
    async def _create_topic(session, account_id: int, d: _Draft, stats: LabelStats) -> None:
        topic = Topic(
            account_id=account_id,
            category=d.category,
            title=d.title,
            description=d.description,
            severity=d.severity,
            embedding=d.vector,
        )
        session.add(topic)
        await session.flush()
        d.topic_id = topic.id
        stats.topics_new += 1

    async def _write_labels(
        self, session, extractions: list[BatchExtraction], drafts: list[_Draft]
    ):
        now = repo.now()
        model = f"{self.llm.provider}/{self.llm.model}"
        rows = [
            {
                "id": int(comment.id),
                "sentiment": sentiment.value if sentiment else None,
                "labeled_at": now,
                "label_model": model,
            }
            for ex in extractions
            for comment, sentiment in zip(ex.batch, ex.sentiments, strict=True)
        ]
        if rows:
            await session.execute(update(Feedback), rows)
        labeled = len(rows)

        links = {(int(c.id), d.topic_id) for d in drafts for c in d.refs}
        if links:
            await session.execute(
                insert(FeedbackTopic)
                .values([{"feedback_id": f, "topic_id": t} for f, t in links])
                .on_conflict_do_nothing()
            )
        seen: dict[int, list[datetime]] = defaultdict(list)
        severity: dict[int, int] = {}
        for d in drafts:
            seen[d.topic_id].extend(c.created_at for c in d.refs if c.created_at)
            if d.severity:
                severity[d.topic_id] = max(severity.get(d.topic_id, 0), d.severity)
        for topic in (
            await session.execute(select(Topic).where(Topic.id.in_(list(seen))))
        ).scalars():
            times = seen[topic.id]
            if times:
                topic.first_seen_at = min([t for t in (topic.first_seen_at, min(times)) if t])
                topic.last_seen_at = max([t for t in (topic.last_seen_at, max(times)) if t])
            if topic.id in severity:
                topic.severity = max(topic.severity or 0, severity[topic.id])
        return labeled


async def embed_pending(
    sessionmaker,
    embedder: Embedder,
    flt: FeedbackFilter,
    on_progress: Callable[[str], None] | None = None,
    batch: int = 256,
) -> int:
    """Fill ``feedback.embedding`` for rows that have none."""
    progress = on_progress or (lambda _msg: None)
    done = 0
    while True:
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    flt.apply(
                        select(Feedback.id, Feedback.title, Feedback.content)
                        .join(Work, Work.id == Feedback.work_id)
                        .where(Feedback.embedding.is_(None))
                    )
                    .order_by(Feedback.created_at.desc().nulls_last())
                    .limit(batch)
                )
            ).all()
            if not rows:
                return done
            vectors = await embedder.embed(
                [f"{title}\n{content}" if title else content for _, title, content in rows]
            )
            await session.execute(
                update(Feedback),
                [
                    {"id": fid, "embedding": vector}
                    for (fid, _, _), vector in zip(rows, vectors, strict=True)
                ],
            )
            await session.commit()
        done += len(rows)
        progress(f"Embedded {done}")
