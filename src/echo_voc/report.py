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
