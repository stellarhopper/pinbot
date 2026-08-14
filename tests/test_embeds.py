"""Content of the standings and announcement embeds.

Exercised directly rather than through a command, because the interesting part
is what the text says — the margin, how long a crown has been held, how far the
chasers are off — and each of those is a separate small assertion.
"""

from __future__ import annotations

import pytest

from bot import embeds
from bot.store import Submission, Table, TableStats, Tournament, now

AVATAR = "https://cdn.discordapp.com/avatars/1/hash.png"


def sub(
    score: int,
    *,
    id: int = 1,
    user_id: int = 1,
    display: str = "alice",
    avatar: str | None = AVATAR,
    age_seconds: int = 0,
    note: str | None = None,
    jump_url: str | None = "https://discord.com/channels/1/2/3",
    voided: bool = False,
) -> Submission:
    return Submission(
        id=id,
        guild_id=500,
        tournament_id=1,
        table_id=1,
        user_id=user_id,
        user_display=display,
        user_avatar=avatar,
        score=score,
        note=note,
        created_at=now() - age_seconds,
        proof_channel_id=900,
        proof_message_id=7001,
        proof_jump_url=jump_url,
        proof_filename="proof.jpg",
        voided_at=now() if voided else None,
        voided_by=99 if voided else None,
        void_reason="cheating" if voided else None,
        vision_score=None,
        vision_verdict=None,
    )


def table(name: str = "Godzilla") -> Table:
    return Table(id=1, guild_id=500, name=name, active=True, sort_order=1, created_at=0)


def stats(attempts: int = 3, players: int = 2, challenges: int = 0) -> TableStats:
    return TableStats(attempts=attempts, players=players, challenges=challenges)


def tournament(*, open_: bool = True, ends_at: int | None = None) -> Tournament:
    return Tournament(
        id=1,
        guild_id=500,
        name="Spring Open 2026",
        started_at=now() - 3600,
        started_by=99,
        ends_at=ends_at,
        ended_at=None if open_ else now(),
        ended_by=None if open_ else 99,
    )


# ------------------------------------------------------------- table accents

def test_each_table_gets_a_stable_accent_colour():
    assert embeds.table_color("Godzilla") == embeds.table_color("godzilla")
    assert embeds.table_color("Godzilla").value in embeds._ACCENTS


def test_accent_colours_survive_a_restart():
    """crc32, not hash(): hash() is salted per process, so a table's colour
    would change on every restart of the bot."""
    assert embeds.table_color("Godzilla").value == 0x00A8FC


def test_the_real_tables_are_visually_distinguishable():
    names = ("Stranger Things", "King Kong", "Godzilla", "Attack From Mars")
    assert len({embeds.table_color(n).value for n in names}) == len(names)


# ---------------------------------------------------------- summary standings

def test_summary_shows_the_king_with_their_avatar():
    embed = embeds.table_summary_embed(
        table(), sub(3_127_605_730), None, stats(), None
    )
    assert embed.author.name == "alice — king of the hill"
    assert embed.author.icon_url == AVATAR
    assert "3,127,605,730" in embed.description
    assert "<@1>" in embed.description


def test_summary_survives_a_player_with_no_avatar():
    embed = embeds.table_summary_embed(
        table(), sub(1_000, avatar=None), None, stats(), None
    )
    assert embed.author.name.startswith("alice")
    assert embed.author.icon_url is None


def test_summary_reports_how_long_the_crown_has_been_held():
    embed = embeds.table_summary_embed(
        table(), sub(1_000, age_seconds=8100), None, stats(), None
    )
    assert "held for **2h 15m**" in embed.description


def test_a_freshly_taken_crown_reads_as_just_now():
    embed = embeds.table_summary_embed(table(), sub(1_000, age_seconds=5), None, stats(), None)
    assert "just now" in embed.description


def test_summary_states_the_margin_over_the_runner_up():
    king = sub(9_000_000, id=1)
    second = sub(5_000_000, id=2, user_id=2, display="bob")
    embed = embeds.table_summary_embed(table(), king, second, stats(), None)
    assert "leading by 4,000,000" in embed.description


def test_a_sole_score_is_marked_unchallenged():
    embed = embeds.table_summary_embed(table(), sub(9_000_000), None, stats(), None)
    assert "unchallenged" in embed.description


def test_a_tie_explains_why_the_earlier_attempt_holds():
    king = sub(9_000_000, id=1)
    tied = sub(9_000_000, id=2, user_id=2)
    embed = embeds.table_summary_embed(table(), king, tied, stats(), None)
    assert "tied on score" in embed.description


def test_summary_footer_counts_attempts_players_and_challenges():
    embed = embeds.table_summary_embed(
        table(), sub(1_000), None, stats(attempts=7, players=4, challenges=3), None
    )
    assert "7 attempt(s)" in embed.footer.text
    assert "4 player(s)" in embed.footer.text
    assert "survived 3 challenge(s)" in embed.footer.text


def test_challenges_are_omitted_when_there_have_been_none():
    embed = embeds.table_summary_embed(
        table(), sub(1_000), None, stats(challenges=0), None
    )
    assert "challenge" not in embed.footer.text


def test_an_unplayed_table_invites_a_score():
    embed = embeds.table_summary_embed(table(), None, None, stats(0, 0, 0), None)
    assert "wide open" in embed.description
    assert embed.color == embeds.GREY, "an empty table shouldn't look like a claimed one"
    assert "first" in embed.footer.text


def test_summary_uses_a_fresh_proof_url_for_its_thumbnail():
    embed = embeds.table_summary_embed(
        table(), sub(1_000), None, stats(), "https://cdn.discordapp.com/fresh.png"
    )
    assert embed.thumbnail.url == "https://cdn.discordapp.com/fresh.png"
    assert "view proof" in embed.description


# ----------------------------------------------------------- detail standings

def test_detail_breaks_out_the_headline_numbers():
    king = sub(9_000_000, id=1, age_seconds=3600)
    second = sub(5_000_000, id=2, user_id=2, display="bob")
    embed = embeds.table_detail_embed(
        table(), king, [king, second], stats(challenges=4), None
    )
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Held for"] == "1h"
    assert fields["Margin"] == "4,000,000"
    assert fields["Challenges survived"] == "4"


def test_detail_tells_each_chaser_exactly_what_it_takes():
    king = sub(9_000_000, id=1)
    second = sub(5_000_000, id=2, user_id=2, display="bob")
    third = sub(1_000_000, id=3, user_id=3, display="carl")
    embed = embeds.table_detail_embed(
        table(), king, [king, second, third], stats(), None
    )
    chasing = next(f for f in embed.fields if f.name == "Chasing").value
    assert embeds.MEDALS[1] in chasing and embeds.MEDALS[2] in chasing
    assert "needs +4,000,001" in chasing, "one point past a tie, since ties don't win"
    assert "needs +8,000,001" in chasing
    assert "9,000,000" not in chasing, "the king isn't chasing anyone"


def test_detail_omits_the_chasing_field_when_nobody_is_chasing():
    king = sub(9_000_000)
    embed = embeds.table_detail_embed(table(), king, [king], stats(), None)
    assert "Chasing" not in [f.name for f in embed.fields]
    assert next(f for f in embed.fields if f.name == "Margin").value == "unchallenged"


def test_detail_shows_the_note_and_the_full_photo():
    king = sub(9_000_000, note="ball 3 comeback")
    embed = embeds.table_detail_embed(
        table(), king, [king], stats(), "https://cdn.discordapp.com/fresh.png"
    )
    assert "ball 3 comeback" in embed.description
    assert embed.image.url == "https://cdn.discordapp.com/fresh.png"


# -------------------------------------------------------------- announcements

def test_crown_announcement_names_the_dethroned_and_the_margin():
    previous = sub(1_234_567, id=1, user_id=1, display="alice", age_seconds=7200)
    taker = sub(9_000_000, id=2, user_id=2, display="carl")
    embed = embeds.crown_embed("Godzilla", taker, previous, "proof.jpg")

    assert "Godzilla" in embed.title
    assert embed.author.name == "carl takes the crown"
    assert "9,000,000" in embed.description
    assert "Dethrones <@1> by **7,765,433**" in embed.description
    assert "held it for 2h" in embed.description
    assert embed.image.url == "attachment://proof.jpg"


def test_a_first_claim_says_so_rather_than_dethroning_nobody():
    embed = embeds.crown_embed("Godzilla", sub(1_000), None, "proof.jpg")
    assert "First on the board" in embed.description
    assert "Dethrones" not in embed.description


def test_the_compact_line_states_the_gap_to_the_crown():
    king = sub(9_000_000, id=1, user_id=1)
    attempt = sub(5_000_000, id=2, user_id=2, display="bob")
    line = embeds.logged_line("Godzilla", attempt, king)
    assert "5,000,000" in line
    assert "4,000,000 short of" in line.replace("**", "")
    assert "`#2`" in line


def test_the_compact_line_copes_with_an_empty_table():
    line = embeds.logged_line("Godzilla", sub(5_000_000, id=2), None)
    assert "short of" not in line
    assert "5,000,000" in line


# -------------------------------------------------------------------- header

def test_the_listing_header_summarises_the_tournament():
    header = embeds.standings_header(tournament(ends_at=now() + 7200), 12, 5, 4)
    assert "Spring Open 2026" in header
    assert "current standings" in header
    assert "4 table(s)" in header and "12 score(s)" in header and "5 player(s)" in header
    assert "ends <t:" in header


def test_the_header_says_final_once_the_tournament_is_over():
    header = embeds.standings_header(tournament(open_=False), 12, 5, 4)
    assert "final standings" in header
    assert "ends" not in header, "an ended tournament has no countdown"


# ------------------------------------------------------------ final results

def test_final_results_report_each_winner_and_their_reign():
    won = sub(9_000_000, age_seconds=7200)
    embed = embeds.final_results_embed(
        tournament(open_=False), [(table("Godzilla"), won), (table("King Kong"), None)]
    )
    fields = {f.name: f.value for f in embed.fields}
    assert "9,000,000" in fields["Godzilla"] and "held for" in fields["Godzilla"]
    assert "view proof" in fields["Godzilla"]
    assert fields["King Kong"] == "no scores set"


# ------------------------------------------------------------------- history

def test_history_marks_the_standing_high_score():
    """With voided rows interleaved and ties broken by timestamp, the top of the
    ledger isn't necessarily the crown — which is what an admin is checking."""
    king = sub(9_000_000, id=1)
    also_ran = sub(5_000_000, id=2, user_id=2, display="bob")
    embed = embeds.history_embed(table(), [king, also_ran], king)

    king_line, other_line = embed.description.split("\n")
    assert king_line.startswith(embeds.CROWN)
    assert "current high score" in king_line
    assert "**9,000,000**" in king_line

    assert other_line.startswith(embeds.BULLET)
    assert "current high score" not in other_line


def test_history_marks_a_king_that_is_not_the_highest_row_shown():
    """A voided 15.68B outranks the standing 3.13B — exactly the real case."""
    standing = sub(3_127_605_730, id=1)
    voided = sub(15_683_956_200, id=2, voided=True)
    embed = embeds.history_embed(table(), [standing, voided], standing)

    lines = embed.description.split("\n")
    assert lines[0].startswith(embeds.CROWN) and "3,127,605,730" in lines[0]
    assert lines[1].startswith(embeds.VOID_MARK) and "~~" in lines[1]
    assert "cheating" in lines[1], "the void reason stays visible"


def test_history_says_when_the_king_is_older_than_the_page():
    """`/history` shows the most recent entries, so a long reign falls off."""
    king = sub(9_000_000, id=1)
    recent = [sub(1_000, id=n, user_id=2) for n in (7, 8, 9)]
    embed = embeds.history_embed(table(), recent, king)

    assert embeds.CROWN not in embed.description
    assert "#1" in embed.footer.text
    assert "9,000,000" in embed.footer.text
    assert "older than this page" in embed.footer.text


def test_history_says_when_everything_is_voided():
    embed = embeds.history_embed(table(), [sub(1_000, voided=True)], None)
    assert "no standing high score" in embed.footer.text


def test_history_of_an_untouched_table():
    embed = embeds.history_embed(table(), [], None)
    assert "No submissions" in embed.description
    assert embed.footer.text is None, "nothing to caveat when there's nothing there"


def test_history_still_reads_without_a_king_argument():
    """Called with no king, every line is neutral rather than wrongly marked."""
    embed = embeds.history_embed(table(), [sub(9_000_000, id=1)])
    assert embeds.CROWN not in embed.description
    assert embed.description.startswith(embeds.BULLET)


ALL_BUILDERS = [
    lambda: embeds.table_summary_embed(table(), sub(1_000), None, stats(), None),
    lambda: embeds.table_detail_embed(
        table(),
        sub(9_000_000),
        [sub(9_000_000), sub(5_000_000, id=2, user_id=2), sub(1, id=3, user_id=3)],
        stats(challenges=2),
        None,
    ),
    lambda: embeds.crown_embed("Godzilla", sub(1_000), sub(500, id=9), "proof.jpg"),
    lambda: embeds.final_results_embed(
        tournament(open_=False), [(table(), sub(1_000)), (table("King Kong"), None)]
    ),
    lambda: embeds.void_embed("Godzilla", sub(1_000, voided=True), sub(500, id=2), 99),
    lambda: embeds.restore_embed("Godzilla", sub(1_000), sub(1_000), 99),
    lambda: embeds.history_embed(
        table(), [sub(1_000), sub(500, id=2, voided=True)], sub(1_000)
    ),
    lambda: embeds.tournament_started_embed(tournament(), ["Godzilla", "King Kong"]),
    lambda: embeds.status_embed(tournament(), 5, 2),
]


@pytest.mark.parametrize("builder", ALL_BUILDERS)
def test_every_embed_stays_within_discord_limits(builder):
    """Discord rejects an embed over 6000 characters or with an empty field."""
    embed = builder()
    assert len(embed) <= 6000
    for field in embed.fields:
        assert field.name and field.value, "Discord 400s on an empty field"


@pytest.mark.parametrize("builder", ALL_BUILDERS)
def test_headings_only_appear_where_discord_renders_them(builder):
    """A markdown heading renders in an embed *description* but not in a field
    value, where the '#' shows up literally (discord-api-docs#7167).

    Confirmed live in Discord: the headline score in a description renders as a
    heading. Published guidance on this is contradictory, so don't take a
    secondary source as grounds to move one into a field.
    """
    embed = builder()
    for field in embed.fields:
        offenders = [
            line for line in field.value.split("\n") if line.lstrip().startswith("#")
        ]
        assert not offenders, f"heading in the field {field.name!r}: {offenders}"
