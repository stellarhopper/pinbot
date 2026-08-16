"""The flag notice and the ✅/❌ review that resolves it.

The rules worth holding down here are the ones that are expensive to get wrong
during a live event: only a *flagged, undecided* score is reviewable by
reaction, and only an admin may decide.
"""

from __future__ import annotations

from bot.review import APPROVE, DROP
from bot.vision import VisionResult

from fakes import ALICE, BOB, BOT_USER, GUILD, FakeMember, Harness

ADMIN = 99
MISMATCH = VisionResult("mismatch", score=1_200_000, reasoning="read 1,200,000")
ILLEGIBLE = VisionResult("illegible", reasoning="glare across the display")
MATCH = VisionResult("match", score=5_000_000, reasoning="clear read")


async def flagged(hx, *, score: str = "5,000,000", result=MISMATCH, user_id=ALICE):
    """Submit a score and flag it. Returns the submission and its proof message."""
    await hx.submit("Godzilla", score, user_id=user_id)
    proof = hx.channel.sent[-1]
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)
    assert submission is not None, "the proof post must be findable by message id"
    await hx.flag(submission, result)
    return submission, proof


# ------------------------------------------------------------------ flagging

async def test_a_flag_pings_the_player_and_the_admin_role(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    hx.store.add_admin_role(GUILD, 777)
    _submission, proof = await flagged(hx)

    notice = hx.channel.sent[-1]
    assert notice.reply_to == proof.id, "the notice replies to the photo it's about"
    assert f"<@{ALICE}>" in notice.content, "the player is standing right there"
    assert "<@&777>" in notice.content
    assert "5,000,000" in notice.content and "1,200,000" in notice.content

    # The one place in the bot that deliberately pings, so the scope matters.
    mentions = notice.allowed_mentions
    assert mentions.everyone is False
    assert [u.id for u in mentions.users] == [ALICE]
    assert [r.id for r in mentions.roles] == [777]
    hx.close()


async def test_a_flag_with_no_admin_role_still_reaches_the_player(tmp_path, monkeypatch):
    """With no admin role configured, admins are Manage Server holders — who are
    not a pingable role. The notice must still make sense."""
    hx = await Harness.create(tmp_path, monkeypatch)
    _submission, _proof = await flagged(hx)

    notice = hx.channel.sent[-1]
    assert f"<@{ALICE}>" in notice.content
    assert "<@&" not in notice.content, "nothing to ping, so don't render an empty role"
    assert notice.allowed_mentions.roles == []
    hx.close()


async def test_the_flag_offers_both_buttons_on_the_photo(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    _submission, proof = await flagged(hx)
    assert hx.reactions_on(proof.id) == {APPROVE, DROP}
    hx.close()


async def test_an_illegible_photo_is_flagged_without_naming_a_score(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    _submission, _proof = await flagged(hx, result=ILLEGIBLE)

    notice = hx.channel.sent[-1]
    assert "couldn't read" in notice.content
    assert "second look" in notice.content
    hx.close()


async def test_a_match_is_recorded_but_says_nothing(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "5,000,000")
    proof = hx.channel.sent[-1]
    before = len(hx.channel.sent)
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)

    await hx.flag(submission, MATCH)

    assert len(hx.channel.sent) == before, "a clean read is not worth a message"
    assert hx.reactions_on(proof.id) == set()
    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.vision_verdict == "match"
    assert not stored.is_pending_review
    hx.close()


async def test_a_missing_add_reactions_permission_still_leaves_the_flag(
    tmp_path, monkeypatch
):
    """Until the permission is granted the one-tap review is gone, but the score
    must still be flagged and still show up in the review queue."""
    hx = await Harness.create(tmp_path, monkeypatch)
    hx.channel.reactions_forbidden = True
    submission, proof = await flagged(hx)

    assert hx.reactions_on(proof.id) == set()
    assert hx.store.get_submission(GUILD, submission.id).is_pending_review
    assert "second look" in hx.channel.sent[-1].content
    hx.close()


# ----------------------------------------------------------------- reviewing

async def test_an_admin_check_lets_the_score_stand(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)
    assert hx.king("Godzilla").id == submission.id

    await hx.react(proof.id, APPROVE, user_id=ADMIN)

    stored = hx.store.get_submission(GUILD, submission.id)
    assert not stored.is_pending_review
    assert stored.reviewed_by == ADMIN
    assert not stored.is_voided
    assert hx.king("Godzilla").id == submission.id
    assert "review.approve" in hx.audit_actions()
    assert hx.reactions_on(proof.id) == set(), "the buttons can't be pressed twice"
    hx.close()


async def test_an_admin_cross_drops_the_score_and_reverts_the_crown(
    tmp_path, monkeypatch
):
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "1,000,000", user_id=BOB, name="bob")
    previous = hx.king("Godzilla")

    submission, proof = await flagged(hx)
    assert hx.king("Godzilla").id == submission.id, "the flagged score is on top"

    await hx.react(proof.id, DROP, user_id=ADMIN)

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.is_voided
    assert stored.void_reason == "photo review"
    assert not stored.is_pending_review
    assert hx.king("Godzilla").id == previous.id, "the crown goes back"
    assert "review.drop" in hx.audit_actions()
    assert hx.reactions_on(proof.id) == set()
    hx.close()


async def test_the_second_admin_to_react_changes_nothing(tmp_path, monkeypatch):
    """Two admins can react in the same instant. Only one decision may land, or
    the score is voided twice and both outcomes get announced."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)
    await hx.react(proof.id, APPROVE, user_id=ADMIN)
    after_first = len(hx.channel.sent)

    await hx.react(proof.id, DROP, user_id=1234)

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.reviewed_by == ADMIN, "the first decision stands"
    assert not stored.is_voided, "the late ❌ must not drop an approved score"
    assert len(hx.channel.sent) == after_first
    assert hx.audit_actions().count("review.drop") == 0
    hx.close()


# ------------------------------------------------------------ what to ignore

async def test_a_non_admin_reaction_is_ignored(tmp_path, monkeypatch):
    """Without this check, any player could drop a rival's flagged score."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, DROP, user_id=BOB, member=FakeMember(BOB))

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.is_pending_review, "still waiting for an actual admin"
    assert not stored.is_voided
    assert "review.drop" not in hx.audit_actions()
    hx.close()


async def test_a_role_admin_may_review(tmp_path, monkeypatch):
    """The admin-role grant works here too, not just Manage Server."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)
    hx.store.add_admin_role(GUILD, 777)

    await hx.react(proof.id, APPROVE, user_id=BOB, member=FakeMember(BOB, roles=(777,)))

    assert not hx.store.get_submission(GUILD, submission.id).is_pending_review
    hx.close()


async def test_a_reaction_on_an_unflagged_proof_post_does_nothing(tmp_path, monkeypatch):
    """The explicit non-goal: this is not "react ❌ on any photo to void it"."""
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "5,000,000")
    proof = hx.channel.sent[-1]
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)

    await hx.react(proof.id, DROP, user_id=ADMIN)

    stored = hx.store.get_submission(GUILD, submission.id)
    assert not stored.is_voided, "an ordinary score is not droppable by reaction"
    assert stored.reviewed_at is None
    assert "review.drop" not in hx.audit_actions()
    hx.close()


async def test_the_bots_own_reactions_are_ignored(tmp_path, monkeypatch):
    """The bot adds ✅/❌ itself, and those arrive back through this listener.

    The member is deliberately an admin here. A real payload for the bot's own
    reaction *does* carry a member, and if anyone ever grants the bot's role
    admin access, nothing downstream would stop it approving every flag the
    instant it posted one. This guard is what makes that impossible.
    """
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(
        proof.id,
        APPROVE,
        user_id=BOT_USER,
        member=FakeMember(BOT_USER, manage_guild=True),
    )

    assert hx.store.get_submission(GUILD, submission.id).is_pending_review
    hx.close()


async def test_other_emoji_are_ignored(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, "\N{PILE OF POO}", user_id=ADMIN)
    await hx.react(proof.id, "\N{THUMBS UP SIGN}", user_id=ADMIN)

    assert hx.store.get_submission(GUILD, submission.id).is_pending_review
    hx.close()


async def test_a_reaction_on_an_unrelated_message_is_ignored(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, _proof = await flagged(hx)

    await hx.react(999_999, DROP, user_id=ADMIN)

    assert hx.store.get_submission(GUILD, submission.id).is_pending_review
    hx.close()


async def test_a_reaction_with_no_member_fails_closed(tmp_path, monkeypatch):
    """A payload carrying no member leaves nobody to authorize, so nothing
    happens — exactly as perms.is_admin does with a bare User."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, DROP, user_id=ADMIN, member=None)

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.is_pending_review
    assert not stored.is_voided
    hx.close()


async def test_a_reaction_outside_a_guild_is_ignored(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, DROP, user_id=ADMIN, guild_id=None)

    assert hx.store.get_submission(GUILD, submission.id).is_pending_review
    hx.close()


# -------------------------------------------------------------- review queue

async def test_pending_flags_is_the_queue_and_empties_on_review(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    first, proof = await flagged(hx)
    second, _ = await flagged(hx, score="4,000,000", user_id=BOB)

    queue = hx.store.pending_flags(GUILD, hx.tournament.id)
    assert [s.id for s in queue] == [first.id, second.id]

    await hx.react(proof.id, APPROVE, user_id=ADMIN)

    queue = hx.store.pending_flags(GUILD, hx.tournament.id)
    assert [s.id for s in queue] == [second.id]
    hx.close()
