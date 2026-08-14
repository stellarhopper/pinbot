"""Duration parsing for /tournament start and /tournament extend."""

from __future__ import annotations

import pytest

from bot.durations import DurationFormatError, format_duration, parse_duration

HOUR = 3600
DAY = 86400


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("36h", 36 * HOUR),
        ("2d", 2 * DAY),
        ("2d 4h 30m", 2 * DAY + 4 * HOUR + 30 * 60),
        ("90m", 90 * 60),
        ("1w", 7 * DAY),
        ("45s", 45),
        ("1w2d", 9 * DAY),
        ("in 3 days", 3 * DAY),
        ("2 days and 4 hours", 2 * DAY + 4 * HOUR),
        ("2D 4H", 2 * DAY + 4 * HOUR),
        ("1 hour, 30 minutes", HOUR + 30 * 60),
    ],
)
def test_accepts_the_shapes_people_type(raw, seconds):
    assert parse_duration(raw).total_seconds() == seconds


@pytest.mark.parametrize(
    "raw",
    [
        "soon",
        "-1h",
        "0m",
        "",
        "   ",
        "2h30",      # 30 what?
        "5x",        # unknown unit
        "abc",
        "400d",      # beyond the sanity ceiling
        "h",
    ],
)
def test_rejects_unparseable_or_useless_durations(raw):
    with pytest.raises(DurationFormatError):
        parse_duration(raw)


def test_error_messages_name_the_valid_units():
    with pytest.raises(DurationFormatError, match="w, d, h, m, or s"):
        parse_duration("5x")
    with pytest.raises(DurationFormatError, match="zero"):
        parse_duration("0m")


@pytest.mark.parametrize(
    ("raw", "rendered"),
    [
        ("36h", "1d 12h"),
        ("2d 4h 30m", "2d 4h 30m"),
        ("90m", "1h 30m"),
        ("1w", "1w"),
        ("45s", "45s"),
    ],
)
def test_format_duration_is_readable(raw, rendered):
    assert format_duration(parse_duration(raw)) == rendered
