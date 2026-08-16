"""The flag notice and the ✅/❌ review that resolves it.

The rules worth holding down here are the ones that are expensive to get wrong
during a live event: only a *flagged, undecided* score is reviewable by
reaction, and only an admin may decide.
"""

from __future__ import annotations

from bot.review import APPROVE, DROP
from bot.vision import VisionResult

from fakes import ALICE, BOB, BOT_USER, CARL, GUILD, FakeMember, Harness

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


# ------------------------------------------------------- the status line

UNAVAILABLE = VisionResult("unavailable", reasoning="the Anthropic account is out of credit")


async def test_a_clean_read_is_recorded_under_the_photo(tmp_path, monkeypatch):
    """Silence on a match is what made a broken check indistinguishable from a
    working one. The photo now carries the verdict either way."""
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "5,000,000", user_id=BOB, name="bob")
    await hx.submit("Godzilla", "1,000,000")   # not the crown: a plain-text post
    proof = hx.channel.sent[-1]
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)

    await hx.flag(submission, VisionResult("match", score=1_000_000))

    assert proof.edits == 1, "the proof post itself is annotated, not replied to"
    assert "Photo check" in proof.content
    assert "matches" in proof.content
    assert "1,000,000" in proof.content
    hx.close()


async def test_an_outage_says_so_instead_of_saying_nothing(tmp_path, monkeypatch):
    """The first live run failed every check on an empty credit balance and
    looked exactly like a working one. It must never be silent again."""
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "5,000,000")
    proof = hx.channel.sent[-1]
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)
    before = len(hx.channel.sent)

    await hx.flag(submission, UNAVAILABLE)

    assert "couldn't run" in proof.embeds[0].fields[-1].value
    assert "score stands" in proof.embeds[0].fields[-1].value
    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.vision_verdict == "unavailable", "and it's on the ledger too"
    assert not stored.is_pending_review, "an outage must never cost the player a flag"
    assert len(hx.channel.sent) == before, "no new message — the photo post carries it"
    assert hx.reactions_on(proof.id) == set()
    hx.close()


async def test_a_crown_post_gets_a_field_and_keeps_its_photo(tmp_path, monkeypatch):
    """A crown announcement is an embed with the photo pulled into it. Editing
    it must add the status without losing the proof."""
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "5,000,000")
    proof = hx.channel.sent[-1]
    image_before = proof.embeds[0].image.url
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)

    await hx.flag(submission, VisionResult("match", score=5_000_000))

    field = proof.embeds[0].fields[-1]
    assert field.name == "Photo check"
    assert "matches" in field.value
    assert proof.embeds[0].image.url == image_before, "the photo must survive the edit"
    hx.close()


async def test_the_status_line_is_replaced_not_stacked(tmp_path, monkeypatch):
    """Flag then review means two annotations on one post."""
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "5,000,000", user_id=BOB, name="bob")
    await hx.submit("Godzilla", "1,000,000")
    proof = hx.channel.sent[-1]
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)

    await hx.flag(submission, MISMATCH)
    await hx.react(proof.id, APPROVE, user_id=ADMIN)

    assert proof.content.count("Photo check") == 1, "one status line, not a pile"
    assert "reviewed by" in proof.content
    assert "score stands" in proof.content
    hx.close()


async def test_a_dropped_score_says_who_dropped_it(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, DROP, user_id=ADMIN)

    field = proof.embeds[0].fields[-1]
    assert f"<@{ADMIN}>" in field.value
    assert "dropped" in field.value
    hx.close()


async def test_the_last_check_is_remembered_for_config_show(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    assert hx.review.last_check is None, "nothing checked yet"

    await hx.submit("Godzilla", "5,000,000")
    proof = hx.channel.sent[-1]
    await hx.flag(hx.store.get_submission_by_proof_message(GUILD, proof.id), UNAVAILABLE)

    verdict, reasoning, _when = hx.review.last_check
    assert verdict == "unavailable"
    assert "out of credit" in reasoning
    hx.close()


# --------------------------------------------------- the player's own score

async def test_the_submitter_can_withdraw_their_own_flagged_score(tmp_path, monkeypatch):
    """Most flags are the player's own typo. Letting them take it back in one
    tap keeps it off the admins' desk entirely."""
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "1,000,000", user_id=BOB, name="bob")
    previous = hx.king("Godzilla")
    submission, proof = await flagged(hx)          # ALICE's, and it's on top
    assert hx.king("Godzilla").id == submission.id

    await hx.react(proof.id, DROP, user_id=ALICE, member=FakeMember(ALICE))

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.is_voided
    assert stored.void_reason == "withdrawn by the player"
    assert not stored.is_pending_review
    assert hx.king("Godzilla").id == previous.id, "the crown goes back"
    assert "review.withdraw" in hx.audit_actions()
    assert "review.drop" not in hx.audit_actions(), "a withdrawal is its own event"
    hx.close()


async def test_the_submitter_cannot_approve_their_own_flagged_score(tmp_path, monkeypatch):
    """Owning a score is enough to take it back, never enough to certify it —
    otherwise a mismatch is settled by the one person who wants it kept."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, APPROVE, user_id=ALICE, member=FakeMember(ALICE))

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.is_pending_review, "still waiting for an admin"
    assert stored.reviewed_at is None
    assert "review.approve" not in hx.audit_actions()
    hx.close()


async def test_another_player_still_cannot_drop_it(tmp_path, monkeypatch):
    """The grant is ownership, not 'being a player'."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)          # ALICE's

    await hx.react(proof.id, DROP, user_id=CARL, member=FakeMember(CARL))

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.is_pending_review
    assert not stored.is_voided
    hx.close()


async def test_a_player_cannot_withdraw_a_score_that_was_never_flagged(
    tmp_path, monkeypatch
):
    """This is 'withdraw the thing the check queried', not 'delete your own
    scores at will'. /drop stays the admin path for everything else."""
    hx = await Harness.create(tmp_path, monkeypatch)
    await hx.submit("Godzilla", "5,000,000", user_id=ALICE)
    proof = hx.channel.sent[-1]
    submission = hx.store.get_submission_by_proof_message(GUILD, proof.id)

    await hx.react(proof.id, DROP, user_id=ALICE, member=FakeMember(ALICE))

    stored = hx.store.get_submission(GUILD, submission.id)
    assert not stored.is_voided, "an unflagged score is not self-serve"
    assert stored.reviewed_at is None
    hx.close()


async def test_a_withdrawal_needs_no_member_object(tmp_path, monkeypatch):
    """Ownership is decided from the user ID the gateway already gave us, so a
    payload without a member still lets the player take their own score back —
    unlike the admin grant, which fails closed there."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, DROP, user_id=ALICE, member=None)

    assert hx.store.get_submission(GUILD, submission.id).is_voided
    hx.close()


async def test_the_photo_says_who_withdrew_it(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    _submission, proof = await flagged(hx)

    await hx.react(proof.id, DROP, user_id=ALICE, member=FakeMember(ALICE))

    field = proof.embeds[0].fields[-1]
    assert "withdrawn by" in field.value
    assert f"<@{ALICE}>" in field.value
    assert "dropped" not in field.value, "a withdrawal doesn't read as a ruling"
    hx.close()


async def test_the_flag_tells_the_player_they_can_withdraw_it(tmp_path, monkeypatch):
    """A player who doesn't know the option exists still costs an admin time."""
    hx = await Harness.create(tmp_path, monkeypatch)
    hx.store.add_admin_role(GUILD, 777)
    await flagged(hx)

    notice = hx.channel.sent[-1]
    assert "withdraw it yourself" in notice.content
    assert "<@&777>" in notice.content, "and the admins are still called"
    hx.close()


async def test_an_admin_dropping_someone_elses_score_is_still_a_ruling(
    tmp_path, monkeypatch
):
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)          # ALICE's

    await hx.react(proof.id, DROP, user_id=ADMIN)

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.void_reason == "photo review"
    assert "review.drop" in hx.audit_actions()
    assert "review.withdraw" not in hx.audit_actions()
    hx.close()


# ------------------------------------------------- clearing up the ping

async def test_the_ping_is_deleted_once_an_admin_rules(tmp_path, monkeypatch):
    """The verdict line on the photo is the durable record. A ping still asking
    for a decision that has already been made is worse than no message."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)
    notice = hx.channel.sent[-1]
    assert notice.id in {m.id for m in hx.live()}

    await hx.react(proof.id, APPROVE, user_id=ADMIN)

    assert notice.id not in {m.id for m in hx.live()}, "the ping is cleaned up"
    assert proof.id in {m.id for m in hx.live()}, "but never the photo"
    assert "reviewed by" in proof.embeds[0].fields[-1].value
    hx.close()


async def test_the_ping_is_deleted_when_the_player_withdraws(tmp_path, monkeypatch):
    hx = await Harness.create(tmp_path, monkeypatch)
    _submission, proof = await flagged(hx)
    notice = hx.channel.sent[-1]

    await hx.react(proof.id, DROP, user_id=ALICE, member=FakeMember(ALICE))

    assert notice.id not in {m.id for m in hx.live()}
    hx.close()


async def test_the_ping_survives_until_someone_actually_rules(tmp_path, monkeypatch):
    """Ignored reactions must not clear the call to action."""
    hx = await Harness.create(tmp_path, monkeypatch)
    _submission, proof = await flagged(hx)
    notice = hx.channel.sent[-1]

    await hx.react(proof.id, DROP, user_id=CARL, member=FakeMember(CARL))
    await hx.react(proof.id, APPROVE, user_id=ALICE, member=FakeMember(ALICE))
    await hx.react(proof.id, "\N{PILE OF POO}", user_id=ADMIN)

    assert notice.id in {m.id for m in hx.live()}, "still waiting on a real decision"
    hx.close()


async def test_the_notice_id_is_persisted_not_just_remembered(tmp_path, monkeypatch):
    """A tournament outlives a deploy. Held in memory, the ID would be lost on
    restart and the ping would sit there for days with nothing to act on."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, _proof = await flagged(hx)
    notice = hx.channel.sent[-1]

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.flag_message_id == notice.id
    hx.close()


async def test_the_notice_id_is_forgotten_after_cleanup(tmp_path, monkeypatch):
    """A message ID that no longer resolves is worth nothing, and retrying it
    on every later lookup is worth less."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)

    await hx.react(proof.id, APPROVE, user_id=ADMIN)

    assert hx.store.get_submission(GUILD, submission.id).flag_message_id is None
    hx.close()


async def test_an_already_deleted_ping_is_not_an_error(tmp_path, monkeypatch):
    """Someone may have tidied it up by hand. The review must still complete."""
    hx = await Harness.create(tmp_path, monkeypatch)
    submission, proof = await flagged(hx)
    notice = hx.channel.sent[-1]
    hx.channel.deleted.add(notice.id)          # deleted out from under us

    await hx.react(proof.id, APPROVE, user_id=ADMIN)

    stored = hx.store.get_submission(GUILD, submission.id)
    assert stored.reviewed_by == ADMIN, "the decision still lands"
    assert stored.flag_message_id is None
    hx.close()
