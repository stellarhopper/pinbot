"""Flagging a score for a second look, and the ✅/❌ review that resolves it.

The flag is a **public** message in the pinball channel, replying to the proof
photo and mentioning both the player and the admin roles. Not a DM and not an
ephemeral: the player is standing at the machine with the channel open, and both
of those are missable. This is the only place in the bot that deliberately
pings, so the allowed-mentions here are scoped to exactly that one player and
those roles rather than the blanket ``none()`` used everywhere else.

Review is by reaction, because an admin refereeing a live event has one hand on
a beer. The bot puts ✅ and ❌ on the proof post; an admin's reaction either
lets the score stand or drops it through the ordinary void path.

**The wording stays neutral.** Every submission is checked and unreadable photos
flag too, so most flags will be glare on a DMD rather than a bad score. Nothing
here should read as an accusation.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from . import embeds
from .perms import has_admin_access
from .scoring import format_score
from .store import Store, Submission, now
from .vision import VisionResult

log = logging.getLogger(__name__)

APPROVE = "\N{WHITE HEAVY CHECK MARK}"
DROP = "\N{CROSS MARK}"
LOOK = "\N{RIGHT-POINTING MAGNIFYING GLASS}"


def flag_text(
    *,
    user_id: int,
    table_name: str,
    claimed: int,
    result: VisionResult,
    admin_role_ids: list[int],
) -> str:
    """The public notice. Neutral by construction — see the module docstring."""
    if result.verdict == "illegible":
        finding = (
            f"I couldn't read a score off the photo, so **{format_score(claimed)}** "
            f"on **{table_name}** needs a human to sign it off."
        )
    else:
        finding = (
            f"You reported **{format_score(claimed)}** on **{table_name}**, but I "
            f"read **{format_score(result.score)}** off the photo."
        )

    lines = [f"{LOOK} <@{user_id}> — worth a second look.", finding]
    if result.wrong_table(table_name):
        lines.append(
            f"The photo also looks like it might be **{result.table_name}** rather "
            f"than **{table_name}**."
        )

    # Offer the player the self-serve route first. Most flags are their own
    # typo, and a player who can withdraw it in one tap never becomes an admin's
    # problem — but say plainly that keeping it is not theirs to decide, so the
    # missing ✅ doesn't read as a bug.
    lines.append(
        f"{DROP} React on the photo above to withdraw it yourself — no need to "
        "wait for anyone."
    )
    who = " ".join(f"<@&{role_id}>" for role_id in admin_role_ids)
    call = f"{APPROVE} keeps the score, {DROP} drops it."
    # With no admin role configured, admins are Manage Server holders, who are
    # not a pingable role. The flag still stands and still shows in /flagged.
    lines.append(f"{who} — {call}" if who else f"An admin decides otherwise: {call}")
    return "\n".join(lines)


class ReviewCog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: Store) -> None:
        self.bot = bot
        self.store = store
        # (verdict, reasoning, when) for the most recent check on this process.
        # Deliberately not persisted: the question it answers is "is the check
        # working *right now*", which a restart resets anyway.
        self.last_check: tuple[str, str | None, int] | None = None

    # ------------------------------------------------------------- flagging

    async def flag(
        self,
        *,
        guild_id: int,
        submission: Submission,
        table_name: str,
        result: VisionResult,
    ) -> None:
        """Record the verdict, say so under the photo, and flag if it needs a look.

        Every verdict is recorded and shown, including "unavailable". A check
        that silently did nothing used to be indistinguishable from one that
        ran and agreed — which is how a broken check survives a whole event.

        Never raises: this runs in a background task behind an already-confirmed
        submission, and a failure here must cost the player nothing.
        """
        try:
            self.store.set_vision_result(
                guild_id, submission.id, score=result.score, verdict=result.verdict
            )
            self.last_check = (result.verdict, result.reasoning, now())
            await self.annotate(
                submission, embeds.vision_status(result, table_name)
            )
            if not result.needs_review:
                return

            flagged = self.store.flag_submission(guild_id, submission.id)
            if flagged is None:
                return
            self.store.log(
                guild_id,
                actor_id=self.bot.user.id if self.bot.user else 0,
                action="review.flag",
                target=f"submission:{submission.id}",
                detail=f"{result.verdict}: read {result.score}, claimed {submission.score}",
            )

            proof = self._proof_message(submission)
            if proof is None:
                log.warning(
                    "submission %s flagged but has no proof message to review on",
                    submission.id,
                )
                return

            admin_role_ids = self.store.get_admin_role_ids(guild_id)
            await proof.reply(
                flag_text(
                    user_id=submission.user_id,
                    table_name=table_name,
                    claimed=submission.score,
                    result=result,
                    admin_role_ids=admin_role_ids,
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=[discord.Object(id=submission.user_id)],
                    roles=[discord.Object(id=r) for r in admin_role_ids],
                ),
            )
            await self._add_buttons(proof)
        except discord.HTTPException:
            log.exception("failed to post the flag for submission %s", submission.id)
        except Exception:  # noqa: BLE001 - a flag must never break a submission
            log.exception("unexpected failure flagging submission %s", submission.id)

    async def _add_buttons(self, proof: discord.PartialMessage) -> None:
        try:
            await proof.add_reaction(APPROVE)
            await proof.add_reaction(DROP)
        except discord.Forbidden:
            # The flag still stands and still shows in /flagged; only the
            # one-tap review is missing. Worth naming the fix precisely,
            # because a missing permission here looks like a design choice.
            log.warning(
                "cannot add reactions in channel %s — grant the bot Add Reactions "
                "there and flagged scores can be reviewed by reacting; until then "
                "use /flagged and /drop",
                proof.channel.id,
            )

    async def annotate(self, submission: Submission, status: str) -> None:
        """Put the check's verdict on the proof post itself, next to the photo.

        Two shapes to handle, and the difference is not cosmetic. A crown
        announcement is an embed with the photo pulled into it, so the status
        goes in a field; an ordinary submission is plain text with the photo
        attached, so it goes in a subtext line. Editing either must leave the
        attachment alone — the photo is the whole point of the message.
        """
        channel = self._channel(submission)
        if channel is None or not submission.proof_message_id:
            return
        try:
            # A PartialMessage can't be read, and the crown embed has to be
            # edited as Discord stored it: the attachment:// reference was
            # rewritten to a CDN URL when it was posted, and rebuilding the
            # embed from scratch would lose the photo.
            message = await channel.fetch_message(submission.proof_message_id)
            if message.embeds:
                embed = message.embeds[0]
                existing = next(
                    (i for i, f in enumerate(embed.fields) if f.name == embeds.VISION_FIELD),
                    None,
                )
                if existing is None:
                    embed.add_field(name=embeds.VISION_FIELD, value=status, inline=False)
                else:
                    embed.set_field_at(
                        existing, name=embeds.VISION_FIELD, value=status, inline=False
                    )
                await message.edit(embed=embed)
            else:
                body = embeds.strip_vision_line(message.content)
                await message.edit(
                    content=f"{body}\n{embeds.VISION_PREFIX} {status}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            log.warning(
                "cannot edit my own proof post in channel %s, so the photo check "
                "result won't be shown under the photo",
                submission.proof_channel_id,
            )
        except (discord.HTTPException, discord.NotFound):
            log.info("could not annotate the proof post for %s", submission.id)

    def _channel(self, submission: Submission) -> discord.abc.Messageable | None:
        if not submission.proof_channel_id:
            return None
        channel = self.bot.get_channel(submission.proof_channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None

    def _proof_message(self, submission: Submission) -> discord.PartialMessage | None:
        """A handle on the proof post without fetching it."""
        if not submission.proof_channel_id or not submission.proof_message_id:
            return None
        channel = self.bot.get_channel(submission.proof_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return None
        get_partial = getattr(channel, "get_partial_message", None)
        if get_partial is None:
            return None
        return get_partial(submission.proof_message_id)

    # ------------------------------------------------------------ reviewing

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Resolve a pending flag when someone entitled to reacts on the proof post.

        Every reaction in every channel the bot can see arrives here, so the
        order of the guards matters: the cheap local ones come first and only a
        genuine review reaches the ledger.

        Two different grants, deliberately asymmetric:

        * an **admin** may do either — keep the score or drop it;
        * the **player who submitted it** may only *withdraw* it. Owning a score
          is enough to take it back, never enough to certify it, or a mismatch
          would be resolved by the one person with a reason to want it kept.

        A player still cannot touch a score that was never flagged, because the
        pending gate above applies to everyone. This is "withdraw the thing the
        check queried", not "delete your own scores at will".
        """
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return  # our own ✅/❌, added when the flag was posted
        emoji = str(payload.emoji)
        if emoji not in (APPROVE, DROP):
            return
        if payload.guild_id is None:
            return

        submission = self.store.get_submission_by_proof_message(
            payload.guild_id, payload.message_id
        )
        if submission is None:
            return  # a reaction on something that isn't a proof post
        if not submission.is_pending_review:
            # Deliberate: this is not "react ❌ on any proof post to void it".
            # Only a score the check actually flagged, and only until someone
            # has decided, is reviewable this way.
            return

        # No member means nobody to authorize as an admin, so fail closed on
        # that grant — exactly as perms.is_admin does. Ownership needs no
        # member object, only the user ID the gateway already gave us.
        member = payload.member
        is_admin = member is not None and has_admin_access(
            manage_guild=member.guild_permissions.manage_guild,
            member_role_ids=[role.id for role in member.roles],
            admin_role_ids=self.store.get_admin_role_ids(payload.guild_id),
        )
        is_owner = payload.user_id == submission.user_id

        if emoji == APPROVE and not is_admin:
            # Including the player whose score it is: they may take it back,
            # never wave it through.
            return
        if emoji == DROP and not (is_admin or is_owner):
            # Ignored in silence. Removing the reaction would need Manage
            # Messages, which the bot does not have and should not need.
            return

        # The claim is what settles a race between two people reacting at once:
        # exactly one gets a row back, and only that one acts.
        reviewed = self.store.review_submission(
            payload.guild_id, submission.id, by=payload.user_id
        )
        if reviewed is None:
            return

        if emoji == APPROVE:
            await self._approve(payload, reviewed)
        else:
            await self._drop(payload, reviewed, withdrawn=is_owner)

    async def _approve(
        self, payload: discord.RawReactionActionEvent, submission: Submission
    ) -> None:
        assert payload.guild_id is not None
        self.store.log(
            payload.guild_id,
            actor_id=payload.user_id,
            action="review.approve",
            target=f"submission:{submission.id}",
        )
        await self._clear_buttons(submission)
        # The verdict line under the photo becomes the record of the decision,
        # so an approved score needs no further message in the channel.
        await self.annotate(
            submission, embeds.vision_reviewed(True, payload.user_id)
        )

    async def _drop(
        self,
        payload: discord.RawReactionActionEvent,
        submission: Submission,
        *,
        withdrawn: bool = False,
    ) -> None:
        assert payload.guild_id is not None
        guild_id = payload.guild_id
        # A player taking their own score back and an admin ruling against it
        # are different events, and /history and /audit should not have to guess
        # which happened.
        reason = "withdrawn by the player" if withdrawn else "photo review"
        voided = self.store.void_submission(
            guild_id, submission.id, voided_by=payload.user_id, reason=reason
        )
        if voided is None:
            return
        self.store.log(
            guild_id,
            actor_id=payload.user_id,
            action="review.withdraw" if withdrawn else "review.drop",
            target=f"submission:{submission.id}",
            detail=reason,
        )
        await self._clear_buttons(submission)
        await self.annotate(
            submission, embeds.vision_reviewed(False, payload.user_id, withdrawn=withdrawn)
        )

        machine = self.store.get_table(guild_id, submission.table_id)
        table_name = machine.name if machine else "an unknown table"
        new_king = self.store.current_king(
            guild_id, submission.tournament_id, submission.table_id
        )
        # The ordinary void announcement, so a drop looks the same however it
        # was made — and so the crown changing hands is visible.
        await self._say(
            submission,
            embed=embeds.void_embed(table_name, voided, new_king, payload.user_id),
        )

    async def _clear_buttons(self, submission: Submission) -> None:
        """Take our own ✅/❌ back off, so the buttons can't be pressed twice."""
        proof = self._proof_message(submission)
        if proof is None:
            return
        for emoji in (APPROVE, DROP):
            try:
                await proof.remove_reaction(emoji, self.bot.user)
            except (discord.HTTPException, AttributeError):
                # Removing *our own* reaction needs no Manage Messages, so this
                # is only reachable if the message is gone. Cosmetic either way.
                log.debug("could not clear %s from submission %s", emoji, submission.id)

    async def _say(
        self,
        submission: Submission,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
    ) -> None:
        proof = self._proof_message(submission)
        if proof is None:
            return
        try:
            await proof.reply(
                content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("failed to post the review outcome for %s", submission.id)


async def setup_review(bot: commands.Bot, store: Store) -> ReviewCog:
    cog = ReviewCog(bot, store)
    await bot.add_cog(cog)
    return cog
