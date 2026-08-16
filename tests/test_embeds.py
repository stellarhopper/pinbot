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
        flagged_at=None,
        reviewed_at=None,
        reviewed_by=None,
        flag_message_id=None,
    )


def table(name: str = "Godzilla", sort_order: int = 1) -> Table:
    return Table(
        id=1, guild_id=500, name=name, active=True, sort_order=sort_order, created_at=0
    )


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

def test_no_two_tables_share_an_accent_until_the_palette_runs_out():
    """The property the old name-hashing version could not hold.

    It picked a slot with crc32(name) % 8, so three tables collided about a
    third of the time — and a real server hit it on its third table. Position
    can't collide: sort_order is unique per guild and never reused.
    """
    colours = [embeds.table_color(table(sort_order=n)).value for n in range(1, 13)]
    assert len(set(colours)) == len(embeds._ACCENTS) == 12


def test_the_accent_depends_only_on_position():
    """Renaming a table must not repaint it, and the colour must survive a
    restart — nothing here may be derived from a per-process hash."""
    assert embeds.table_color(table("Godzilla", 3)) == embeds.table_color(
        table("Renamed Mid-Event", 3)
    )
    assert embeds.table_color(table(sort_order=1)).value == embeds._ACCENTS[0]


def test_the_collision_that_prompted_this():
    """Regression: these three names at these positions are the real server's
    tables. 'King Kong' and 'Dungeons & Dragons' both hashed to slot 4."""
    tables = [
        table("Stranger Things", 1),
        table("King Kong", 2),
        table("Dungeons & Dragons", 3),
    ]
    assert len({embeds.table_color(t).value for t in tables}) == 3


def test_the_palette_wraps_once_it_is_exhausted():
    """The accepted limit: a thirteenth table reuses the first colour."""
    assert embeds.table_color(table(sort_order=13)) == embeds.table_color(
        table(sort_order=1)
    )


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


# --------------------------------------------------------------- chunking

def _embed(chars: int) -> "embeds.discord.Embed":
    from bot import embeds as e
    return e.discord.Embed(description="x" * chars)


def test_a_small_set_stays_in_one_message():
    assert len(embeds.chunk_embeds([_embed(100) for _ in range(5)])) == 1


def test_the_ten_embed_cap_is_respected():
    pages = embeds.chunk_embeds([_embed(10) for _ in range(23)])
    assert [len(p) for p in pages] == [10, 10, 3]


def test_the_character_cap_splits_before_the_count_cap():
    """The limit that actually bites: ten embeds are allowed, but not if they
    total more than 6000 characters between them."""
    pages = embeds.chunk_embeds([_embed(1000) for _ in range(10)])
    assert all(sum(len(e) for e in p) <= embeds.MAX_EMBED_CHARS_PER_MESSAGE for p in pages)
    assert len(pages) == 2
    assert sum(len(p) for p in pages) == 10, "nothing may be dropped"


def test_nothing_is_ever_dropped():
    built = [_embed(700) for _ in range(31)]
    pages = embeds.chunk_embeds(built)
    assert sum(len(p) for p in pages) == 31
    for page in pages:
        assert len(page) <= embeds.MAX_EMBEDS_PER_MESSAGE
        assert sum(len(e) for e in page) <= embeds.MAX_EMBED_CHARS_PER_MESSAGE


def test_an_oversized_embed_gets_a_message_to_itself():
    """Dropping it would silently lose a table from the standings."""
    pages = embeds.chunk_embeds([_embed(100), _embed(5999), _embed(100)])
    assert sum(len(p) for p in pages) == 3


def test_no_embeds_means_no_messages():
    assert embeds.chunk_embeds([]) == []


# ------------------------------------------------------- audit log paging

def _audit(n: int, *, detail: str = "mismatch: read 3127605730, claimed 12335465367"):
    from bot.store import AuditEntry
    return [
        AuditEntry(id=i, guild_id=500, at=now(), actor_id=197105676512788491,
                   action="review.drop", target=f"submission:{i}", detail=detail)
        for i in range(1, n + 1)
    ]


def _links(n: int):
    return {
        f"submission:{i}": (
            f"https://discord.com/channels/1420254013458223158/"
            f"1536971356942372954/{1536999999999999000 + i}"
        )
        for i in range(1, n + 1)
    }


def test_a_short_log_is_still_a_single_embed():
    built = embeds.audit_embeds(_audit(5), 5, links=_links(5))
    assert len(built) == 1
    footer = built[0].footer.text
    assert "showing all 5" in footer
    assert "limit" not in footer, "nothing was held back, so don't offer more"
    assert "pages" not in footer


def test_a_long_log_pages_instead_of_being_cut_off():
    """It used to slice the description at 4000 characters: a 40-entry request
    rendered 18 and cut the last one mid-word, with a footer claiming 40."""
    entries = _audit(40)
    built = embeds.audit_embeds(entries, 40, links=_links(40))

    assert len(built) > 1, "40 entries do not fit in one embed"
    shown = sum(page.description.count("submission:") for page in built)
    assert shown == 40, "every entry survives"
    for page in built:
        assert len(page.description) <= embeds.MAX_DESCRIPTION_CHARS


def test_paging_loses_not_one_character():
    """The pages joined back together must be exactly what one giant embed
    would have said — no entry split across a boundary, nothing trimmed."""
    entries = _audit(40)
    links = _links(40)
    built = embeds.audit_embeds(entries, 40, links=links)
    rejoined = "\n".join(p.description for p in built)

    single = embeds.audit_embeds(entries[:1], 1, links=links)[0].description
    assert single in rejoined, "the first entry renders identically either way"
    for i in range(1, 41):
        assert f"submission:{i}`" in rejoined
        assert links[f"submission:{i}"] in rejoined, "every jump link is intact"


def test_the_footer_counts_what_was_actually_shown():
    built = embeds.audit_embeds(_audit(40), 900, links=_links(40))
    footer = built[-1].footer.text
    assert "40 of 900" in footer
    assert "pages" in footer, "and says it spans several"
    assert all(p.footer.text is None for p in built[:-1]), "one footer, on the last page"


def test_a_truncated_log_says_how_to_see_the_rest():
    """'showing 15 of 17' alone reads like a page that never arrived."""
    footer = embeds.audit_embeds(_audit(15), 17, links=_links(15))[-1].footer.text
    assert "showing 15 of 17" in footer
    assert "/audit show limit:17" in footer


def test_the_suggested_limit_never_exceeds_what_the_command_accepts():
    footer = embeds.audit_embeds(_audit(15), 5000, links=_links(15))[-1].footer.text
    assert "/audit show limit:40" in footer, "40 is the cap; don't suggest 5000"


def test_paged_audit_still_fits_discords_message_limits():
    """audit_embeds pages, chunk_embeds then packs those into messages."""
    built = embeds.audit_embeds(_audit(40), 40, links=_links(40))
    for message in embeds.chunk_embeds(built):
        assert len(message) <= embeds.MAX_EMBEDS_PER_MESSAGE
        assert sum(len(e) for e in message) <= embeds.MAX_EMBED_CHARS_PER_MESSAGE


def test_an_empty_log_is_one_embed_that_explains_itself():
    built = embeds.audit_embeds([], 0)
    assert len(built) == 1
    assert "Nothing recorded yet" in built[0].description


def test_a_cleared_log_keeps_its_note():
    built = embeds.audit_embeds([], 0, cleared_note="Wiped 12 entries.")
    assert built[0].description == "Wiped 12 entries."
