"""/new and /hs driven through their real command callbacks.

These cover the seam between the ledger and Discord: what actually gets
announced, and how the proof photo is stored. Both bugs found on the first live
run lived here rather than in store.py.
"""

from __future__ import annotations

import logging
import types

import pytest

from bot.proofs import ProofURLCache

from fakes import (
    ALICE,
    BOB,
    CARL,
    BrokenChannel,
    FakeAttachment,
    FakeInteraction,
    Harness,
    NoHistoryChannel,
)


# ------------------------------------------------------------ the crown

async def test_first_score_is_announced_as_king(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.submit("Godzilla", "1,234,567")
    assert "new king" in itx.reply

    embed = h.channel.last.embeds[0]
    assert "Godzilla" in embed.title
    assert "takes the crown" in embed.author.name
    assert "1,234,567" in embed.description
    assert "First on the board" in embed.description
    h.close()


async def test_lower_score_does_not_claim_the_crown(tmp_path, monkeypatch):
    """Regression: a 15.68 billion entry was announced as king over 3.13 billion.

    The ledger was right and the input was wrong (see test_scoring), but this
    asserts the announcement itself, which is what a player actually sees.
    """
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "3,127,605,730", user_id=ALICE)

    itx = await h.submit("Godzilla", "1,568,395,620", user_id=BOB, name="bob")
    assert "new king" not in itx.reply.lower()
    assert "Logged" in itx.reply
    assert "3,127,605,730" in itx.reply, "the reply should state the standing score"

    posted = h.channel.last
    assert posted.embeds == [], "a non-crown submission must stay compact"
    assert "short of" in posted.content, "the compact line states the gap to beat"
    assert "1,559,210,110" in posted.content
    assert h.king("Godzilla").user_id == ALICE
    h.close()


async def test_higher_score_takes_over_and_names_the_previous_holder(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,234,567", user_id=ALICE)
    itx = await h.submit("Godzilla", "9,000,000", user_id=CARL, name="carl")

    assert "new king" in itx.reply
    description = h.channel.last.embeds[0].description
    assert "Dethrones" in description and f"<@{ALICE}>" in description
    assert "7,765,433" in description, "the announcement states the winning margin"
    assert h.king("Godzilla").user_id == CARL
    h.close()


async def test_tying_the_leader_does_not_steal_the_crown(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "5,000,000", user_id=ALICE)
    itx = await h.submit("Godzilla", "5,000,000", user_id=BOB, name="bob")
    assert "new king" not in itx.reply.lower()
    assert h.king("Godzilla").user_id == ALICE, "first to the number keeps it"
    h.close()


async def test_tables_are_scored_independently(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)
    # A far lower score on a different machine is still that machine's king.
    itx = await h.submit("Attack From Mars", "12,000", user_id=BOB, name="bob")
    assert "new king" in itx.reply
    assert h.king("Godzilla").user_id == ALICE
    assert h.king("Attack From Mars").user_id == BOB
    h.close()


# ----------------------------------------------------- proof photo handling

async def test_a_crowns_photo_is_embedded_and_still_findable_by_hs(tmp_path, monkeypatch):
    """Regression: /hs showed a proof *link* instead of the photo.

    A crown announcement references its photo as ``attachment://`` so the
    message keeps rendering days later. Discord honours that by moving the
    attachment *into the embed* and emptying ``message.attachments`` — so
    reading only the attachment list finds nothing for every current king,
    which is exactly what happened live.
    """
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    posted = h.channel.last
    assert posted.attachments == [], "Discord empties this for an embedded photo"
    assert posted.embedded_image_url, "the photo must live on the embed instead"

    itx = await h.hs()
    assert itx.embed_list[0].thumbnail.url == posted.embedded_image_url
    h.close()


async def test_a_compact_posts_photo_is_a_plain_attachment(tmp_path, monkeypatch):
    """The other message shape: no embed, so the photo stays an attachment."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)
    await h.submit("Godzilla", "1,000,000", user_id=BOB, name="bob")

    posted = h.channel.last
    assert posted.embeds == []
    assert len(posted.attachments) == 1 and posted.attachments[0].filename == "proof.jpg"
    h.close()


async def test_the_submitters_avatar_is_snapshotted_and_shown(tmp_path, monkeypatch):
    """Captured at submit time so the standings can show a face with no API
    call per table, and keep working if the player later leaves the server."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=BOB, name="bob")

    king = h.king("Godzilla")
    assert king.user_avatar == f"https://cdn.discordapp.com/avatars/{BOB}/hash.png"
    assert h.channel.last.embeds[0].author.icon_url == king.user_avatar

    itx = await h.hs()
    assert itx.embed_list[0].author.icon_url == king.user_avatar
    h.close()


async def test_proof_is_stored_as_ids_never_as_a_url(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    posted = h.channel.last

    king = h.king("Godzilla")
    assert king.proof_message_id == posted.id
    assert king.proof_channel_id == h.channel.id
    assert king.proof_jump_url == posted.jump_url
    assert "cdn.discordapp.com" not in (king.proof_jump_url or "")
    h.close()


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("image/jpeg", "IMG_1.JPEG", "proof.jpg"),
        ("image/png", "shot.png", "proof.png"),
        ("image/heic", "IMG_2.HEIC", "proof.heic"),
        ("image/tiff", "odd.tiff", "proof.png"),  # unknown type falls back
    ],
)
async def test_proof_filename_is_sanitized(tmp_path, monkeypatch, content_type, filename, expected):
    """The filename lands in an attachment:// URL, so it can't carry user input."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit(
        "Godzilla", "1,000,000", proof=FakeAttachment(content_type=content_type, filename=filename)
    )
    assert h.king("Godzilla").proof_filename == expected
    h.close()


async def test_nothing_is_recorded_when_the_photo_cannot_be_posted(tmp_path, monkeypatch):
    """A score with no proof isn't worth keeping, so the row is rolled back."""
    h = await Harness.create(tmp_path, monkeypatch, channel=BrokenChannel())
    itx = await h.submit("Godzilla", "1,000,000")
    assert "nothing" in itx.reply and "was recorded" in itx.reply
    assert h.king("Godzilla") is None
    assert h.store.submission_count(500, h.tournament.id) == 0
    h.close()


# ------------------------------------------------------------ bad input

@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(table="Godzilla", score="not a score"), "whole number"),
        (dict(table="Nonexistent", score="1"), "don't have a table"),
    ],
)
async def test_bad_arguments_are_refused(tmp_path, monkeypatch, kwargs, expected):
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.submit(**kwargs)
    assert expected in itx.reply
    assert h.channel.sent == [], "a refused submission must not post anything"
    h.close()


async def test_a_decimal_score_is_refused_not_multiplied(tmp_path, monkeypatch):
    """Regression: 1,568,395,620.0 was silently recorded as 15,683,956,200."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "3,127,605,730", user_id=ALICE)
    itx = await h.submit("Godzilla", "1,568,395,620.0", user_id=BOB, name="bob")
    assert "decimal point" in itx.reply
    assert h.king("Godzilla").score == 3_127_605_730, "the crown must not have moved"
    h.close()


@pytest.mark.parametrize(
    ("proof", "expected"),
    [
        (FakeAttachment(content_type="application/pdf", filename="scan.pdf"), "isn't an image"),
        (FakeAttachment(data=b"x" * (11 * 1024 * 1024)), "under 10 MB"),
    ],
)
async def test_bad_photos_are_refused(tmp_path, monkeypatch, proof, expected):
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.submit("Godzilla", "1,000,000", proof=proof)
    assert expected in itx.reply
    assert h.channel.sent == []
    h.close()


# ------------------------------------------------------ the submission window

async def test_submission_before_the_tournament_starts_is_refused(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tournament=False)
    itx = await h.submit("Godzilla", "1,000,000")
    assert "No tournament is running" in itx.reply
    assert h.channel.sent == []
    h.close()


async def test_submission_after_the_tournament_ends_is_refused(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    h.store.end_tournament(500, h.tournament.id, ended_by=99)
    itx = await h.submit("Godzilla", "99,999,999")
    assert "ended" in itx.reply and "extend" in itx.reply
    assert h.channel.sent == []
    h.close()


async def test_submission_without_a_configured_channel_is_refused(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, set_channel=False)
    itx = await h.submit("Godzilla", "1,000,000")
    assert "pinball channel isn't set up" in itx.reply
    h.close()


async def test_submission_with_no_tables_points_at_table_add(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    itx = await h.submit("Godzilla", "1,000,000")
    assert "/table add" in itx.reply
    h.close()


# ------------------------------------------------------------------- /hs

async def test_hs_lists_every_table(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)

    itx = await h.hs()
    assert "current standings" in itx.reply
    listing = itx.embed_list
    assert len(listing) == 2
    assert "9,000,000" in listing[0].description
    assert "view proof" in listing[0].description
    assert listing[0].thumbnail.url.startswith("https://cdn.discordapp.com/")
    assert "wide open" in listing[1].description, "an unplayed table says so"
    h.close()


async def test_hs_detail_shows_the_chasing_scores(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)
    await h.submit("Godzilla", "5,000,000", user_id=BOB, name="bob")
    await h.submit("Godzilla", "1,000,000", user_id=CARL, name="carl")

    itx = await h.hs("Godzilla")
    chasing = next(f for f in itx.embed.fields if f.name == "Chasing").value
    assert "5,000,000" in chasing and "1,000,000" in chasing
    assert "9,000,000" not in chasing, "the king isn't chasing anyone"
    h.close()


async def test_hs_reflects_a_voided_score(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=ALICE)
    await h.submit("Godzilla", "9,000,000", user_id=BOB, name="bob")

    leader = h.king("Godzilla")
    h.store.void_submission(500, leader.id, voided_by=99, reason="photo mismatch")

    itx = await h.hs()
    assert "1,000,000" in itx.embed_list[0].description
    h.close()


async def test_hs_switches_to_final_standings_once_ended(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    h.store.end_tournament(500, h.tournament.id, ended_by=99)
    itx = await h.hs()
    assert "final standings" in itx.reply
    h.close()


async def test_hs_with_no_tournament_explains_rather_than_erroring(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tournament=False)
    itx = await h.hs()
    assert "/tournament start" in itx.reply
    h.close()


async def test_hs_with_an_unknown_table_lists_the_real_ones(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.hs("Twilight Zone")
    assert "Godzilla" in itx.reply and "Attack From Mars" in itx.reply
    h.close()


# --------------------------------------------------------- proof URL cache

async def test_proof_urls_are_cached_then_refetched_when_cold(tmp_path, monkeypatch):
    """A four-table listing shouldn't cost four API calls every time."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    h.channel.fetch_count = 0
    await h.hs()
    first = h.channel.fetch_count
    assert first == 1, "the first listing resolves a fresh URL once"

    await h.hs()
    await h.hs()
    assert h.channel.fetch_count == first, "repeat listings must hit the cache"

    # Expire the cache and confirm it goes back to Discord exactly once more.
    h.scores.urls = ProofURLCache(ttl_seconds=0)
    await h.hs()
    assert h.channel.fetch_count == first + 1
    h.close()


async def test_missing_read_message_history_degrades_and_says_why(tmp_path, monkeypatch, caplog):
    """The real symptom: /hs showed proof *links* instead of photos.

    Falling back to the link is correct, but it used to happen silently — the
    log now names the missing permission.
    """
    h = await Harness.create(tmp_path, monkeypatch, channel=NoHistoryChannel())
    await h.submit("Godzilla", "1,000,000")

    with caplog.at_level(logging.WARNING, logger="bot.proofs"):
        itx = await h.hs()

    embed = itx.embed_list[0]
    assert embed.thumbnail.url is None, "no photo without the permission"
    assert "view proof" in embed.description, "but the permanent link still works"

    assert any("Read Message History" in r.message for r in caplog.records), (
        "the operator must be told which permission is missing"
    )
    h.close()


async def test_a_legacy_score_without_a_snapshot_still_shows_a_face(tmp_path, monkeypatch):
    """Scores recorded before the avatar column existed fall back to a lookup."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=BOB, name="bob")

    # Blank the snapshot, exactly as a pre-migration row looks.
    h.store._conn.execute("UPDATE submissions SET user_avatar = NULL")
    h.store._conn.commit()
    assert h.king("Godzilla").user_avatar is None

    fetched = "https://cdn.discordapp.com/avatars/2/looked-up.png"

    async def fetch_user(user_id):
        return types.SimpleNamespace(
            display_avatar=types.SimpleNamespace(url=fetched)
        )

    monkeypatch.setattr(h.bot, "get_user", lambda uid: None)
    monkeypatch.setattr(h.bot, "fetch_user", fetch_user)

    itx = await h.hs()
    assert itx.embed_list[0].author.icon_url == fetched
    h.close()


async def test_a_deleted_proof_message_degrades_to_the_jump_link(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    h.channel.sent.clear()  # as if someone deleted the proof message

    itx = await h.hs()
    embed = itx.embed_list[0]
    assert embed.thumbnail.url is None, "no thumbnail when the photo is gone"
    assert "view proof" in embed.description, "the permanent link still works"
    h.close()


# --------------------------------------------------------- /hs * (all tables)

async def test_hs_star_expands_every_table_in_full(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)
    await h.submit("Godzilla", "5,000,000", user_id=BOB, name="bob")

    itx = await h.hs("*")

    listing = itx.embed_list
    assert len(listing) == 2, "one detail embed per table"
    godzilla = listing[0]
    # The detail shape, not the summary one: summaries have no field breakdown.
    assert {f.name for f in godzilla.fields} >= {"Held for", "Margin"}
    assert "Chasing" in {f.name for f in godzilla.fields}
    assert "current standings" in itx.reply, "the header still leads"
    h.close()


async def test_hs_all_is_accepted_as_a_word_too(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)

    for spelling in ("all", "ALL", "  All  "):
        itx = await h.hs(spelling)
        assert len(itx.embed_list) == 2, f"{spelling!r} should expand everything"
    h.close()


async def test_a_real_table_beats_the_wildcard(tmp_path, monkeypatch):
    """A machine actually called 'All' must stay reachable by name."""
    h = await Harness.create(tmp_path, monkeypatch, tables=("All", "Godzilla"))
    await h.submit("All", "9,000,000", user_id=ALICE)

    itx = await h.hs("All")

    assert itx.embed.title.startswith("All"), "resolved as the table, not the wildcard"
    h.close()


async def test_hs_summary_no_longer_drops_tables_past_the_tenth(tmp_path, monkeypatch):
    """It used to send one message capped at ten embeds, which silently lost
    every table after that — invisible until an event big enough to hit it."""
    names = tuple(f"Table {n:02d}" for n in range(1, 15))
    h = await Harness.create(tmp_path, monkeypatch, tables=names)

    itx = await h.hs()

    shown = [e for kind, _c, _e, embeds_ in itx.record if embeds_ for e in embeds_]
    assert len(shown) == 14, "every table is accounted for"
    titles = " ".join(e.title for e in shown)
    assert "Table 14" in titles and "Table 01" in titles
    h.close()


async def test_hs_star_splits_across_messages_when_it_has_to(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=tuple(
        f"Table {n:02d}" for n in range(1, 15)
    ))

    itx = await h.hs("*")

    pages = [embeds_ for _k, _c, _e, embeds_ in itx.record if embeds_]
    assert len(pages) > 1, "14 detail embeds cannot fit in one message"
    assert all(len(p) <= 10 for p in pages)
    assert sum(len(p) for p in pages) == 14
    h.close()


async def test_the_hs_autocomplete_offers_the_wildcard(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    itx = FakeInteraction()

    choices = await h.scores.hs_table_autocomplete(itx, "")
    assert choices[0].value == "*"
    assert "All tables" in choices[0].name

    # Once you're typing a real name it gets out of the way.
    narrowed = await h.scores.hs_table_autocomplete(itx, "godz")
    assert [c.value for c in narrowed] == ["Godzilla"]
    h.close()


async def test_the_new_autocomplete_does_not_offer_the_wildcard(tmp_path, monkeypatch):
    """/new takes a machine you actually played."""
    h = await Harness.create(tmp_path, monkeypatch)
    choices = await h.scores.table_autocomplete(FakeInteraction(), "")
    assert "*" not in [c.value for c in choices]
    h.close()


# ----------------------------------------------------- mention hygiene

async def test_hs_cannot_be_used_to_ping_someone(tmp_path, monkeypatch):
    """/hs answers in public and echoes the table argument back. Without a
    mention policy, `/hs table:<@someone>` makes the bot ping an arbitrary
    person on any player's say-so — user mentions need no permission at all."""
    h = await Harness.create(tmp_path, monkeypatch)

    itx = await h.hs("<@197105676512788491> and <@&777> and @everyone")

    echoed = [
        (content, policy)
        for (_k, content, _e, _es), policy in zip(itx.record, itx.mentions)
        if content and "don't have a table" in content
    ]
    assert echoed, "the unknown-table reply is the echo path"
    content, policy = echoed[0]
    assert "197105676512788491" in content, "it really does echo the argument back"
    assert policy is not None, "a public echo of user input must state a policy"
    assert policy.everyone is False
    assert policy.users in (False, [], None)
    assert policy.roles in (False, [], None)
    h.close()


async def test_every_public_reply_states_a_mention_policy(tmp_path, monkeypatch):
    """A send with no stated policy is the one that gets copied into the next
    feature. Public replies must all be explicit."""
    import inspect
    import re
    from bot import scores as scores_module

    source = inspect.getsource(scores_module)
    sends = re.findall(
        r"(?:followup\.send|channel\.send)\((?:[^()]|\([^()]*\))*\)", source
    )
    public = [s for s in sends if "ephemeral=True" not in s]
    assert public, "sanity: there are public sends to check"
    missing = [s for s in public if "allowed_mentions" not in s]
    assert not missing, f"public send without a mention policy: {missing}"
