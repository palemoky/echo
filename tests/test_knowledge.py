"""Labeling, topic catalog, RAG and snapshots against a real PostgreSQL + pgvector."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from fakes import FakeEmbedder
from sqlalchemy import func, select

from echo_voc.label import Labeler, embed_pending
from echo_voc.llm.base import LLM
from echo_voc.memory.snapshots import compare, save_snapshot
from echo_voc.models import AccountInfo, Comment, WorkInfo
from echo_voc.rag import Asker
from echo_voc.store import repo
from echo_voc.store.models import Account, Feedback, FeedbackTopic, Topic
from echo_voc.store.query import FeedbackFilter
from echo_voc.timeutil import utcnow

pytestmark = pytest.mark.db

THEMES = {
    "crash": ("pain_point", "App crashes on launch", "The app crashes right after opening"),
    "dark": ("feature_request", "Add dark mode", "Users want a dark theme"),
    "love": ("praise", "Clean design", "Users like the clean interface"),
}


class ScriptedLLM(LLM):
    """Classifies by keyword, like a (very) literal analyst."""

    provider, model = "scripted", "v1"

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls: list[str] = []

    async def complete(self, system, user, *, json_mode=False):
        self.calls.append(system[:40])
        if "Draft topics with their closest" in user:
            matches = []
            for m in re.finditer(r"\[draft (\d+)\][^\n]*\n((?:    [^\n]*\n?)*)", user):
                cands = re.findall(r"candidate (\d+):", m.group(2))
                matches.append(
                    {"draft": int(m.group(1)), "topic": int(cands[0]) if cands else None}
                )
            return json.dumps({"matches": matches})
        if "structured query" in system:
            return json.dumps(self.plan)
        if "answering a creator's question" in system:
            ids = re.findall(r"\[#(\d+)\]", user)
            return "Top issue is crashes " + " ".join(f"[#{i}]" for i in ids[:2]) + " [#999999]"
        if "period summaries" in system:
            return "Crashes dominate."
        rows = re.findall(r"^\[(\d+)\] \([^)]*\) (.*)$", user, re.M)
        groups: dict[str, list[int]] = {}
        sentiments = {}
        for n, text in rows:
            key = next((k for k in THEMES if k in text), None)
            sentiments[n] = {"crash": "negative", "love": "positive"}.get(key, "neutral")
            if key:
                groups.setdefault(key, []).append(int(n))
        themes = [
            {
                "category": THEMES[k][0],
                "title": THEMES[k][1],
                "description": THEMES[k][2],
                "severity": 4 if k == "crash" else None,
                "comments": ns,
            }
            for k, ns in groups.items()
        ]
        return json.dumps({"sentiments": sentiments, "themes": themes})


async def seed(sessionmaker, texts: list[tuple[str, float]]) -> Account:
    """texts: (content, days_ago)."""
    async with sessionmaker() as session:
        account = await repo.upsert_account(
            session, AccountInfo(platform="fake", external_id="a1", name="Acme"), track_all=True
        )
        work, _ = await repo.upsert_work(
            session, account.id, WorkInfo(platform="fake", external_id="w1", title="App")
        )
        now = utcnow()
        await repo.upsert_feedback(
            session,
            work,
            [
                Comment(
                    id=f"c{i}",
                    platform="fake",
                    content=t,
                    likes=i,
                    created_at=now - timedelta(days=d),
                )
                for i, (t, d) in enumerate(texts)
            ],
        )
        await session.commit()
        return account


async def test_label_builds_topic_catalog_incrementally(sessionmaker):
    await seed(
        sessionmaker,
        [(f"crash again #{i}", 1) for i in range(5)]
        + [("please add dark mode", 2), ("love it", 3), ("meh", 3)],
    )
    llm = ScriptedLLM()
    labeler = Labeler(sessionmaker, llm, FakeEmbedder())
    flt = FeedbackFilter()

    stats = await labeler.run(flt, limit=4)
    assert stats.labeled == 4
    stats = await labeler.run(flt)
    assert stats.labeled == 4

    async with sessionmaker() as s:
        topics = (
            await s.execute(
                select(Topic.title, func.count(FeedbackTopic.feedback_id))
                .join(FeedbackTopic)
                .group_by(Topic.id)
            )
        ).all()
        sentiments = dict(
            (
                await s.execute(
                    select(Feedback.sentiment, func.count()).group_by(Feedback.sentiment)
                )
            ).all()
        )
    # "App crashes on launch" was seen in both rounds but is still a single topic.
    assert sorted(topics) == [
        ("Add dark mode", 1),
        ("App crashes on launch", 5),
        ("Clean design", 1),
    ]
    assert sentiments == {"negative": 5, "neutral": 2, "positive": 1}
    assert await labeler.run(flt) == type(stats)()  # nothing left


async def test_ask_ranking_counts_windows_and_filters_citations(sessionmaker):
    await seed(
        sessionmaker,
        [(f"crash {i}", 3) for i in range(4)]
        + [("crash old", 45), ("love it", 5), ("dark please", 50), ("dark again", 52)],
    )
    llm = ScriptedLLM()
    await Labeler(sessionmaker, llm, FakeEmbedder()).run(FeedbackFilter())

    llm.plan = {"intent": "ranking", "search": "top issues"}
    result = await Asker(sessionmaker, llm, FakeEmbedder()).ask("最近1个月用户反馈较多的问题是什么")
    top = result.topics[0]
    assert (top.title, top.mentions, top.previous) == ("App crashes on launch", 4, 1)
    assert [t.title for t in result.topics] == ["App crashes on launch", "Clean design"]
    assert result.window[0] is not None  # defaulted to 30 days
    assert any("999999" in w for w in result.warnings)  # hallucinated citation flagged
    assert all(s.content.startswith("crash") for s in result.sources[:2])


async def test_ask_lookup_uses_vectors(sessionmaker):
    await seed(
        sessionmaker,
        [
            ("battery drains fast overnight", 1),
            ("love the clean design", 1),
            ("battery drains quickly", 2),
        ],
    )
    emb = FakeEmbedder()
    assert await embed_pending(sessionmaker, emb, FeedbackFilter()) == 3
    llm = ScriptedLLM({"intent": "lookup", "search": "battery drains"})
    result = await Asker(sessionmaker, llm, emb).ask("what do people say about battery?")
    assert {s.content for s in result.sources[:2]} == {
        "battery drains fast overnight",
        "battery drains quickly",
    }
    assert not result.topics


async def test_snapshots_and_compare(sessionmaker):
    account = await seed(
        sessionmaker,
        [
            ("crash", 40),
            ("crash", 41),
            ("love it", 42),
            ("crash", 5),
            ("dark please", 6),
            ("dark", 7),
            ("dark mode", 8),
            ("love it", 9),
        ],
    )
    llm = ScriptedLLM()
    await Labeler(sessionmaker, llm, FakeEmbedder()).run(FeedbackFilter())
    now = utcnow()
    async with sessionmaker() as s:
        acc = await s.get(Account, account.id)
        old = await save_snapshot(
            s, acc, "old", start=now - timedelta(days=60), end=now - timedelta(days=30), llm=llm
        )
        new = await save_snapshot(s, acc, "new", start=now - timedelta(days=30), end=now, llm=None)
        again = await save_snapshot(
            s, acc, "old", start=now - timedelta(days=60), end=now - timedelta(days=30), llm=None
        )
        await s.commit()
    assert old.summary == "Crashes dominate." and again.id == old.id
    assert again.summary == "Crashes dominate."  # refresh without LLM keeps the summary
    diff = compare(old, new, min_mentions=1)
    status = {c.title: c.status for c in diff.changes}
    assert status == {
        "Add dark mode": "new",
        "App crashes on launch": "falling",
        "Clean design": "falling",
    }
    assert diff.sentiment_delta["negative"] < 0
