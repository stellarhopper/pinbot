"""Duration parsing for scheduling the end of a tournament.

Accepts the shapes an organizer would actually type on a phone: "36h", "2d",
"2d 4h 30m", "90m", "1w", "in 3 days".
"""

from __future__ import annotations

import re
from datetime import timedelta

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

_ALIASES = {
    "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "day": "d", "days": "d",
    "week": "w", "weeks": "w",
}

# Optional separator ("2d 4h", "2d, 4h", "2d and 4h"), then amount + unit.
_TOKEN = re.compile(r"[\s,]*(?:and[\s,]*)?(\d+)\s*([a-z]+)", re.IGNORECASE)

MAX_SECONDS = 365 * 86400


class DurationFormatError(ValueError):
    """Raised when a duration string can't be understood."""


def parse_duration(raw: str) -> timedelta:
    """Parse a duration into a timedelta.

    Raises DurationFormatError for anything unparseable, zero, or negative.
    """
    text = (raw or "").strip().lower()
    if text.startswith("in "):
        text = text[3:].strip()
    if not text:
        raise DurationFormatError(
            "Enter a duration like `36h`, `2d`, or `2d 4h 30m`."
        )

    total = 0
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise DurationFormatError(
                f"`{raw.strip()}` isn't a duration I understand. Try `36h`, "
                "`2d`, `90m`, or `2d 4h 30m`."
            )
        amount, unit = match.group(1), match.group(2)
        unit = _ALIASES.get(unit, unit)
        if unit not in _UNIT_SECONDS:
            raise DurationFormatError(
                f"`{match.group(2)}` isn't a unit I know. Use w, d, h, m, or s."
            )
        total += int(amount) * _UNIT_SECONDS[unit]
        pos = match.end()

    if total <= 0:
        raise DurationFormatError("That duration is zero — pick something longer.")
    if total > MAX_SECONDS:
        raise DurationFormatError("That's over a year out. Pick something shorter.")
    return timedelta(seconds=total)


def format_duration(delta: timedelta) -> str:
    """Render a timedelta as '2d 4h 30m', for echoing a parsed value back."""
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "0m"
    parts: list[str] = []
    for unit in ("w", "d", "h", "m"):
        size = _UNIT_SECONDS[unit]
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    if seconds and not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)
