from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import T0, FakeSource, comment, make_work
from sqlalchemy import func, select

from echo_voc import sources
from echo_voc.store.models import Account, Feedback, SyncRun, Work
from echo_voc.sync import Syncer, SyncOptions

pytestmark = pytest.mark.db


@pytest.fixture
def fake(monkeypatch):
    source = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", FakeSource)
    return source


async def count(sessionmaker, model, *where):
    async with sessionmaker() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar()


async def test_account_sync_is_incremental(sessionmaker, fake):
    fake.works = [make_work("v2", 1), make_work("v1", 10)]
    fake.comments = {
        "v1": [comment(i, hours_ago=i * 2, work="v1") for i in range(60)],
        "v2": [comment(i, hours_ago=i, work="v2", likes=i) for i in range(5)],
    }
    fake.danmaku = {"v2": [comment(i, kind="danmaku", work="v2") for i in range(8)]}
    syncer = Syncer(sessionmaker)
    syncer._sources["fake"] = fake

    account, work = await syncer.add("fake://account/acc")
    assert work is None and account.track_all

    stats = await syncer.sync_account(account.id, SyncOptions(danmaku=True))
    assert (stats.works_new, stats.comments_new, stats.danmaku_new) == (2, 65, 8)
    assert await count(sessionmaker, Feedback) == 73

    # New comments arrive; a second run should only walk until it meets old ones.
    fake.comments["v1"] += [comment(100 + i, hours_ago=-1 - i, work="v1") for i in range(3)]
    fake.pulled.clear()
    stats = await syncer.sync_account(account.id, SyncOptions(danmaku=True))
    assert stats.comments_new == 3 and stats.works_new == 0 and stats.danmaku_new == 0
    assert fake.pulled["v1"] < 40  # stopped early instead of re-reading all 63
    assert await count(sessionmaker, SyncRun, SyncRun.status == "ok") >= 6


async def test_like_counts_refresh_and_since_filter(sessionmaker, fake):
    fake.works = [make_work("v1")]
    fake.comments = {"v1": [comment(1, hours_ago=1, likes=1), comment(2, hours_ago=24 * 40)]}
    syncer = Syncer(sessionmaker)
    syncer._sources["fake"] = fake
    account, _ = await syncer.add("fake://account/acc")

    await syncer.sync_account(account.id, SyncOptions(since=T0 - timedelta(days=30)))
    assert await count(sessionmaker, Feedback) == 1  # 40-day-old comment skipped

    fake.comments["v1"][0] = comment(1, hours_ago=1, likes=99)
    await syncer.sync_account(account.id, SyncOptions())
    async with sessionmaker() as s:
        likes = (
            await s.execute(select(Feedback.likes).where(Feedback.external_id == "v1-comment-1"))
        ).scalar()
    assert likes == 99


async def test_add_single_work_tracks_owner_partially(sessionmaker, fake):
    fake.works = [make_work("v1"), make_work("v2")]
    syncer = Syncer(sessionmaker)
    syncer._sources["fake"] = fake
    account, work = await syncer.add("fake://work/v1")
    assert not account.track_all and work.external_id == "v1"
    await syncer.sync_account(account.id, SyncOptions())
    assert fake.listed == 0  # did not list the whole account
    assert await count(sessionmaker, Work) == 1
    # Adding the account later upgrades it to track everything.
    account, _ = await syncer.add("fake://account/acc")
    assert account.track_all
    assert await count(sessionmaker, Account) == 1


async def test_max_per_work_marks_partial(sessionmaker, fake):
    fake.works = [make_work("v1")]
    fake.comments = {"v1": [comment(i, hours_ago=i) for i in range(10)]}
    syncer = Syncer(sessionmaker)
    syncer._sources["fake"] = fake
    account, _ = await syncer.add("fake://account/acc")
    await syncer.sync_account(account.id, SyncOptions(max_per_work=4))
    async with sessionmaker() as s:
        work = (await s.execute(select(Work))).scalar_one()
    assert work.meta.get("partial") is True
    assert await count(sessionmaker, Feedback) == 4
