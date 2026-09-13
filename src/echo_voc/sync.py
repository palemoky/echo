"""Incremental sync: account → works → comments / danmaku, checkpointed per work."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from echo_voc.errors import ConfigError, EchoError, SourceError
from echo_voc.models import Comment
from echo_voc.sources import SOURCES, detect_link
from echo_voc.sources.base import Source
from echo_voc.store import repo
from echo_voc.store.models import Account, SyncRun, Work

log = logging.getLogger(__name__)

TOP_LEVEL_KINDS = {"comment", "review"}


@dataclass
class SyncOptions:
    since: datetime | None = None  # ignore works / comments older than this
    replies: bool = False
    danmaku: bool = False
    comments: bool = True
    full: bool = False  # re-list every work and walk every comment again
    max_per_work: int | None = None
    # Stop walking a newest-first stream after this many consecutive already-covered items.
    stop_after_old: int = 20
    stop_after_known_works: int = 5
    overlap: timedelta = timedelta(hours=6)  # re-read recent items to refresh like counts
    danmaku_refresh: timedelta = timedelta(days=1)
    max_consecutive_failures: int = 3


@dataclass
class SyncStats:
    works_new: int = 0
    works_synced: int = 0
    comments_new: int = 0
    danmaku_new: int = 0
    errors: list[str] = field(default_factory=list)


class Syncer:
    def __init__(self, sessionmaker, on_progress: Callable[[str], None] | None = None) -> None:
        self.sessionmaker = sessionmaker
        self.progress = on_progress or (lambda _msg: None)
        self._sources: dict[str, Source] = {}

    def source(self, platform: str) -> Source:
        if platform not in self._sources:
            self._sources[platform] = SOURCES[platform]()
        return self._sources[platform]

    async def aclose(self) -> None:
        for source in self._sources.values():
            await source.aclose()

    # ---- registration -----------------------------------------------------------------------

    async def add(self, url: str) -> tuple[Account, Work | None]:
        """Track an account (all works) or a single work (its owner is tracked partially)."""
        detected, kind = detect_link(url)
        source = self._sources.setdefault(detected.name, detected)
        async with self.sessionmaker() as session:
            if kind == "account":
                info = await source.resolve_account(url)
                account = await repo.upsert_account(session, info, track_all=True)
                work = None
            else:
                info, work_info = await source.resolve_work(url)
                account = await repo.upsert_account(session, info, track_all=False)
                work, _ = await repo.upsert_work(session, account.id, work_info)
            await session.commit()
        return account, work

    # ---- sync -------------------------------------------------------------------------------

    async def sync_all(self, options: SyncOptions) -> SyncStats:
        async with self.sessionmaker() as session:
            ids = (await session.execute(select(Account.id).order_by(Account.id))).scalars().all()
        stats = SyncStats()
        for account_id in ids:
            self._merge(stats, await self.sync_account(account_id, options))
        return stats

    async def sync_account(self, account_id: int, options: SyncOptions) -> SyncStats:
        stats = SyncStats()
        async with self.sessionmaker() as session:
            account = await session.get(Account, account_id)
            if account is None:
                raise EchoError(f"Account {account_id} not found")
            label = account.name or account.external_id
            source = self.source(account.platform)
            if account.track_all:
                self.progress(f"[{label}] listing works")
                await self._sync_works(session, account, source, options, stats)
            query = select(Work).where(Work.account_id == account.id)
            if options.since:
                query = query.where(
                    (Work.published_at >= options.since) | (Work.published_at.is_(None))
                )
            works = (
                (await session.execute(query.order_by(Work.published_at.desc()))).scalars().all()
            )

        failures = 0
        for i, work in enumerate(works, 1):
            self.progress(f"[{label}] {i}/{len(works)} {work.title or work.external_id}")
            try:
                await self.sync_work(work.id, options, stats)
                failures = 0
            except ConfigError:
                raise
            except SourceError as e:
                failures += 1
                stats.errors.append(f"{work.external_id}: {e}")
                log.warning("Sync failed for %s: %s", work.external_id, e)
                if failures >= options.max_consecutive_failures:
                    stats.errors.append(f"[{label}] aborted after {failures} consecutive failures")
                    break

        async with self.sessionmaker() as session:
            account = await session.get(Account, account_id)
            account.last_synced_at = repo.now()
            await session.commit()
        return stats

    async def sync_work(self, work_id: int, options: SyncOptions, stats: SyncStats) -> None:
        async with self.sessionmaker() as session:
            work = await session.get(Work, work_id)
            source = self.source(work.platform)
            if options.comments:
                await self._run(
                    session,
                    work,
                    "comments",
                    stats,
                    self._sync_comments(session, work, source, options, stats),
                )
            if options.danmaku and source.supports_danmaku:
                await self._run(
                    session,
                    work,
                    "danmaku",
                    stats,
                    self._sync_danmaku(session, work, source, options, stats),
                )
            stats.works_synced += 1

    # ---- internals --------------------------------------------------------------------------

    async def _sync_works(self, session, account, source: Source, options, stats) -> None:
        run = SyncRun(account_id=account.id, kind="works")
        session.add(run)
        known_streak = 0
        first_sync = account.last_synced_at is None
        try:
            async for info in source.iter_works(repo.account_info(account)):
                if options.since and info.published_at and info.published_at < options.since:
                    break
                _, created = await repo.upsert_work(session, account.id, info)
                run.seen_items += 1
                if created:
                    run.new_items += 1
                    stats.works_new += 1
                    known_streak = 0
                else:
                    known_streak += 1
                    if (
                        not (options.full or first_sync)
                        and known_streak >= options.stop_after_known_works
                    ):
                        break
            run.status = "ok"
        except SourceError as e:
            run.status, run.error = "failed", str(e)
            stats.errors.append(f"works: {e}")
        run.finished_at = repo.now()
        await session.commit()

    async def _run(self, session, work: Work, kind: str, stats: SyncStats, coro) -> None:
        run = SyncRun(account_id=work.account_id, work_id=work.id, kind=kind)
        session.add(run)
        await session.commit()
        try:
            run.new_items, run.seen_items = await coro
            run.status = "ok"
        except EchoError as e:
            await session.rollback()
            run.status, run.error = "failed", str(e)
            run.finished_at = repo.now()
            await session.commit()
            raise
        run.finished_at = repo.now()
        await session.commit()

    async def _sync_comments(self, session, work: Work, source: Source, options, stats):
        cutoff = None
        if not options.full and work.latest_comment_at:
            cutoff = work.latest_comment_at - options.overlap
        if options.since and (cutoff is None or options.since > cutoff):
            cutoff = options.since

        buffer: list[Comment] = []
        new = seen = old_streak = 0
        latest = work.latest_comment_at

        async def flush() -> None:
            nonlocal new
            if buffer:
                new += await repo.upsert_feedback(session, work, buffer)
                buffer.clear()
                await session.commit()  # checkpoint; the watermark only moves once the walk ends

        async for comment in source.iter_comments(repo.work_info(work), replies=options.replies):
            seen += 1
            is_old = bool(cutoff and comment.created_at and comment.created_at < cutoff)
            if comment.kind in TOP_LEVEL_KINDS:
                old_streak = old_streak + 1 if is_old else 0
                if old_streak >= options.stop_after_old:
                    break
            if options.since and comment.created_at and comment.created_at < options.since:
                continue
            if comment.created_at and (latest is None or comment.created_at > latest):
                latest = comment.created_at
            buffer.append(comment)
            if len(buffer) >= 200:
                await flush()
            if options.max_per_work and seen >= options.max_per_work:
                # Sampled, not complete: `sync --full` is needed to backfill older comments.
                work.meta = {**(work.meta or {}), "partial": True}
                break
        else:
            if work.meta and work.meta.get("partial") and cutoff is None:
                work.meta = {k: v for k, v in work.meta.items() if k != "partial"}
        await flush()

        work.latest_comment_at = latest
        work.comments_synced_at = repo.now()
        stats.comments_new += new
        return new, seen

    async def _sync_danmaku(self, session, work: Work, source: Source, options, stats):
        if (
            not options.full
            and work.danmaku_synced_at
            and repo.now() - work.danmaku_synced_at < options.danmaku_refresh
        ):
            return 0, 0
        info = repo.work_info(work)
        items = await source.fetch_danmaku(info)
        if options.since:
            items = [d for d in items if not d.created_at or d.created_at >= options.since]
        new = await repo.upsert_feedback(session, work, items)
        work.meta = {**(work.meta or {}), **info.meta}
        work.danmaku_synced_at = repo.now()
        stats.danmaku_new += new
        return new, len(items)

    @staticmethod
    def _merge(total: SyncStats, part: SyncStats) -> None:
        total.works_new += part.works_new
        total.works_synced += part.works_synced
        total.comments_new += part.comments_new
        total.danmaku_new += part.danmaku_new
        total.errors.extend(part.errors)
