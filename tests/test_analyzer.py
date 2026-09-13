from __future__ import annotations

import pytest

from echo_voc.analyzer import Analyzer, make_batches, parse_json
from echo_voc.errors import LLMError
from echo_voc.models import Comment, ThemeCategory
from echo_voc.report import render_markdown

MAP = {
    "sentiments": {
        "1": "negative",
        "2": "negative",
        "3": "positive",
        "4": "neutral",
        "5": "negative",
        "99": "positive",
    },
    "themes": [
        {
            "category": "pain_point",
            "title": "Camera crash",
            "description": "Crash in camera",
            "severity": "5",
            "comments": [1, 5],
        },
        {
            "category": "pain_point",
            "title": "Crash on launch",
            "description": "After update",
            "severity": 4,
            "comments": [2, 2, 42],
        },
        {
            "category": "praise",
            "title": "Dark mode",
            "description": "Users like it",
            "comments": [3],
        },
        {
            "category": "feature_request",
            "title": "PDF export",
            "description": "Export",
            "severity": 3,
            "comments": [4],
        },
    ],
}
REDUCE = {
    "summary": "Stability is the top issue.",
    "themes": [
        {
            "category": "pain_point",
            "title": "Crashes",
            "description": "Various crashes",
            "severity": 5,
            "merged": [0, 1],
        },
        {"category": "praise", "title": "Dark mode", "description": "Loved", "merged": [2, 0]},
    ],
    "suggestions": ["Fix crashes"],
}


async def test_analyze_merges_and_counts_from_real_comments(sample_data, fake_llm_cls):
    llm = fake_llm_cls(map_reply=MAP, reduce_reply=REDUCE)
    report = await Analyzer(llm, language="English").analyze(sample_data)

    # Out-of-range sentiment keys are ignored; counts come from the model's labels.
    assert report.sentiment.model_dump() == {"positive": 1, "neutral": 1, "negative": 3}
    assert report.average_rating == 2.8
    assert report.model == "fake/fake-1"

    crashes, feature, praise = report.themes  # sorted by category, forgotten draft kept
    assert crashes.title == "Crashes"
    assert crashes.mentions == 3  # c0, c4, c1 — duplicates and bogus numbers dropped
    assert [e.comment_id for e in crashes.evidence] == ["c4", "c0", "c1"]  # by likes
    assert feature.category == ThemeCategory.feature_request
    assert feature.title == "PDF export" and feature.severity is None
    assert praise.mentions == 1  # draft 0 is a pain point, so it can't merge into praise

    md = render_markdown(report)
    assert "## Summary" in md and "Fix crashes" in md and "> App crashes every time" in md


async def test_invalid_json_is_retried(sample_data, fake_llm_cls):
    replies = iter(["not json at all", MAP])
    llm = fake_llm_cls(map_reply=lambda _u: next(replies), reduce_reply=REDUCE)
    report = await Analyzer(llm).analyze(sample_data)
    assert report.total_comments == 5
    assert "previous reply was invalid" in llm.calls[1][1]


async def test_all_batches_failing_raises(sample_data, fake_llm_cls):
    llm = fake_llm_cls(map_reply="garbage", reduce_reply=REDUCE)
    with pytest.raises(LLMError):
        await Analyzer(llm).analyze(sample_data)


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! Here it is: {"a": [1, 2]} hope it helps') == {"a": [1, 2]}


def test_make_batches_respects_limits():
    comments = [Comment(id=str(i), platform="x", content="y" * 700) for i in range(40)]
    batches = make_batches(comments)
    assert sum(len(b) for b in batches) == 40
    assert all(len(b) <= 60 for b in batches)
    assert len(batches) > 1


async def test_unplaced_duplicate_drafts_are_grouped(sample_data, fake_llm_cls):
    same = {"category": "pain_point", "title": "Crashes", "description": "d", "severity": 3}
    batch = {
        "sentiments": {},
        "themes": [
            {**same, "comments": [1]},
            {**same, "comments": [2]},
            {**same, "title": "crashes ", "comments": [5]},
        ],
    }
    llm = fake_llm_cls(
        map_reply=batch, reduce_reply={"summary": "s", "themes": [], "suggestions": []}
    )
    report = await Analyzer(llm).analyze(sample_data)
    assert [(t.title, t.mentions) for t in report.themes] == [("Crashes", 3)]
