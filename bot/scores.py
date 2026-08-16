"""Player-facing commands: /new and /hs."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from . import embeds, proofs, vision
from .avatars import AvatarCache
from .review import ReviewCog
from .scoring import ScoreFormatError, format_score, parse_score
from .store import Store, Submission, Table, Tournament

log = logging.getLogger(__name__)


class ScoresCog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: Store, urls: proofs.ProofURLCache) -> None:
        self.bot = bot
        self.store = store
        self.urls = urls
        # Bounds how many proof photos are in memory at once. Purely a memory
        # guard for the Raspberry Pi — the ledger needs no locking, because the
        # crown is a derived query and the insert-and-re-derive happens inside
        # one SQLite transaction, so concurrent submissions cannot disagree
        # about who is king.
        self._upload_slots = asyncio.Semaphore(2)
        # Fills in faces for scores recorded before the avatar was snapshotted.
        self.avatars = AvatarCache()
        # The photo check runs after the player has already been told their
        # score is in, so it gets its own budget: one at a time, because a Pi
        # should not be holding a second 10 MB photo while an upload is in
        # flight. asyncio keeps only weak references to tasks, so a task that
        # isn't held here can be garbage-collected mid-flight.
        self._vision_slots = asyncio.Semaphore(1)
        self._vision_tasks: set[asyncio.Task] = set()
        self.review: ReviewCog | None = None

    async def cog_unload(self) -> None:
        for task in list(self._vision_tasks):
            task.cancel()

    # ----------------------------------------------------------- autocomplete

    async def table_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        needle = current.strip().lower()
        return [
            app_commands.Choice(name=table.name, value=table.name)
            for table in self.store.list_tables(interaction.guild_id)
            if needle in table.name.lower()
        ][:25]

    # ------------------------------------------------------------------- /new

    @app_commands.command(
        name="new",
        description="Report a new score. Photo of the score screen required.",
    )
    @app_commands.describe(
        table="Which machine you played",
        score="Your score — commas and spaces are fine",
        proof="Photo of the score screen",
        note="Optional note for the admins",
    )
    @app_commands.autocomplete(table=table_autocomplete)
    @app_commands.guild_only()
    async def new(
        self,
        interaction: discord.Interaction,
        table: str,
        score: str,
        proof: discord.Attachment,
        note: str | None = None,
    ) -> None:
        # Reading the photo and re-uploading it takes longer than the 3-second
        # interaction window, so defer before doing any work.
        await interaction.response.defer(ephemeral=True, thinking=True)
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        tournament = self.store.active_tournament(guild_id)
        if tournament is None:
            latest = self.store.latest_tournament(guild_id)
            if latest is not None and latest.ended_at:
                await interaction.followup.send(
                    f"**{latest.label}** ended {embeds.ts(latest.ended_at, 'R')}, so "
                    "scores are closed. An admin can reopen it with "
                    "`/tournament extend`.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "No tournament is running yet. An admin needs to open one with "
                    "`/tournament start`.",
                    ephemeral=True,
                )
            return

        machine = self.store.get_table_by_name(guild_id, table)
        if machine is None or not machine.active:
            available = self.store.list_tables(guild_id)
            if not available:
                await interaction.followup.send(
                    "No tables are set up yet. An admin needs to add them with "
                    "`/table add`.",
                    ephemeral=True,
                )
            else:
                names = ", ".join(f"**{t.name}**" for t in available)
                await interaction.followup.send(
                    f"I don't have a table called **{table}**. Try one of: {names}",
                    ephemeral=True,
                )
            return

        try:
            value = parse_score(score)
        except ScoreFormatError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        channel = await self._resolve_channel(guild_id)
        if channel is None:
            await interaction.followup.send(
                "The pinball channel isn't set up (or I can't post in it). An admin "
                "needs to run `/config pinball-channel`.",
                ephemeral=True,
            )
            return

        try:
            data, filename = await proofs.read_proof(proof)
        except proofs.ProofError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        async with self._upload_slots:
            try:
                submission, previous, king = await self._record(
                    interaction=interaction,
                    tournament=tournament,
                    machine=machine,
                    value=value,
                    note=note,
                    channel=channel,
                    data=data,
                    filename=filename,
                )
            except discord.HTTPException:
                log.exception("failed to post proof for guild %s", guild_id)
                await interaction.followup.send(
                    "I couldn't post your photo to the pinball channel, so nothing "
                    "was recorded. Check my permissions there and try again.",
                    ephemeral=True,
                )
                return
            else:
                # Hand the bytes to the check before the `del` below drops our
                # name for them — the task then holds the only reference, for
                # the few seconds the call takes. This is the one place the
                # memory guard is deliberately relaxed.
                self._spawn_photo_check(
                    guild_id=guild_id,
                    submission=submission,
                    machine=machine,
                    data=data,
                    media_type=proofs.vision_media_type(proof),
                )
            finally:
                del data  # release the image bytes promptly

        took_crown = king is not None and king.id == submission.id
        if took_crown:
            await interaction.followup.send(
                f"{embeds.CROWN} Recorded — **{format_score(value)}** on "
                f"**{machine.name}**. You're the new king of the hill!",
                ephemeral=True,
            )
        else:
            standing = (
                f"The high score is **{format_score(king.score)}** by "
                f"<@{king.user_id}>."
                if king is not None
                else "There's no high score on that table yet."
            )
            await interaction.followup.send(
                f"Logged **{format_score(value)}** on **{machine.name}** as "
                f"`#{submission.id}`. {standing}",
                ephemeral=True,
            )

    # ------------------------------------------------------------ photo check

    def _spawn_photo_check(
        self,
        *,
        guild_id: int,
        submission: Submission,
        machine: Table,
        data: bytes,
        media_type: str | None,
    ) -> None:
        """Start the background cross-check, if it can run at all.

        Every reason to skip is a silent one. The player has already been told
        their score is in, and none of these are their problem: the feature is
        off, the host has no key or package, the photo is a format the API
        won't take, or the review cog isn't loaded.
        """
        if media_type is None or self.review is None:
            return
        if not self.store.get_vision_enabled(guild_id) or not vision.is_available():
            return

        task = asyncio.create_task(
            self._check_photo(
                guild_id=guild_id,
                submission=submission,
                machine=machine,
                data=data,
                media_type=media_type,
            )
        )
        self._vision_tasks.add(task)
        task.add_done_callback(self._vision_tasks.discard)

    async def _check_photo(
        self,
        *,
        guild_id: int,
        submission: Submission,
        machine: Table,
        data: bytes,
        media_type: str | None,
    ) -> None:
        try:
            async with self._vision_slots:
                result = await vision.check_score(data, media_type, submission.score)
            if result.verdict == "unavailable":
                # The bot's own failure. Already logged in vision.py, and it
                # must never cost the player a flag.
                return
            assert self.review is not None
            await self.review.flag(
                guild_id=guild_id,
                submission=submission,
                table_name=machine.name,
                result=result,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the score is already recorded
            log.exception("photo check failed for submission %s", submission.id)

    async def _record(
        self,
        *,
        interaction: discord.Interaction,
        tournament: Tournament,
        machine: Table,
        value: int,
        note: str | None,
        channel: discord.abc.Messageable,
        data: bytes,
        filename: str,
    ) -> tuple[Submission, Submission | None, Submission | None]:
        """Write the ledger row, then post the proof and backfill its location.

        The row is written first so a submission is never lost to a failed
        Discord post; the proof message IDs are patched in immediately after.
        """
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        display = getattr(interaction.user, "display_name", None) or interaction.user.name
        # Snapshot the avatar alongside the name so the standings can show a
        # face without an API call per table, and keep working for a player who
        # later leaves the server.
        avatar = getattr(interaction.user, "display_avatar", None)

        submission, previous, king = self.store.add_submission(
            guild_id=guild_id,
            tournament_id=tournament.id,
            table_id=machine.id,
            user_id=interaction.user.id,
            user_display=display,
            user_avatar=str(avatar.url) if avatar is not None else None,
            score=value,
            note=note,
        )
        took_crown = king is not None and king.id == submission.id

        try:
            if took_crown:
                message = await channel.send(
                    embed=embeds.crown_embed(machine.name, submission, previous, filename),
                    file=proofs.as_file(data, filename),
                )
            else:
                message = await channel.send(
                    embeds.logged_line(machine.name, submission, king),
                    file=proofs.as_file(data, filename),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            # The photo is the proof, so a score with no photo isn't worth
            # keeping. Roll the row back so the caller can honestly report
            # that nothing was recorded.
            self.store.delete_submission(guild_id, submission.id)
            raise

        # Store where the proof lives, never its URL: attachment CDN URLs are
        # signed and expire in about 24 hours, and this event runs for days.
        self.store.attach_proof(
            guild_id,
            submission.id,
            channel_id=message.channel.id,
            message_id=message.id,
            jump_url=message.jump_url,
            filename=filename,
        )
        refreshed = self.store.get_submission(guild_id, submission.id)
        assert refreshed is not None
        return refreshed, previous, king

    # -------------------------------------------------------------------- /hs

    @app_commands.command(name="hs", description="Show the current high scores.")
    @app_commands.describe(table="Show one table in detail (omit for all tables)")
    @app_commands.autocomplete(table=table_autocomplete)
    @app_commands.guild_only()
    async def hs(self, interaction: discord.Interaction, table: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        tournament = self.store.latest_tournament(guild_id)
        if tournament is None:
            await interaction.followup.send(
                "No tournament has run here yet. An admin can start one with "
                "`/tournament start`."
            )
            return

        tables = self.store.list_tables(guild_id)
        if not tables:
            await interaction.followup.send(
                "No tables are set up yet. An admin needs to add them with `/table add`."
            )
            return

        if table is not None:
            machine = self.store.get_table_by_name(guild_id, table)
            if machine is None:
                names = ", ".join(f"**{t.name}**" for t in tables)
                await interaction.followup.send(
                    f"I don't have a table called **{table}**. Try one of: {names}"
                )
                return
            standings = self.store.standings(guild_id, tournament.id, machine.id, limit=6)
            king = standings[0] if standings else None
            fresh = await self._fresh(king)
            stats = self.store.table_stats(guild_id, tournament.id, machine.id, king=king)
            avatar = await self.avatars.url_for(self.bot, king)
            await interaction.followup.send(
                embed=embeds.table_detail_embed(
                    machine, king, standings, stats, fresh, avatar
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        built: list[discord.Embed] = []
        for machine in tables[:10]:  # Discord caps a message at 10 embeds
            top = self.store.standings(guild_id, tournament.id, machine.id, limit=2)
            king = top[0] if top else None
            runner_up = top[1] if len(top) > 1 else None
            fresh = await self._fresh(king)
            stats = self.store.table_stats(guild_id, tournament.id, machine.id, king=king)
            avatar = await self.avatars.url_for(self.bot, king)
            built.append(
                embeds.table_summary_embed(
                    machine, king, runner_up, stats, fresh, avatar
                )
            )

        submissions, players = self.store.tournament_stats(guild_id, tournament.id)
        await interaction.followup.send(
            embeds.standings_header(tournament, submissions, players, len(tables)),
            embeds=built,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _fresh(self, submission: Submission | None) -> str | None:
        if submission is None:
            return None
        return await self.urls.fresh_url(
            self.bot, submission.proof_channel_id, submission.proof_message_id
        )

    # ----------------------------------------------------------------- shared

    async def _resolve_channel(self, guild_id: int) -> discord.abc.Messageable | None:
        channel_id = self.store.get_channel_id(guild_id)
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.HTTPException, discord.InvalidData):
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None


async def setup_scores(
    bot: commands.Bot, store: Store, urls: proofs.ProofURLCache
) -> ScoresCog:
    cog = ScoresCog(bot, store, urls)
    await bot.add_cog(cog)
    return cog
