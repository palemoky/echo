from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import HTMLResponse

from echo_voc.llm import PROVIDERS
from echo_voc.timeutil import parse_since
from echo_voc.web.jobs import Job, JobManager
from echo_voc.web.templating import render

SINCE_CHOICES = [
    ("7d", "近 7 天"),
    ("30d", "近 30 天"),
    ("90d", "近 90 天"),
    ("1y", "近 1 年"),
    ("", "全部"),
]


def jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def since(value: str | None):
    return parse_since(clean(value))


def int_or_none(value: str | None) -> int | None:
    value = clean(value)
    return int(value) if value else None


def job_card(request: Request, job: Job) -> HTMLResponse:
    return render(request, "partials/job.html", job=job)


def llm_defaults() -> dict:
    return {
        "providers": list(PROVIDERS),
        "provider": os.getenv("ECHO_PROVIDER") or "deepseek",
        "language": os.getenv("ECHO_LANGUAGE") or "",
    }
