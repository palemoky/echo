from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from sqlalchemy import and_, select

from echo_voc import workspace
from echo_voc.store import repo
from echo_voc.store.db import get_sessionmaker
from echo_voc.store.models import Feedback, FeedbackTopic, Snapshot, Topic, Work
from echo_voc.web import charts
from echo_voc.web.routes.common import SINCE_CHOICES, clean, job_card, jobs, llm_defaults, since
from echo_voc.web.templating import render

router = APIRouter()


@router.get("/ask")
async def ask_page(request: Request):
    return render(
        request, "ask.html", accounts=await workspace.accounts_overview(), **llm_defaults()
    )


@router.post("/jobs/ask")
async def start_ask(
    request: Request,
    question: str = Form(...),
    account: str = Form(""),
    memory: bool = Form(False),
    provider: str = Form(""),
    model: str = Form(""),
):
    question = question.strip()
    if not question:
        raise ValueError("请输入问题")
    job = jobs(request).submit(
        "ask",
        question[:60],
        lambda progress: workspace.ask(
            question,
            account=clean(account),
            provider=clean(provider),
            model=clean(model),
            memory=memory,
        ),
    )
    return job_card(request, job)


@router.get("/topics")
async def topics_page(
    request: Request,
    account: str = "",
    since_value: str = Query("30d", alias="since"),
    category: str = "",
):
    return render(
        request,
        "topics.html",
        accounts=await workspace.accounts_overview(),
        since_choices=SINCE_CHOICES,
        filters={"account": account, "since": since_value, "category": category},
        rows=await workspace.topics(
            account=clean(account), since=since(since_value), category=clean(category), limit=50
        ),
    )


@router.get("/topics/table")
async def topics_table(
    request: Request,
    account: str = "",
    since_value: str = Query("30d", alias="since"),
    category: str = "",
):
    rows = await workspace.topics(
        account=clean(account), since=since(since_value), category=clean(category), limit=50
    )
    return render(
        request,
        "partials/topics_table.html",
        rows=rows,
        filters={"account": account, "since": since_value, "category": category},
    )


@router.get("/topics/{topic_id}/feedback")
async def topic_feedback(
    request: Request, topic_id: int, since_value: str = Query("", alias="since")
):
    query = (
        select(Feedback, Work)
        .join(Work, Work.id == Feedback.work_id)
        .join(
            FeedbackTopic,
            and_(FeedbackTopic.feedback_id == Feedback.id, FeedbackTopic.topic_id == topic_id),
        )
    )
    start = since(since_value)
    if start:
        query = query.where(Feedback.created_at >= start)
    async with get_sessionmaker()() as session:
        topic = await session.get(Topic, topic_id)
        rows = (
            await session.execute(
                query.order_by(
                    Feedback.likes.desc().nulls_last(), Feedback.created_at.desc()
                ).limit(50)
            )
        ).all()
    return render(request, "partials/topic_feedback.html", topic=topic, rows=rows)


@router.get("/trends")
async def trends_page(request: Request, account: str = ""):
    accounts = await workspace.accounts_overview()
    selected = clean(account) or (str(accounts[0].id) if accounts else None)
    return render(
        request,
        "trends.html",
        accounts=accounts,
        selected=selected,
        since_choices=SINCE_CHOICES,
        **llm_defaults(),
    )


@router.get("/trends/snapshots")
async def snapshot_list(request: Request, account: str):
    async with get_sessionmaker()() as session:
        acc = await repo.find_account(session, account)
        snaps = (
            (
                await session.execute(
                    select(Snapshot)
                    .where(Snapshot.account_id == acc.id, Snapshot.work_id.is_(None))
                    .order_by(Snapshot.period_start.asc().nulls_last(), Snapshot.id)
                )
            )
            .scalars()
            .all()
        )
    return render(
        request, "partials/snapshot_list.html", snapshots=snaps, account=acc, **llm_defaults()
    )


@router.post("/jobs/snapshot")
async def start_snapshot(
    request: Request,
    account: str = Form(...),
    periods: str = Form(""),
    monthly: str = Form(""),
    by_version: bool = Form(False),
    summary: bool = Form(False),
    remember: bool = Form(False),
    provider: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
):
    labels = [
        p.strip() for p in periods.replace("，", ",").replace("\n", ",").split(",") if p.strip()
    ]

    async def run(progress):
        saved, warnings = await workspace.snapshots(
            account=account,
            periods=labels,
            monthly_since=since(monthly),
            by_version=by_version,
            summary=summary,
            remember=remember,
            provider=clean(provider),
            model=clean(model),
            lang=clean(language),
            on_progress=progress,
        )
        return {"snapshots": saved, "warnings": warnings}

    job = jobs(request).submit("snapshot", "生成快照", run, trigger="snapshots-changed")
    return job_card(request, job)


@router.post("/jobs/trend")
async def start_trend(
    request: Request,
    account: str = Form(...),
    labels: Annotated[list[str] | None, Form()] = None,
    narrative: bool = Form(False),
    remember: bool = Form(False),
    provider: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
):
    async def run(progress):
        progress("对比快照…")
        data = await workspace.trend_data(
            account=account,
            labels=labels or None,
            narrative=narrative,
            remember=remember,
            provider=clean(provider),
            model=clean(model),
            lang=clean(language),
        )
        return {
            "data": data,
            "sentiment_chart": charts.trend_sentiment(data),
            "topics_chart": charts.trend_topics(data),
        }

    job = jobs(request).submit("trend", "趋势对比", run)
    return job_card(request, job)


@router.get("/memory/search")
async def memory_search(request: Request, q: str = "", account: str = ""):
    memories = await workspace.recall(q.strip(), account=clean(account)) if q.strip() else []
    return render(request, "partials/memories.html", memories=memories, query=q)
