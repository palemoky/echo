"""ECharts option builders (plain JSON; rendered client-side by static/app.js)."""

from __future__ import annotations

from echo_voc.analyzer_danmaku import DanmakuReport, mmss
from echo_voc.workspace import TrendData

SENTIMENT_COLORS = {"positive": "#2e9e6b", "neutral": "#9aa3ad", "negative": "#d9534f"}


def danmaku_timeline(report: DanmakuReport) -> dict:
    labels = [mmss(i * report.bucket_s * 1000) for i in range(len(report.histogram))]
    areas = [
        [
            {"xAxis": mmss(h.start_ms), "name": h.label or ""},
            {"xAxis": mmss(max(h.start_ms, h.end_ms - report.bucket_s * 1000))},
        ]
        for h in report.hotspots
    ]
    return {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 40, "right": 16, "top": 24, "bottom": 40},
        "xAxis": {"type": "category", "data": labels, "name": "视频时间"},
        "yAxis": {"type": "value", "name": "弹幕数"},
        "series": [
            {
                "type": "bar",
                "data": report.histogram,
                "itemStyle": {"color": "#4f8ef7"},
                "markArea": {"itemStyle": {"color": "rgba(255, 170, 0, 0.18)"}, "data": areas},
            }
        ],
    }


def trend_sentiment(data: TrendData) -> dict:
    series = []
    for key, label in (("positive", "正面"), ("neutral", "中性"), ("negative", "负面")):
        values = []
        for snap in data.snapshots:
            sentiment = snap["stats"].get("sentiment", {})
            total = sum(sentiment.values())
            values.append(round(sentiment.get(key, 0) / total * 100, 1) if total else 0)
        series.append(
            {
                "name": label,
                "type": "line",
                "smooth": True,
                "data": values,
                "itemStyle": {"color": SENTIMENT_COLORS[key]},
            }
        )
    return {
        "tooltip": {"trigger": "axis", "valueFormatter": "percent"},
        "legend": {"top": 0},
        "grid": {"left": 40, "right": 16, "top": 32, "bottom": 32},
        "xAxis": {"type": "category", "data": data.labels},
        "yAxis": {"type": "value", "max": 100, "axisLabel": {"formatter": "{value}%"}},
        "series": series,
    }


def trend_topics(data: TrendData, top: int = 8) -> dict:
    totals: dict[int, tuple[str, int]] = {}
    for snap in data.snapshots:
        for t in snap["stats"].get("topics", []):
            title, n = totals.get(t["id"], (t["title"], 0))
            totals[t["id"]] = (title, n + t["mentions"])
    chosen = sorted(totals.items(), key=lambda kv: kv[1][1], reverse=True)[:top]
    series = []
    for snap in data.snapshots:
        by_id = {t["id"]: t["mentions"] for t in snap["stats"].get("topics", [])}
        series.append(
            {"name": snap["label"], "type": "bar", "data": [by_id.get(tid, 0) for tid, _ in chosen]}
        )
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"top": 0},
        "grid": {"left": 140, "right": 16, "top": 32, "bottom": 24},
        "xAxis": {"type": "value", "name": "提及"},
        "yAxis": {
            "type": "category",
            "inverse": True,
            "data": [title[:18] for _, (title, _) in chosen],
        },
        "series": series,
    }
