from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_REL = re.compile(r"^(\d+)\s*([hdwmy])$", re.I)
_UNITS = {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "y": 365}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_since(value: str | None) -> datetime | None:
    """Parse '30d', '12h', '2w', '6m', '1y' or an ISO date like '2026-08-01'."""
    if not value:
        return None
    value = value.strip()
    if m := _REL.match(value):
        return utcnow() - timedelta(days=int(m[1]) * _UNITS[m[2].lower()])
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid time '{value}': use e.g. 30d, 12h, 2w or 2026-08-01") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_period(label: str) -> tuple[datetime, datetime]:
    """'2026-08' → [Aug 1, Sep 1); '2026' → the year; '2026-08-01..2026-08-15' → that range."""
    if ".." in label:
        start, end = label.split("..", 1)
        return parse_since(start), parse_since(end)
    if re.fullmatch(r"\d{4}-\d{2}", label):
        start = datetime.strptime(label, "%Y-%m").replace(tzinfo=timezone.utc)
        end = (start + timedelta(days=32)).replace(day=1)
        return start, end
    if re.fullmatch(r"\d{4}", label):
        start = datetime(int(label), 1, 1, tzinfo=timezone.utc)
        return start, start.replace(year=start.year + 1)
    raise ValueError(f"Invalid period '{label}': use 2026-08, 2026 or 2026-08-01..2026-08-15")
