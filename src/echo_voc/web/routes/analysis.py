from __future__ import annotations

from fastapi import APIRouter, Form, Request

from echo_voc import offline, service
from echo_voc.web import charts
from echo_voc.web.routes.common import clean, int_or_none, job_card, jobs, llm_defaults, since
from echo_voc.web.templating import render

router = APIRouter()


@router.get("/analyze")
async def analyze_page(request: Request):
    return render(request, "analyze.html", **llm_defaults())


@router.post("/jobs/analyze")
async def start_analyze(
    request: Request,
    url: str = Form(...),
    limit: int = Form(200),
    provider: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
):
    async def run(progress):
        progress("正在抓取评论…")
        data = await service.fetch_comments(url.strip(), max(1, min(limit, 1000)))
        progress(f"已抓取 {len(data.comments)} 条，开始分析…")
        return await service.analyze(
            data,
            provider=clean(provider),
            model=clean(model),
            language=clean(language),
            on_progress=progress,
        )

    job = jobs(request).submit("report", f"实时分析：{url.strip()[:60]}", run)
    return job_card(request, job)


@router.post("/jobs/report")
async def start_report(
    request: Request,
    account: str = Form(""),
    work: str = Form(""),
    since_value: str = Form("", alias="since"),
    limit: int = Form(500),
    order: str = Form("likes"),
    provider: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
):
    job = jobs(request).submit(
        "report",
        "库内报告",
        lambda progress: offline.report_from_db(
            account=clean(account),
            work=clean(work),
            since=since(since_value),
            limit=limit,
            order=order,
            provider=clean(provider),
            model=clean(model),
            language=clean(language),
            on_progress=progress,
        ),
    )
    return job_card(request, job)


@router.post("/jobs/danmaku")
async def start_danmaku(
    request: Request,
    work: str = Form(...),
    bucket: str = Form("30"),
    overall: bool = Form(False),
    provider: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
):
    async def run(progress):
        report = await offline.danmaku_report_from_db(
            work=work,
            bucket_s=int_or_none(bucket) or 30,
            overall=overall,
            provider=clean(provider),
            model=clean(model),
            language=clean(language),
            on_progress=progress,
        )
        return {"report": report, "chart": charts.danmaku_timeline(report)}

    job = jobs(request).submit("danmaku", "弹幕分析", run)
    return job_card(request, job)
