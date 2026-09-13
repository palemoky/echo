from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from echo_voc.errors import EchoError
from echo_voc.models import AccountInfo, Comment, WorkInfo
from echo_voc.store.models import Account, Feedback, Work


def now() -> datetime:
    return datetime.now(timezone.utc)


def author_hash(platform: str, author: str | None) -> str | None:
    """Pseudonymous, stable author key; raw user ids are never stored."""
    if not author:
        return None
    return hashlib.sha256(f"{platform}:{author}".encode()).hexdigest()[:16]


async def upsert_account(session: AsyncSession, info: AccountInfo, *, track_all: bool) -> Account:
    stmt = insert(Account).values(
        platform=info.platform,
        external_id=info.external_id,
        name=info.name,
        url=info.url,
        meta=info.meta,
        track_all=track_all,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["platform", "external_id"],
        set_={
            "name": func.coalesce(stmt.excluded.name, Account.name),
            "url": func.coalesce(stmt.excluded.url, Account.url),
            "meta": Account.meta.op("||")(stmt.excluded.meta),
            "track_all": Account.track_all | stmt.excluded.track_all,
        },
    ).returning(Account.id)
    account_id = (await session.execute(stmt)).scalar_one()
    return await session.get(Account, account_id, populate_existing=True)


async def upsert_work(session: AsyncSession, account_id: int, info: WorkInfo) -> tuple[Work, bool]:
    """Insert or refresh a work. Returns (work, created)."""
    stmt = insert(Work).values(
        account_id=account_id,
        platform=info.platform,
        external_id=info.external_id,
        title=info.title,
        url=info.url,
        published_at=info.published_at,
        meta=info.meta,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["platform", "external_id"],
        set_={
            "title": func.coalesce(stmt.excluded.title, Work.title),
            "url": func.coalesce(stmt.excluded.url, Work.url),
            "published_at": func.coalesce(stmt.excluded.published_at, Work.published_at),
            "meta": Work.meta.op("||")(stmt.excluded.meta),
        },
    ).returning(Work.id, literal_column("xmax = 0"))
    work_id, created = (await session.execute(stmt)).one()
    return await session.get(Work, work_id, populate_existing=True), bool(created)


async def upsert_feedback(session: AsyncSession, work: Work, comments: Iterable[Comment]) -> int:
    """Insert new feedback and refresh mutable fields of known items. Returns #new rows."""
    rows = {}
    for c in comments:
        if not c.content.strip():
            continue
        rows[(c.kind, c.id)] = {
            "work_id": work.id,
            "platform": c.platform,
            "kind": c.kind,
            "external_id": c.id,
            "parent_external_id": c.parent_id,
            "author_hash": author_hash(c.platform, c.author),
            "title": c.title,
            "content": c.content,
            "rating": c.rating,
            "likes": c.likes,
            "app_version": c.version,
            "progress_ms": c.progress_ms,
            "url": c.url,
            "created_at": c.created_at,
        }
    if not rows:
        return 0
    new = 0
    values = list(rows.values())
    for start in range(0, len(values), 1000):
        stmt = insert(Feedback).values(values[start : start + 1000])
        stmt = stmt.on_conflict_do_update(
            index_elements=["platform", "kind", "external_id"],
            set_={"likes": stmt.excluded.likes, "fetched_at": func.now()},
        ).returning(literal_column("xmax = 0"))
        new += sum(1 for (inserted,) in (await session.execute(stmt)).all() if inserted)
    return new


async def find_account(session: AsyncSession, ref: str | int) -> Account:
    """Look up an account by numeric id, external id or (partial) name."""
    ref = str(ref)
    query = select(Account)
    if ref.isdigit():
        found = await session.get(Account, int(ref))
        if found:
            return found
        query = query.where(Account.external_id == ref)
    else:
        query = query.where((Account.external_id == ref) | Account.name.ilike(f"%{ref}%"))
    found = (await session.execute(query.limit(2))).scalars().all()
    if len(found) != 1:
        raise EchoError(
            f"Account '{ref}' not found" if not found else f"Account '{ref}' is ambiguous"
        )
    return found[0]


async def find_work(session: AsyncSession, ref: str | int) -> Work:
    ref = str(ref)
    if ref.isdigit() and (found := await session.get(Work, int(ref))):
        return found
    found = (
        (await session.execute(select(Work).where(Work.external_id == ref).limit(1)))
        .scalars()
        .first()
    )
    if not found:
        raise EchoError(f"Work '{ref}' not found")
    return found


def work_info(work: Work) -> WorkInfo:
    return WorkInfo(
        platform=work.platform,
        external_id=work.external_id,
        title=work.title,
        url=work.url,
        published_at=work.published_at,
        meta=dict(work.meta or {}),
    )


def account_info(account: Account) -> AccountInfo:
    return AccountInfo(
        platform=account.platform,
        external_id=account.external_id,
        name=account.name,
        url=account.url,
        meta=dict(account.meta or {}),
    )
