from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from echo_voc import __version__
from echo_voc.analyzer_danmaku import mmss

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# html=False: model output and user comments are untrusted; raw HTML is escaped.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_CITE = re.compile(r"\[#(\d+)\]")

CATEGORY_LABELS = {
    "pain_point": "痛点",
    "feature_request": "功能需求",
    "praise": "好评",
    "question": "疑问",
}
KIND_LABELS = {"comment": "评论", "reply": "回复", "review": "评价", "danmaku": "弹幕"}
SENTIMENT_LABELS = {"positive": "正面", "neutral": "中性", "negative": "负面"}
STATUS_LABELS = {"queued": "排队中", "running": "进行中", "done": "完成", "failed": "失败"}
PLATFORM_LABELS = {"bilibili": "B 站", "youtube": "YouTube", "appstore": "App Store"}


def markdown(text: str | None, cite_prefix: str | None = None) -> Markup:
    html = _MD.render(text or "")
    if cite_prefix:
        html = _CITE.sub(
            lambda m: f'<a class="cite" href="#{cite_prefix}-{m[1]}">#{m[1]}</a>', html
        )
    return Markup(html)  # noqa: S704 - produced by markdown-it with HTML disabled


def dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone().strftime(fmt) if value else "-"


def pct(value: float | None, digits: int = 0) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def share(part: int, total: int) -> float:
    return part / total if total else 0.0


for name, fn in {
    "markdown": markdown,
    "dt": dt,
    "pct": pct,
    "mmss": mmss,
    "category": lambda v: CATEGORY_LABELS.get(v, v),
    "kind": lambda v: KIND_LABELS.get(v, v),
    "sentiment": lambda v: SENTIMENT_LABELS.get(v, v or "-"),
    "status": lambda v: STATUS_LABELS.get(v, v),
    "platform": lambda v: PLATFORM_LABELS.get(v, v),
    "num": lambda v: f"{v:,}" if isinstance(v, int) else v,
}.items():
    TEMPLATES.env.filters[name] = fn
TEMPLATES.env.globals.update(version=__version__, share=share)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, template, context, status_code=status_code)
