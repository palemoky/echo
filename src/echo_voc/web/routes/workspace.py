from __future__ import annotations

from fastapi import APIRouter, Form, Query, Request
from sqlalchemy import func, or_, select

from echo_voc import workspace
from echo_voc.store import repo
from echo_voc.store.db import get_sessionmaker
from echo_voc.store.models import Feedback, Snapshot
from echo_voc.sync import SyncOptions
from echo_voc.web.routes.common import (
    SINCE_CHOICES,
    clean,
    int_or_none,
    job_card,
    jobs,
    llm_defaults,
    since,
)
from echo_voc.web.templating import render

router = APIRouter()
PAGE_SIZE = 50


@router.get("/accounts")
async def accounts_page(request: Request):
    return render(request, "accounts.html", accounts=await workspace.accounts_overview())


@router.post("/accounts")
async def add_account(request: Request, url: str = Form(...)):
    account, work = await workspace.add(url.strip())
    message = (
        f"已开始跟踪账号「{account.name or account.external_id}」"
        if work is None
        else f"已添加作品「{work.title}」（账号：{account.name or account.external_id}）"
    )
    return render(
        request,
        "partials/accounts_table.html",
        accounts=await workspace.accounts_overview(),
        flash=message,
    )


@router.get("/accounts/{account_id}")
async def account_page(request: Request, account_id: int):
    async with get_sessionmaker()() as session:
        account = await repo.find_account(session, account_id)
        snapshots = (
            (
                await session.execute(
                    select(Snapshot.label)
                    .where(Snapshot.account_id == account.id)
                    .order_by(Snapshot.period_start.desc().nulls_last())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
    overview = next(a for a in await workspace.accounts_overview() if a.id == account.id)
    return render(
        request,
        "account.html",
        account=account,
        overview=overview,
        works=await workspace.works_overview(account=str(account.id)),
        snapshots=snapshots,
        since_choices=SINCE_CHOICES,
        **llm_defaults(),
    )


@router.get("/works/{work_id}")
async def work_page(request: Request, work_id: int):
    async with get_sessionmaker()() as session:
        work = await repo.find_work(session, work_id)
        account = await repo.find_account(session, work.account_id)
        counts = dict(
            (
                await session.execute(
                    select(Feedback.kind, func.count())
                    .where(Feedback.work_id == work.id)
                    .group_by(Feedback.kind)
                )
            ).all()
        )
    return render(request, "work.html", work=work, account=account, counts=counts, **llm_defaults())


@router.get("/works/{work_id}/feedback")
async def work_feedback(
    request: Request,
    work_id: int,
    kind: str = "",
    sentiment: str = "",
    q: str = "",
    order: str = "newest",
    page: int = 1,
):
    query = select(Feedback).where(Feedback.work_id == work_id)
    if kind:
        query = query.where(Feedback.kind == kind)
    else:
        query = query.where(Feedback.kind != "danmaku")
    if sentiment:
        query = query.where(Feedback.sentiment == sentiment)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Feedback.content.ilike(like), Feedback.title.ilike(like)))
    ordering = {
        "newest": Feedback.created_at.desc().nulls_last(),
        "likes": Feedback.likes.desc().nulls_last(),
        "progress": Feedback.progress_ms.asc().nulls_last(),
    }.get(order, Feedback.created_at.desc().nulls_last())
    page = max(1, page)
    async with get_sessionmaker()() as session:
        total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar()
        rows = (
            (
                await session.execute(
                    query.order_by(ordering).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
                )
            )
            .scalars()
            .all()
        )
    return render(
        request,
        "partials/feedback_list.html",
        rows=rows,
        total=total,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        params={"kind": kind, "sentiment": sentiment, "q": q, "order": order},
        base=f"/works/{work_id}/feedback",
    )


# ---- jobs ----------------------------------------------------------------------------------------


@router.post("/jobs/sync")
async def start_sync(
    request: Request,
    account: str = Form(""),
    work: str = Form(""),
    since_value: str = Form("", alias="since"),
    danmaku: bool = Form(False),
    replies: bool = Form(False),
    full: bool = Form(False),
    max_per_work: str = Form(""),
):
    options = SyncOptions(
        since=since(since_value),
        danmaku=danmaku,
        replies=replies,
        full=full,
        max_per_work=int_or_none(max_per_work),
    )
    scope = {"account": clean(account), "work": clean(work)}
    title = "同步 " + (
        "作品 #" + scope["work"]
        if scope["work"]
        else "账号 #" + scope["account"]
        if scope["account"]
        else "全部账号"
    )
    job = jobs(request).submit(
        "sync",
        title,
        lambda progress: workspace.sync(**scope, options=options, on_progress=progress),
        trigger="data-changed",
    )
    return job_card(request, job)


@router.post("/jobs/embed")
async def start_embed(request: Request, account: str = Form(""), work: str = Form("")):
    scope = {"account": clean(account), "work": clean(work)}
    job = jobs(request).submit(
        "embed",
        "计算向量（bge-m3）",
        lambda progress: workspace.embed(**scope, on_progress=progress),
        trigger="data-changed",
    )
    return job_card(request, job)


@router.get("/label/estimate")
async def label_estimate(
    request: Request,
    account: str = "",
    work: str = "",
    since_value: str = Query("", alias="since"),
    limit: str = "",
):
    scope = {
        "account": clean(account),
        "work": clean(work),
        "since": since(since_value),
        "limit": int_or_none(limit),
    }
    estimate = await workspace.estimate_label(**scope)
    return render(
        request,
        "partials/label_estimate.html",
        estimate=estimate,
        form={"account": account, "work": work, "since": since_value, "limit": limit},
        **llm_defaults(),
    )


@router.post("/jobs/label")
async def start_label(
    request: Request,
    account: str = Form(""),
    work: str = Form(""),
    since_value: str = Form("", alias="since"),
    limit: str = Form(""),
    provider: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
):
    scope = {
        "account": clean(account),
        "work": clean(work),
        "since": since(since_value),
        "limit": int_or_none(limit),
    }
    job = jobs(request).submit(
        "label",
        "打标与归类问题",
        lambda progress: workspace.label(
            **scope,
            provider=clean(provider),
            model=clean(model),
            lang=clean(language),
            on_progress=progress,
        ),
        trigger="data-changed",
    )
    return job_card(request, job)
