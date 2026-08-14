"""Score parsing and formatting.

Scores come in as a string option rather than a Discord integer option: people
type "12,345,678", and an integer option rejects the commas outright with a
client-side error the bot never gets a chance to explain.

The parser refuses to guess. Anything ambiguous is rejected rather than
normalized, because the failure mode of guessing is a *silently multiplied
score* — and these numbers decide who gets a trophy.
"""

from __future__ import annotations

import re

# Separators that can only ever be decoration: underscores, apostrophes, and
# whitespace including the non-breaking, narrow and thin spaces that phone
# keyboards and some score-display apps emit.
_DECORATION = str.maketrans("", "", "_' \t   ")

# A '.' or ',' is only safe to strip when it unambiguously groups thousands:
# 1-3 digits, then groups of exactly 3, with one consistent separator
# throughout. The backreference is what rejects mixed "1,234.567".
_GROUPED = re.compile(r"\d{1,3}(?P<sep>[.,])\d{3}(?:(?P=sep)\d{3})*")

# A trailing group of 1 or 2 digits after a separator reads as a decimal
# fraction. Worth its own error message, since stripping it would multiply the
# score by 10 or 100.
_DECIMAL_LOOKING = re.compile(r"\d[\d.,]*[.,]\d{1,2}")

# Generous upper bound. The highest-scoring real machines top out around a
# trillion; a quadrillion means someone leaned on the keyboard.
MAX_SCORE = 10**15


class ScoreFormatError(ValueError):
    """Raised when a submitted score can't be read as a positive integer."""


def parse_score(raw: str) -> int:
    """Parse a user-entered score.

    Accepts "12345678", "12,345,678", "12 345 678", "12.345.678".
    Rejects anything else, including decimals and inconsistent grouping.
    """
    cleaned = (raw or "").strip().translate(_DECORATION)
    if not cleaned:
        raise ScoreFormatError("Enter your score, for example `12,345,678`.")

    # isascii() matters: "²" and "١٢٣" both pass isdigit(), and only one of
    # them survives int().
    if cleaned.isascii() and cleaned.isdigit():
        digits = cleaned
    elif _GROUPED.fullmatch(cleaned):
        digits = cleaned.replace(",", "").replace(".", "")
    elif _DECIMAL_LOOKING.fullmatch(cleaned):
        raise ScoreFormatError(
            f"`{raw.strip()}` looks like it has a decimal point, and dropping it "
            "would multiply your score. Pinball scores are whole numbers — enter "
            "the points exactly as the machine shows them."
        )
    else:
        raise ScoreFormatError(
            f"`{raw.strip()}` isn't a whole number. Enter digits only, either "
            "plain or grouped in threes — `12345678` or `12,345,678`."
        )

    value = int(digits)
    if value == 0:
        raise ScoreFormatError("A score of 0 can't take the crown.")
    if value > MAX_SCORE:
        raise ScoreFormatError(
            f"`{format_score(value)}` is impossibly high — check for an extra digit."
        )
    return value


def format_score(value: int) -> str:
    """Render a score with thousands separators: 12345678 -> '12,345,678'."""
    return f"{value:,}"
