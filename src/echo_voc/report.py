from __future__ import annotations

from echo_voc.models import Report, ThemeCategory

SECTION_TITLES = {
    ThemeCategory.pain_point: "🔴 Pain points",
    ThemeCategory.feature_request: "💡 Feature requests",
    ThemeCategory.praise: "💚 What users love",
    ThemeCategory.question: "❓ Recurring questions",
}


def _pct(n: int, total: int) -> str:
    return f"{n / total:.0%}" if total else "0%"


def _quote(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(report: Report) -> str:
    s = report.sentiment
    lines = [
        f"# VOC report: {report.title or report.target}",
        "",
        f"- **Platform:** {report.platform}",
        f"- **Source:** {report.target}",
        f"- **Comments analyzed:** {report.total_comments}",
    ]
    if report.average_rating is not None:
        lines.append(f"- **Average rating:** {report.average_rating} / 5")
    lines += [
        f"- **Sentiment:** 👍 {_pct(s.positive, s.total)} · 😐 {_pct(s.neutral, s.total)}"
        f" · 👎 {_pct(s.negative, s.total)}",
        f"- **Model:** {report.model} · {report.generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        "## Summary",
        "",
        report.summary,
        "",
    ]
    if report.suggestions:
        lines += ["## Suggested actions", ""]
        lines += [f"{i}. {text}" for i, text in enumerate(report.suggestions, 1)]
        lines.append("")

    for category, heading in SECTION_TITLES.items():
        themes = [t for t in report.themes if t.category == category]
        if not themes:
            continue
        lines += [f"## {heading}", ""]
        for t in themes:
            badge = f" · severity {t.severity}/5" if t.severity else ""
            lines += [
                f"### {t.title}",
                f"*{t.mentions} mention(s) ({_pct(t.mentions, report.total_comments)}){badge}*",
                "",
                t.description,
                "",
            ]
            for ev in t.evidence:
                meta = []
                if ev.rating is not None:
                    meta.append(f"{ev.rating:g}★")
                if ev.likes:
                    meta.append(f"👍 {ev.likes}")
                suffix = f" — {' · '.join(meta)}" if meta else ""
                lines.append(f"> {_quote(ev.content)}{suffix}")
                lines.append(">")
            if t.evidence:
                lines.pop()
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


SPARKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int], width: int = 60) -> str:
    if not values:
        return ""
    step = max(1, -(-len(values) // width))
    points = [max(values[i : i + step]) for i in range(0, len(values), step)]
    top = max(points) or 1
    return "".join(SPARKS[min(len(SPARKS) - 1, v * len(SPARKS) // (top + 1))] for v in points)


def render_danmaku_markdown(report) -> str:
    from echo_voc.analyzer_danmaku import mmss

    spark = sparkline(report.histogram)
    axis_start, axis_end = "00:00", mmss(report.duration_s * 1000)
    lines = [
        f"# Danmaku report: {report.title or report.url}",
        "",
        f"- **Danmaku analyzed:** {report.total}",
        f"- **Video length:** {mmss(report.duration_s * 1000)}",
        "",
        "## Density timeline",
        "",
        f"`{spark}`",
        f"`{axis_start}{axis_end.rjust(max(len(spark) - len(axis_start), len(axis_end) + 1))}`",
        "",
    ]
    if report.hotspots:
        lines += [
            "## Hotspots",
            "",
            "| Time | Danmaku | Type | What's happening | Top danmaku |",
            "| --- | --- | --- | --- | --- |",
        ]
        for h in report.hotspots:
            time = f"{mmss(h.start_ms)}–{mmss(h.end_ms)}"
            if report.url:
                time = f"[{time}]({report.url}?t={h.start_ms // 1000})"
            examples = " / ".join(e.replace("|", "\\|") for e in h.examples[:3])
            summary = (h.summary or "").replace("|", "\\|")
            lines.append(
                f"| {time} | {h.count} (×{h.ratio}) | {h.label or '-'} | {summary} | {examples} |"
            )
        lines.append("")
    if report.overall:
        overall = render_markdown(report.overall).split("\n", 1)[1]  # drop the H1
        lines += ["## Overall", overall]
    return "\n".join(lines).rstrip() + "\n"
