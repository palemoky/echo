from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from echo_voc import prompts
from echo_voc.errors import LLMError
from echo_voc.llm.base import LLM
from echo_voc.models import (
    Comment,
    Evidence,
    FetchResult,
    Report,
    Sentiment,
    SentimentStats,
    Theme,
    ThemeCategory,
)

log = logging.getLogger(__name__)

MAX_COMMENT_CHARS = 800
BATCH_CHARS = 12_000
BATCH_MAX_COMMENTS = 60
EVIDENCE_PER_THEME = 3
DEFAULT_LANGUAGE = "the same language as the majority of the comments"
CATEGORY_ORDER = list(ThemeCategory)


# ---- LLM response schemas -------------------------------------------------------------------


def _coerce_severity(v: Any) -> int | None:
    try:
        return min(5, max(1, int(v))) if v is not None else None
    except (TypeError, ValueError):
        return None


class _DraftTheme(BaseModel):
    category: ThemeCategory
    title: str
    description: str = ""
    severity: int | None = None
    comments: list[int] = Field(default_factory=list)

    _sev = field_validator("severity", mode="before")(_coerce_severity)


class _BatchResult(BaseModel):
    sentiments: dict[str, Sentiment] = Field(default_factory=dict)
    themes: list[_DraftTheme] = Field(default_factory=list)


class _MergedTheme(BaseModel):
    category: ThemeCategory
    title: str
    description: str = ""
    severity: int | None = None
    merged: list[int] = Field(default_factory=list)

    _sev = field_validator("severity", mode="before")(_coerce_severity)


class _ReduceResult(BaseModel):
    summary: str
    themes: list[_MergedTheme] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


# ---- helpers --------------------------------------------------------------------------------


def parse_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences or surrounding prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def make_batches(comments: list[Comment]) -> list[list[Comment]]:
    batches: list[list[Comment]] = [[]]
    size = 0
    for c in comments:
        length = min(len(c.content), MAX_COMMENT_CHARS) + len(c.title or "")
        if batches[-1] and (size + length > BATCH_CHARS or len(batches[-1]) >= BATCH_MAX_COMMENTS):
            batches.append([])
            size = 0
        batches[-1].append(c)
        size += length
    return [b for b in batches if b]


def _format_comment(n: int, c: Comment) -> str:
    meta = []
    if c.rating is not None:
        meta.append(f"{c.rating:g}★")
    if c.likes:
        meta.append(f"{c.likes} likes")
    text = " ".join(c.content.split())[:MAX_COMMENT_CHARS]
    if c.title:
        text = f"{c.title} — {text}"
    return f"[{n}] ({', '.join(meta) or '-'}) {text}"


# ---- analyzer -------------------------------------------------------------------------------


@dataclass
class BatchExtraction:
    """Per-batch map output, with model-assigned numbers resolved back to real comments."""

    batch: list[Comment]
    sentiments: list[Sentiment | None]  # aligned with ``batch``
    drafts: list[tuple[_DraftTheme, list[Comment]]]


async def ask_json(llm: LLM, system: str, user: str, schema: type[BaseModel], retries: int = 1):
    """Ask for JSON matching ``schema``; on invalid output, retry once with the error attached."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        prompt = user
        if last_error is not None:
            prompt += f"\n\nYour previous reply was invalid ({last_error}). Return valid JSON only."
        text = await llm.complete(system, prompt, json_mode=True)
        try:
            return schema.model_validate(parse_json(text))
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            log.debug("Invalid LLM JSON (attempt %d): %s", attempt + 1, text[:500])
    raise LLMError(f"Model returned invalid JSON after {retries + 1} attempts: {last_error}")


async def extract_batch(llm: LLM, batch: list[Comment], language: str) -> BatchExtraction:
    user = prompts.MAP_USER.format(
        comments="\n".join(_format_comment(i, c) for i, c in enumerate(batch, 1))
    )
    system = prompts.MAP_SYSTEM.format(language=language)
    result: _BatchResult = await ask_json(llm, system, user, _BatchResult)
    sentiments: list[Sentiment | None] = [None] * len(batch)
    for key, value in result.sentiments.items():
        if key.isdigit() and 1 <= int(key) <= len(batch):
            sentiments[int(key) - 1] = value
    drafts = []
    for theme in result.themes:
        refs = [batch[n - 1] for n in dict.fromkeys(theme.comments) if 1 <= n <= len(batch)]
        if refs:
            drafts.append((theme, refs))
    return BatchExtraction(batch, sentiments, drafts)


async def extract_all(
    llm: LLM,
    comments: list[Comment],
    language: str = DEFAULT_LANGUAGE,
    concurrency: int = 4,
    on_progress: Callable[[str], None] | None = None,
) -> list[BatchExtraction]:
    """Run the map step over all comments concurrently; failed batches are skipped."""
    progress = on_progress or (lambda _msg: None)
    batches = make_batches(comments)
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def run(batch: list[Comment]) -> BatchExtraction | None:
        nonlocal done
        async with sem:
            try:
                result = await extract_batch(llm, batch, language)
            except LLMError as e:
                log.warning("Batch failed, skipping: %s", e)
                result = None
            done += 1
            progress(f"Batch {done}/{len(batches)} done")
            return result

    results = await asyncio.gather(*(run(b) for b in batches))
    if batches and all(r is None for r in results):
        raise LLMError("All analysis batches failed; check the provider, model and API key")
    return [r for r in results if r is not None]


class Analyzer:
    def __init__(
        self,
        llm: LLM,
        language: str = DEFAULT_LANGUAGE,
        concurrency: int = 4,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.language = language
        self.concurrency = concurrency
        self.on_progress = on_progress or (lambda _msg: None)

    async def analyze(self, data: FetchResult) -> Report:
        comments = [c for c in data.comments if c.content.strip()]
        if not comments:
            raise ValueError("No non-empty comments to analyze")

        self.on_progress(f"Analyzing {len(comments)} comments")
        extractions = await extract_all(
            self.llm, comments, self.language, self.concurrency, self.on_progress
        )

        sentiment = SentimentStats()
        drafts: list[tuple[_DraftTheme, list[Comment]]] = []
        for extraction in extractions:
            for value in extraction.sentiments:
                if value is not None:
                    setattr(sentiment, value.value, getattr(sentiment, value.value) + 1)
            drafts.extend(extraction.drafts)

        ratings = [c.rating for c in comments if c.rating is not None]
        average_rating = round(sum(ratings) / len(ratings), 2) if ratings else None

        self.on_progress(f"Merging {len(drafts)} draft themes")
        reduced = await self._reduce(data, len(comments), sentiment, average_rating, drafts)

        return Report(
            platform=data.platform,
            target=data.target,
            title=data.title,
            model=f"{self.llm.provider}/{self.llm.model}",
            total_comments=len(comments),
            sentiment=sentiment,
            average_rating=average_rating,
            summary=reduced.summary,
            themes=self._build_themes(reduced, drafts),
            suggestions=reduced.suggestions,
        )

    async def _reduce(
        self,
        data: FetchResult,
        total: int,
        sentiment: SentimentStats,
        average_rating: float | None,
        drafts: list[tuple[_DraftTheme, list[Comment]]],
    ) -> _ReduceResult:
        lines = [
            f"[{i}] {t.category.value} | {t.title} | {len(refs)} | {t.severity or '-'}"
            f" — {t.description}"
            for i, (t, refs) in enumerate(drafts)
        ]
        system = prompts.REDUCE_SYSTEM.format(
            title=data.title or data.target, platform=data.platform, language=self.language
        )
        user = prompts.REDUCE_USER.format(
            total=total,
            sentiment=sentiment.model_dump(),
            rating=f"average rating {average_rating}/5" if average_rating else "no ratings",
            drafts="\n".join(lines) or "(none)",
        )
        return await ask_json(self.llm, system, user, _ReduceResult)

    def _build_themes(
        self, reduced: _ReduceResult, drafts: list[tuple[_DraftTheme, list[Comment]]]
    ) -> list[Theme]:
        themes: list[Theme] = []
        claimed: set[int] = set()

        def build(category, title, description, severity, indexes: list[int]) -> Theme | None:
            refs: dict[str, Comment] = {}
            for i in indexes:
                for c in drafts[i][1]:
                    refs.setdefault(c.id, c)
            if not refs:
                return None
            top = sorted(refs.values(), key=lambda c: c.likes or 0, reverse=True)
            return Theme(
                category=category,
                title=title,
                description=description,
                severity=severity if category == ThemeCategory.pain_point else None,
                mentions=len(refs),
                evidence=[
                    Evidence(comment_id=c.id, content=c.content, likes=c.likes, rating=c.rating)
                    for c in top[:EVIDENCE_PER_THEME]
                ],
            )

        for t in reduced.themes:
            indexes = [
                i
                for i in dict.fromkeys(t.merged)
                if 0 <= i < len(drafts) and i not in claimed and drafts[i][0].category == t.category
            ]
            claimed.update(indexes)
            if theme := build(t.category, t.title, t.description, t.severity, indexes):
                themes.append(theme)

        # Keep drafts the model forgot to place instead of silently dropping them.
        for i, (d, _) in enumerate(drafts):
            if i not in claimed:
                if theme := build(d.category, d.title, d.description, d.severity, [i]):
                    themes.append(theme)

        themes.sort(
            key=lambda t: (CATEGORY_ORDER.index(t.category), -t.mentions, -(t.severity or 0))
        )
        return themes
