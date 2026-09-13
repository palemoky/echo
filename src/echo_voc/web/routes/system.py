from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import func, select, text

from echo_voc import workspace
from echo_voc.errors import EchoError
from echo_voc.llm import PROVIDERS
from echo_voc.store.db import database_url, get_sessionmaker
from echo_voc.store.models import Feedback, SyncRun, Topic
from echo_voc.web.routes.common import job_card, jobs
from echo_voc.web.templating import render

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    context: dict = {"jobs": jobs(request).recent(8), "db_error": None}
    try:
        context["accounts"] = await workspace.accounts_overview()
        async with get_sessionmaker()() as session:
            context["topics"] = (await session.execute(select(func.count(Topic.id)))).scalar()
            context["embedded"] = (
                await session.execute(
                    select(func.count(Feedback.id)).where(Feedback.embedding.is_not(None))
                )
            ).scalar()
            context["runs"] = (
                (await session.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(10)))
                .scalars()
                .all()
            )
    except Exception as e:  # show setup help instead of a stack trace  # noqa: BLE001
        context["db_error"] = str(e).splitlines()[0]
    return render(request, "dashboard.html", **context)


@router.get("/jobs")
async def job_list(request: Request):
    return render(request, "partials/job_list.html", jobs=jobs(request).recent(8))


@router.get("/jobs/{job_id}")
async def job_status(request: Request, job_id: int):
    job = jobs(request).get(job_id)
    if job is None:
        raise EchoError("任务不存在（服务重启后任务记录会清空）")
    response = job_card(request, job)
    if job.finished and job.context.get("trigger"):
        response.headers["HX-Trigger"] = job.context["trigger"]
    return response


@router.get("/settings")
async def settings(request: Request):
    checks = {}
    try:
        async with get_sessionmaker()() as session:
            revision = (
                await session.execute(text("select version_num from alembic_version"))
            ).scalar()
        checks["database"] = (True, f"已连接，迁移版本 {revision}")
    except Exception as e:  # noqa: BLE001
        checks["database"] = (
            False,
            f"{str(e).splitlines()[0]} — 运行 docker compose up -d 与 echo-voc db init",
        )

    embedder = (os.getenv("ECHO_EMBEDDER") or "ollama").lower()
    embed_model = os.getenv("ECHO_EMBED_MODEL") or "bge-m3"
    if embedder == "ollama":
        base = os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                tags = (await client.get(f"{base}/api/tags")).json().get("models", [])
            names = {m.get("name", "").split(":")[0] for m in tags}
            ok = embed_model.split(":")[0] in names
            status = "已安装" if ok else f"未安装，运行 ollama pull {embed_model}"
            checks["embedder"] = (ok, f"Ollama 已运行；{embed_model} {status}")
        except httpx.HTTPError:
            checks["embedder"] = (False, f"无法连接 Ollama（{base}），运行 ollama serve")
    else:
        checks["embedder"] = (True, f"{embedder} / {embed_model}")

    try:
        import mem0  # noqa: F401

        memory_on = os.getenv("ECHO_MEMORY", "on").lower() not in {"off", "0", "false"}
        checks["memory"] = (
            memory_on,
            "mem0 已启用" if memory_on else "已通过 ECHO_MEMORY=off 关闭",
        )
    except ImportError:
        checks["memory"] = (False, "未安装 mem0（uv sync --extra memory）")

    provider = (os.getenv("ECHO_PROVIDER") or "deepseek").lower()
    preset = PROVIDERS.get(provider)
    llm = {
        "provider": provider,
        "model": os.getenv("ECHO_MODEL") or (preset.default_model if preset else "-"),
        "base_url": os.getenv("ECHO_BASE_URL") or (preset.base_url if preset else None) or "默认",
        "key_env": preset.api_key_env if preset else None,
        "key_set": bool(
            (preset and preset.api_key_env and os.getenv(preset.api_key_env))
            or os.getenv("ECHO_API_KEY")
            or (preset and preset.api_key_env is None)
        ),
    }
    credentials = {
        "YOUTUBE_API_KEY": bool(os.getenv("YOUTUBE_API_KEY")),
        "BILIBILI_COOKIE": bool(os.getenv("BILIBILI_COOKIE")),
    }
    return render(
        request,
        "settings.html",
        checks=checks,
        llm=llm,
        credentials=credentials,
        database=database_url().split("@")[-1],
        memory_infer=os.getenv("ECHO_MEMORY_INFER", "false"),
    )
