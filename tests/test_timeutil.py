from datetime import datetime, timezone

import pytest

from echo_voc.timeutil import parse_period, parse_since


def test_parse_since():
    assert parse_since(None) is None
    assert parse_since("2026-08-01") == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert round((datetime.now(timezone.utc) - parse_since("30d")).total_seconds() / 86400) == 30
    with pytest.raises(ValueError):
        parse_since("last month")


def test_parse_period():
    assert parse_period("2026-12") == (
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    start, end = parse_period("2026-08-01..2026-08-15")
    assert (start.day, end.day) == (1, 15)
