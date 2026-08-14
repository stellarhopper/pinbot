"""Score parsing: accept what people actually type, reject what isn't a score."""

from __future__ import annotations

import pytest

from bot.scoring import MAX_SCORE, ScoreFormatError, format_score, parse_score


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12345678", 12_345_678),
        ("12,345,678", 12_345_678),      # the shape a phone keyboard encourages
        ("12 345 678", 12_345_678),
        ("12.345.678", 12_345_678),      # a dot can only be a separator here
        ("12_345_678", 12_345_678),
        ("  1  ", 1),
        ("000123", 123),
        ("999999999999", 999_999_999_999),
    ],
)
def test_accepts_human_formatting(raw, expected):
    assert parse_score(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "-5",
        "0",
        "1.2e9",
        "",
        "   ",
        "12,34a",
        "1 million",
        "²",          # isdigit() is True but int() would fail
        "١٢٣",         # non-ASCII digits
        "9" * 20,     # over MAX_SCORE
        "1,234.567",  # mixed separators — which one groups thousands?
        "12,34",      # not a thousands group
        "1,23,456",   # lakh grouping: unambiguous to a human, not to this parser
    ],
)
def test_rejects_non_scores(raw):
    with pytest.raises(ScoreFormatError):
        parse_score(raw)


@pytest.mark.parametrize("raw", ["1568395620.0", "1,568,395,620.0", "1.5", "999.99", "0.5"])
def test_decimals_are_refused_not_silently_multiplied(raw):
    """Stripping a decimal point multiplies the score by 10 or 100.

    Regression test: a real submission of 1,568,395,620.0 was recorded as
    15,683,956,200 and wrongly took the crown. In a tournament with trophies,
    refusing an ambiguous score always beats guessing at one.
    """
    with pytest.raises(ScoreFormatError, match="decimal point"):
        parse_score(raw)


def test_error_messages_are_actionable():
    with pytest.raises(ScoreFormatError, match="whole number"):
        parse_score("abc")
    with pytest.raises(ScoreFormatError, match="crown"):
        parse_score("0")
    with pytest.raises(ScoreFormatError, match="impossibly high"):
        parse_score(str(MAX_SCORE + 1))


def test_max_score_boundary_is_inclusive():
    assert parse_score(str(MAX_SCORE)) == MAX_SCORE


def test_format_score_round_trips():
    assert format_score(12_345_678) == "12,345,678"
    assert parse_score(format_score(987_654_321)) == 987_654_321
