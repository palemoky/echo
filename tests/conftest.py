from __future__ import annotations

import json

import pytest

from echo_voc.llm.base import LLM
from echo_voc.models import Comment, FetchResult
from echo_voc.sources.base import Source
from echo_voc.sources.bilibili import BilibiliSource


@pytest.fixture(scope="session")
def migrated_db():
    from db_utils import TEST_DB, ensure_test_database

    if not ensure_test_database():
        pytest.skip("PostgreSQL not available (docker compose up -d)")
    from echo_voc.store.db import migrate

    migrate(TEST_DB)
    return TEST_DB


@pytest.fixture
async def sessionmaker(migrated_db):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE accounts, works, feedback, topics, feedback_topics, snapshots, sync_runs "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Keep tests fast: no politeness delay or retry backoff."""
    monkeypatch.setattr(Source, "backoff_base", 0.0)
    monkeypatch.setattr(BilibiliSource, "min_interval", 0.0)
    monkeypatch.setattr(BilibiliSource, "jitter", 0.0)


class FakeLLM(LLM):
    """Scripted LLM: answers map prompts per batch and the reduce prompt once."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, map_reply=None, reduce_reply=None):
        self.map_reply = map_reply
        self.reduce_reply = reduce_reply
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        self.calls.append((system, user))
        if "Draft themes" in user:
            reply = self.reduce_reply
        else:
            reply = self.map_reply
        if callable(reply):
            reply = reply(user)
        return reply if isinstance(reply, str) else json.dumps(reply)


@pytest.fixture
def sample_data() -> FetchResult:
    texts = [
        ("App crashes every time I open the camera", 1, 50),
        ("Crashes on launch after the update, unusable", 1, 30),
        ("Love the new dark mode!", 5, 10),
        ("Please add an export to PDF option", 4, 5),
        ("Great app, but it keeps crashing when uploading photos", 3, 80),
    ]
    return FetchResult(
        platform="appstore",
        target="appstore:1",
        title="Demo App",
        comments=[
            Comment(id=f"c{i}", platform="appstore", content=t, rating=r, likes=likes)
            for i, (t, r, likes) in enumerate(texts)
        ],
    )


@pytest.fixture
def fake_llm_cls():
    return FakeLLM
