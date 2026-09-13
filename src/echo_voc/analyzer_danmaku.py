"""Danmaku analysis: density timeline → hotspots → LLM explanations + overall themes."""

from __future__ import annotations

import random
import statistics
from collections import Counter
from collections.abc import Callable

from pydantic import BaseModel, Field

from echo_voc import prompts
from echo_voc.analyzer import DEFAULT_LANGUAGE, Analyzer, ask_json
from echo_voc.llm.base import LLM
from echo_voc.models import Comment, FetchResult, Report

SAMPLES_PER_HOTSPOT = 60
OVERALL_SAMPLE = 800


class Hotspot(BaseModel):
    start_ms: int
    end_ms: int
    count: int
    ratio: float  # count relative to the average bucket
    label: str | None = None
    summary: str | None = None
    examples: list[str] = Field(default_factory=list)


class DanmakuReport(BaseModel):
    title: str | None
    url: str | None = None
    total: int
    duration_s: int
    bucket_s: int
    histogram: list[int]
    hotspots: list[Hotspot]
    overall: Report | None = None


class _HotspotItem(BaseModel):
    index: int
    label: str = "other"
    summary: str = ""


class _HotspotReply(BaseModel):
    hotspots: list[_HotspotItem] = Field(default_factory=list)


def histogram(danmaku: list[Comment], duration_s: int, bucket_s: int) -> list[int]:
    buckets = [0] * max(1, -(-duration_s // bucket_s))
    for d in danmaku:
        if d.progress_ms is None:
            continue
        buckets[min(len(buckets) - 1, max(0, d.progress_ms // 1000 // bucket_s))] += 1
    return buckets


def find_hotspots(hist: list[int], bucket_s: int, top: int = 6, z: float = 1.5) -> list[Hotspot]:
    """Buckets well above average, adjacent ones merged, strongest first."""
    if not hist or not any(hist):
        return []
    mean = statistics.fmean(hist)
    threshold = mean + z * (statistics.pstdev(hist) if len(hist) > 1 else 0)
    spans: list[list[int]] = []
    for i, n in enumerate(hist):
        if n > 0 and n >= threshold:
            if spans and spans[-1][1] == i - 1:
                spans[-1][1] = i
            else:
                spans.append([i, i])
    if not spans:  # flat distribution: only the few busiest buckets are worth a look
        busiest = sorted(range(len(hist)), key=hist.__getitem__, reverse=True)
        spans = [[i, i] for i in busiest[: min(top, max(1, len(hist) // 5))]]
    hotspots = [
        Hotspot(
            start_ms=a * bucket_s * 1000,
            end_ms=(b + 1) * bucket_s * 1000,
            count=sum(hist[a : b + 1]),
            ratio=round(max(hist[a : b + 1]) / mean, 1) if mean else 0,
        )
        for a, b in spans
    ]
    hotspots.sort(key=lambda h: h.count, reverse=True)
    return sorted(hotspots[:top], key=lambda h: h.start_ms)


def _dedupe(texts: list[str]) -> list[tuple[str, int]]:
    return Counter(" ".join(t.split()) for t in texts if t.strip()).most_common()


def mmss(ms: int) -> str:
    s = ms // 1000
    return (
        f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
        if s >= 3600
        else f"{s // 60:02d}:{s % 60:02d}"
    )


class DanmakuAnalyzer:
    def __init__(
        self,
        llm: LLM,
        language: str = DEFAULT_LANGUAGE,
        bucket_s: int = 30,
        overall: bool = True,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.language = language
        self.bucket_s = bucket_s
        self.overall = overall
        self.progress = on_progress or (lambda _msg: None)

    async def analyze(
        self,
        danmaku: list[Comment],
        *,
        title: str | None,
        url: str | None = None,
        duration_s: int | None = None,
        seed: int = 0,
    ) -> DanmakuReport:
        if not danmaku:
            raise ValueError("No danmaku to analyze")
        duration = duration_s or max((d.progress_ms or 0) for d in danmaku) // 1000 + 1
        hist = histogram(danmaku, duration, self.bucket_s)
        hotspots = find_hotspots(hist, self.bucket_s)
        rng = random.Random(seed)

        blocks = []
        for i, h in enumerate(hotspots):
            texts = [
                d.content
                for d in danmaku
                if d.progress_ms is not None and h.start_ms <= d.progress_ms < h.end_ms
            ]
            common = _dedupe(texts)[:SAMPLES_PER_HOTSPOT]
            h.examples = [t for t, _ in common[:5]]
            sample = "\n".join(f"  - {t}" + (f" ×{n}" if n > 1 else "") for t, n in common)
            blocks.append(f"[{i}] {mmss(h.start_ms)}-{mmss(h.end_ms)}, {h.count} danmaku\n{sample}")

        if hotspots:
            self.progress(f"Explaining {len(hotspots)} hotspots")
            reply: _HotspotReply = await ask_json(
                self.llm,
                prompts.DANMAKU_HOTSPOT_SYSTEM.format(language=self.language),
                prompts.DANMAKU_HOTSPOT_USER.format(
                    title=title or "-", hotspots="\n\n".join(blocks)
                ),
                _HotspotReply,
            )
            for item in reply.hotspots:
                if 0 <= item.index < len(hotspots):
                    hotspots[item.index].label = item.label
                    hotspots[item.index].summary = item.summary

        overall = None
        if self.overall:
            # Collapse repeats ("哈哈哈" ×300) so the model sees variety, then sample.
            counted = _dedupe([d.content for d in danmaku])
            if len(counted) > OVERALL_SAMPLE:
                head = counted[: OVERALL_SAMPLE // 2]
                counted = head + rng.sample(counted[OVERALL_SAMPLE // 2 :], OVERALL_SAMPLE // 2)
            comments = [
                Comment(
                    id=f"dm{i}",
                    platform="bilibili",
                    kind="danmaku",
                    content=t + (f"（×{n}）" if n > 1 else ""),
                )
                for i, (t, n) in enumerate(counted)
            ]
            overall = await Analyzer(
                self.llm, language=self.language, on_progress=self.progress
            ).analyze(
                FetchResult(
                    platform="bilibili", target=url or title or "", title=title, comments=comments
                )
            )

        return DanmakuReport(
            title=title,
            url=url,
            total=len(danmaku),
            duration_s=duration,
            bucket_s=self.bucket_s,
            histogram=hist,
            hotspots=hotspots,
            overall=overall,
        )
