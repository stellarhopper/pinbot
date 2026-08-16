"""Admin commands: tournament control, table setup, config, void/restore, purges."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import embeds, proofs
from .durations import DurationFormatError, format_duration, parse_duration
from .perms import is_admin, require_admin, require_manage_guild
from .scoring import format_score
from .store import InvalidName, Store, StoreError, Submission, Table, Tournament, now

log = logging.getLogger(__name__)


class ConfirmView(discord.ui.View):
    """Ephemeral yes/no gate, usable only by the admin who invoked the command."""

    def __init__(self, author_id: int, *, confirm_label: str = "Confirm") -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "That confirmation isn't yours.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


class TypedConfirmModal(discord.ui.Modal):
    """Destructive-action gate that makes you read a number before typing it.

    A button is too easy to fat-finger for something irreversible, so the
    confirmation is the count of rows about to be destroyed.
    """

    def __init__(
        self,
        *,
        title: str,
        phrase: str,
        prompt: str,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        super().__init__(title=title[:45])
        self.phrase = phrase
        self._on_confirm = on_confirm
        self.field: discord.ui.TextInput = discord.ui.TextInput(
            label=prompt[:45], placeholder=phrase, required=True, max_length=64
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.field.value.strip() != self.phrase:
            await interaction.response.send_message(
                f"That didn't match `{self.phrase}` — nothing was deleted.",
                ephemeral=True,
            )
            return
        await self._on_confirm(interaction)


def _short_date(epoch: int) -> str:
    return time.strftime("%b %d", time.localtime(epoch))


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: Store, urls: proofs.ProofURLCache) -> None:
        self.bot = bot
        self.store = store
        self.urls = urls
        self.autoclose.start()

    async def cog_unload(self) -> None:
        self.autoclose.cancel()

    # ---------------------------------------------------------------- helpers

    async def _channel(self, guild_id: int) -> discord.abc.Messageable | None:
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

    async def _announce(
        self,
        guild_id: int,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        fallback: discord.abc.Messageable | None = None,
    ) -> None:
        """Post to the configured pinball channel, falling back to the caller's."""
        target = await self._channel(guild_id)
        if target is None and isinstance(fallback, discord.abc.Messageable):
            target = fallback
        if target is None:
            # Worth a log line: this is how final results go missing at prize
            # time, and the cause is always an unset or unreachable channel.
            log.warning(
                "no usable channel in guild %s — announcement dropped", guild_id
            )
            return
        try:
            await target.send(
                content=content,
                embed=embed,
                # Announcements carry admin-supplied text (the tournament
                # name), and nothing the bot posts ever needs to ping.
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("failed to announce in guild %s", guild_id)

    def _last_check(self) -> tuple[str, str | None, int] | None:
        """What the photo check last did, from the review cog if it's loaded."""
        cog = self.bot.get_cog("ReviewCog")
        return getattr(cog, "last_check", None)

    async def _adopt_channel(self, interaction: discord.Interaction) -> bool:
        """Claim the channel a setup command was run in, if none is set yet.

        The pinball channel is the one piece of setup with no natural prompt:
        nothing works without it, `/new` refuses until it's there, and the
        person adding tables has no particular reason to expect that. So the
        first setup command adopts the channel it was run in.

        An explicit `/config pinball-channel` always wins — this only fires when
        nothing is configured. The claim is announced in the channel itself, and
        that post *is* the permission check: if the bot can't post there, the
        channel is no use as the pinball channel and the adoption is abandoned
        rather than recorded.
        """
        guild_id = interaction.guild_id
        if guild_id is None or self.store.get_channel_id(guild_id) is not None:
            return False
        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        if channel_id is None or not isinstance(channel, discord.abc.Messageable):
            return False
        try:
            await channel.send(
                "\N{PUSHPIN} I'll post scores and proof photos here — this is now "
                "the tournament channel. Move it any time with "
                "`/config pinball-channel`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.info(
                "guild %s: can't post in channel %s, not adopting it",
                guild_id,
                channel_id,
            )
            return False
        self.store.set_channel_id(guild_id, channel_id)
        self.store.log(
            guild_id,
            actor_id=interaction.user.id,
            action="config.channel",
            detail=f"{channel_id} (adopted automatically)",
        )
        return True

    def _results(
        self, guild_id: int, tournament: Tournament
    ) -> list[tuple[Table, Submission | None]]:
        return [
            (table, self.store.current_king(guild_id, tournament.id, table.id))
            for table in self.store.list_tables(guild_id)
        ]

    async def table_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        if not is_admin(self.store, interaction):
            # An autocomplete can only answer with choices, never a message, so
            # a non-admin gets an empty list rather than an explanation. These
            # three feed admin-only commands; /new has its own in scores.py.
            return []
        needle = current.strip().lower()
        return [
            app_commands.Choice(name=table.name, value=table.name)
            for table in self.store.list_tables(interaction.guild_id, include_inactive=True)
            if needle in table.name.lower()
        ][:25]

    def _candidate_choices(
        self, rows: list[tuple[Submission, str]]
    ) -> list[app_commands.Choice[int]]:
        choices: list[app_commands.Choice[int]] = []
        for submission, table_name in rows:
            label = (
                f"#{submission.id} · {table_name} · {format_score(submission.score)} · "
                f"{submission.user_display} · {_short_date(submission.created_at)}"
            )
            choices.append(app_commands.Choice(name=label[:100], value=submission.id))
        return choices

    async def drop_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """Live scores, highest first — the standing king is what you usually void."""
        if interaction.guild_id is None:
            return []
        if not is_admin(self.store, interaction):
            # An autocomplete can only answer with choices, never a message, so
            # a non-admin gets an empty list rather than an explanation. These
            # three feed admin-only commands; /new has its own in scores.py.
            return []
        tournament = self.store.latest_tournament(interaction.guild_id)
        if tournament is None:
            return []
        return self._candidate_choices(
            self.store.drop_candidates(interaction.guild_id, tournament.id, current)
        )

    async def restore_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """Voided scores, most recently voided first — usually the drop you regret."""
        if interaction.guild_id is None:
            return []
        if not is_admin(self.store, interaction):
            # An autocomplete can only answer with choices, never a message, so
            # a non-admin gets an empty list rather than an explanation. These
            # three feed admin-only commands; /new has its own in scores.py.
            return []
        tournament = self.store.latest_tournament(interaction.guild_id)
        if tournament is None:
            return []
        return self._candidate_choices(
            self.store.restore_candidates(interaction.guild_id, tournament.id, current)
        )

    # ------------------------------------------------------------------- /drop

    @app_commands.command(
        name="drop", description="Admin: void a score and revert to the previous one."
    )
    @app_commands.describe(
        id="Which submission to void (autocompletes, current kings first)",
        reason="Why it's being voided — shown publicly",
    )
    @app_commands.autocomplete(id=drop_autocomplete)
    @app_commands.guild_only()
    async def drop(
        self, interaction: discord.Interaction, id: int, reason: str | None = None
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        submission = self.store.get_submission(guild_id, id)
        if submission is None:
            await interaction.response.send_message(
                f"No submission `#{id}` here.", ephemeral=True
            )
            return
        if submission.is_voided:
            await interaction.response.send_message(
                f"`#{id}` is already voided. Use `/restore` to bring it back.",
                ephemeral=True,
            )
            return
        machine = self.store.get_table(guild_id, submission.table_id)
        table_name = machine.name if machine else "an unknown table"

        view = ConfirmView(interaction.user.id, confirm_label="Void it")
        await interaction.response.send_message(
            f"Void `#{submission.id}` — **{format_score(submission.score)}** by "
            f"<@{submission.user_id}> on **{table_name}**?",
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return

        voided = self.store.void_submission(
            guild_id, submission.id, voided_by=interaction.user.id, reason=reason
        )
        # Existence was checked above, so None here would mean the row vanished
        # between the check and the confirmation.
        if voided is None:
            await interaction.edit_original_response(
                content=f"`#{submission.id}` disappeared before I could void it.",
                view=None,
            )
            return
        new_king = self.store.current_king(
            guild_id, submission.tournament_id, submission.table_id
        )
        self.store.log(
            guild_id,
            actor_id=interaction.user.id,
            action="drop",
            target=f"submission:{submission.id}",
            detail=reason,
        )
        await interaction.edit_original_response(
            content=f"Voided `#{submission.id}`.", view=None
        )
        await self._announce(
            guild_id,
            embed=embeds.void_embed(table_name, voided, new_king, interaction.user.id),
            fallback=interaction.channel,
        )

    # ---------------------------------------------------------------- /restore

    @app_commands.command(name="restore", description="Admin: un-void a score.")
    @app_commands.describe(
        id="Which voided submission to restore (autocompletes, newest void first)"
    )
    @app_commands.autocomplete(id=restore_autocomplete)
    @app_commands.guild_only()
    async def restore(self, interaction: discord.Interaction, id: int) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        submission = self.store.get_submission(guild_id, id)
        if submission is None:
            await interaction.response.send_message(
                f"No submission `#{id}` here.", ephemeral=True
            )
            return
        if not submission.is_voided:
            await interaction.response.send_message(
                f"`#{id}` isn't voided — nothing to restore.", ephemeral=True
            )
            return
        machine = self.store.get_table(guild_id, submission.table_id)
        table_name = machine.name if machine else "an unknown table"

        view = ConfirmView(interaction.user.id, confirm_label="Restore it")
        await interaction.response.send_message(
            f"Restore `#{submission.id}` — **{format_score(submission.score)}** by "
            f"<@{submission.user_id}> on **{table_name}**?",
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return

        restored = self.store.restore_submission(guild_id, submission.id)
        if restored is None:
            await interaction.edit_original_response(
                content=f"`#{submission.id}` disappeared before I could restore it.",
                view=None,
            )
            return
        new_king = self.store.current_king(
            guild_id, submission.tournament_id, submission.table_id
        )
        self.store.log(
            guild_id,
            actor_id=interaction.user.id,
            action="restore",
            target=f"submission:{submission.id}",
        )
        await interaction.edit_original_response(
            content=f"Restored `#{submission.id}`.", view=None
        )
        await self._announce(
            guild_id,
            embed=embeds.restore_embed(
                table_name, restored, new_king, interaction.user.id
            ),
            fallback=interaction.channel,
        )

    # ---------------------------------------------------------------- /history

    @app_commands.command(
        name="history", description="Admin: full submission ledger for one table."
    )
    @app_commands.describe(table="Which machine", limit="How many entries (default 15)")
    @app_commands.autocomplete(table=table_autocomplete)
    @app_commands.guild_only()
    async def history(
        self, interaction: discord.Interaction, table: str, limit: int = 15
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        machine = self.store.get_table_by_name(guild_id, table)
        if machine is None:
            await interaction.response.send_message(
                f"I don't have a table called **{table}**.", ephemeral=True
            )
            return
        tournament = self.store.latest_tournament(guild_id)
        if tournament is None:
            await interaction.response.send_message(
                "No tournament has run here yet.", ephemeral=True
            )
            return
        rows = self.store.history(
            guild_id, tournament.id, machine.id, limit=max(1, min(limit, 40))
        )
        king = self.store.current_king(guild_id, tournament.id, machine.id)
        await interaction.response.send_message(
            embed=embeds.history_embed(machine, rows, king),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------------------------------------------------------------- /flagged

    @app_commands.command(
        name="flagged",
        description="Admin: scores the photo check flagged and nobody has judged yet.",
    )
    @app_commands.guild_only()
    async def flagged(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        tournament = self.store.latest_tournament(guild_id)
        if tournament is None:
            await interaction.response.send_message(
                "No tournament has run here yet.", ephemeral=True
            )
            return
        pending = self.store.pending_flags(guild_id, tournament.id)
        names = {t.id: t.name for t in self.store.list_tables(guild_id, include_inactive=True)}
        await interaction.response.send_message(
            embed=embeds.flagged_embed(pending, names),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ------------------------------------------------------------------ /table

    table_group = app_commands.Group(
        name="table",
        description="Admin: manage the machines in play",
        guild_only=True,
    )

    @table_group.command(name="add", description="Add a machine.")
    @app_commands.describe(name="The machine's name, e.g. Godzilla")
    async def table_add(self, interaction: discord.Interaction, name: str) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        await self._adopt_channel(interaction)
        try:
            table = self.store.add_table(interaction.guild_id, name)
        except StoreError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.store.log(
            interaction.guild_id,
            actor_id=interaction.user.id,
            action="table.add",
            target=f"table:{table.id}",
            detail=table.name,
        )
        count = len(self.store.list_tables(interaction.guild_id))
        await interaction.response.send_message(
            f"Added **{table.name}**. {count} table(s) now in play.", ephemeral=True
        )

    @table_group.command(
        name="remove", description="Retire a machine (its scores are kept)."
    )
    @app_commands.describe(table="Which machine to retire")
    @app_commands.autocomplete(table=table_autocomplete)
    async def table_remove(self, interaction: discord.Interaction, table: str) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        await self._adopt_channel(interaction)
        machine = self.store.get_table_by_name(interaction.guild_id, table)
        if machine is None:
            await interaction.response.send_message(
                f"I don't have a table called **{table}**.", ephemeral=True
            )
            return
        # Deactivate rather than delete: the submissions stay on the ledger and
        # the audit trail stays intact.
        self.store.set_table_active(machine.id, False)
        self.store.log(
            interaction.guild_id,
            actor_id=interaction.user.id,
            action="table.remove",
            target=f"table:{machine.id}",
            detail=machine.name,
        )
        await interaction.response.send_message(
            f"Retired **{machine.name}**. Its scores are still on record — "
            "`/table add` with the same name brings it back.",
            ephemeral=True,
        )

    @table_group.command(name="rename", description="Rename a machine.")
    @app_commands.describe(table="Which machine", new_name="Its new name")
    @app_commands.autocomplete(table=table_autocomplete)
    async def table_rename(
        self, interaction: discord.Interaction, table: str, new_name: str
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        await self._adopt_channel(interaction)
        machine = self.store.get_table_by_name(interaction.guild_id, table)
        if machine is None:
            await interaction.response.send_message(
                f"I don't have a table called **{table}**.", ephemeral=True
            )
            return
        try:
            renamed = self.store.rename_table(interaction.guild_id, machine.id, new_name)
        except StoreError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.store.log(
            interaction.guild_id,
            actor_id=interaction.user.id,
            action="table.rename",
            target=f"table:{machine.id}",
            detail=f"{machine.name} -> {renamed.name}",
        )
        await interaction.response.send_message(
            f"Renamed **{machine.name}** to **{renamed.name}**.", ephemeral=True
        )

    @table_group.command(name="list", description="List the machines in play.")
    async def table_list(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        tables = self.store.list_tables(interaction.guild_id, include_inactive=True)
        if not tables:
            await interaction.response.send_message(
                "No tables yet. Add one with `/table add`.", ephemeral=True
            )
            return
        lines = [
            f"• **{t.name}**" + ("" if t.active else "  _(retired)_") for t in tables
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ----------------------------------------------------------------- /config

    config_group = app_commands.Group(
        name="config", description="Admin: configure the bot for this server", guild_only=True
    )
    admin_role_group = app_commands.Group(
        name="admin-role",
        description="Admin: roles that may run admin commands",
        parent=config_group,
    )

    @config_group.command(
        name="pinball-channel", description="Set the channel scores and photos go to."
    )
    @app_commands.describe(
        channel="Where scores and proof photos go (default: the channel you're in)"
    )
    async def config_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        # Defaulting to the current channel is what makes this command a
        # reliable escape hatch: it is the one command answered in any channel,
        # so "move the tournament here" must not require naming the channel
        # you are standing in.
        target = channel if channel is not None else interaction.channel
        moving_here = channel is None
        if getattr(target, "id", None) is None:
            await interaction.response.send_message(
                "I can't tell which channel you mean — name one, like "
                "`/config pinball-channel #pinball`.",
                ephemeral=True,
            )
            return
        me = getattr(getattr(target, "guild", None), "me", None)
        perms = target.permissions_for(me) if me else None
        if perms and not (perms.send_messages and perms.attach_files and perms.embed_links):
            await interaction.response.send_message(
                f"I need **Send Messages**, **Embed Links**, and **Attach Files** in "
                f"{target.mention}. Grant those and run this again.",
                ephemeral=True,
            )
            return
        self.store.set_channel_id(interaction.guild_id, target.id)
        self.store.log(
            interaction.guild_id,
            actor_id=interaction.user.id,
            action="config.channel",
            detail=str(target.id),
        )
        await interaction.response.send_message(
            ("Scores and proof photos will go here from now on."
             if moving_here
             else f"Scores and proof photos will go to {target.mention}.")
            + " Other pinball commands are answered there too.",
            ephemeral=True,
        )

    @admin_role_group.command(name="add", description="Let a role run admin commands.")
    @app_commands.describe(role="The role to trust")
    async def admin_role_add(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        self.store.add_admin_role(interaction.guild_id, role.id)
        self.store.log(
            interaction.guild_id,
            actor_id=interaction.user.id,
            action="config.admin_role.add",
            detail=str(role.id),
        )
        await interaction.response.send_message(
            f"{role.mention} can now run admin commands.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_role_group.command(name="remove", description="Revoke a role's admin access.")
    @app_commands.describe(role="The role to remove")
    async def admin_role_remove(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        self.store.remove_admin_role(interaction.guild_id, role.id)
        self.store.log(
            interaction.guild_id,
            actor_id=interaction.user.id,
            action="config.admin_role.remove",
            detail=str(role.id),
        )
        await interaction.response.send_message(
            f"{role.mention} no longer has admin access. Anyone with **Manage "
            "Server** still does.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_role_group.command(name="list", description="Show the admin roles.")
    async def admin_role_list(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        ids = self.store.get_admin_role_ids(interaction.guild_id)
        body = ", ".join(f"<@&{rid}>" for rid in ids) if ids else "_none configured_"
        await interaction.response.send_message(
            f"Admin roles: {body}\nAnyone with **Manage Server** is always an admin.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @config_group.command(
        name="vision", description="Toggle the photo/score cross-check (phase 2)."
    )
    @app_commands.describe(enabled="Turn the check on or off")
    async def config_vision(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        from .vision import is_available

        if enabled and not is_available():
            await interaction.response.send_message(
                "The photo check isn't installed on this host — it needs the "
                "`anthropic` package and an `ANTHROPIC_API_KEY`.",
                ephemeral=True,
            )
            return
        self.store.set_vision_enabled(interaction.guild_id, enabled)
        self.store.log(
            interaction.guild_id,
            actor_id=interaction.user.id,
            action="config.vision",
            detail=str(enabled),
        )
        await interaction.response.send_message(
            f"Photo/score cross-check is now **{'on' if enabled else 'off'}**.",
            ephemeral=True,
        )

    @config_group.command(
        name="reset",
        description="Admin: clear all settings, as if the bot had just been added.",
    )
    async def config_reset(self, interaction: discord.Interaction) -> None:
        # Deliberately stricter than the other admin commands: this clears the
        # admin-role list, so a role-only admin running it would revoke their
        # own access. Manage Server holders can always undo it.
        if not await require_manage_guild(interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        channel_id = self.store.get_channel_id(guild_id)
        roles = self.store.get_admin_role_ids(guild_id)
        settings = self.store.setting_count(guild_id)
        if settings == 0:
            await interaction.response.send_message(
                "There's nothing configured here — already a clean slate.",
                ephemeral=True,
            )
            return

        running = self.store.active_tournament(guild_id)

        async def do_it(modal_interaction: discord.Interaction) -> None:
            cleared = self.store.clear_settings(guild_id)
            self.store.log(
                guild_id,
                actor_id=modal_interaction.user.id,
                action="config.reset",
                detail=f"cleared {cleared} setting(s)",
            )
            note = (
                f"Cleared {cleared} setting(s) — pinball channel, admin roles, and "
                "photo-check settings are back to defaults. Only **Manage Server** "
                "grants admin access now.\nTables and scores are untouched; use "
                "`/tournament reset` or `/droptables` for those."
            )
            if running is not None:
                note += (
                    f"\n\n\N{WARNING SIGN} **{running.label}** is still running, and "
                    "`/new` will be refused until you set `/config pinball-channel` "
                    "again."
                )
            await modal_interaction.response.send_message(note, ephemeral=True)

        summary = f"{settings} setting(s)"
        if channel_id:
            summary += ", incl. the pinball channel"
        if roles:
            summary += f" and {len(roles)} admin role(s)"
        await interaction.response.send_modal(
            TypedConfirmModal(
                title="Reset all configuration?",
                phrase=str(settings),
                prompt=f"Type {settings} to clear {summary}"[:45],
                on_confirm=do_it,
            )
        )

    @config_group.command(name="show", description="Show this server's configuration.")
    async def config_show(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        channel_id = self.store.get_channel_id(guild_id)
        roles = self.store.get_admin_role_ids(guild_id)
        tables = self.store.list_tables(guild_id)
        tournament = self.store.latest_tournament(guild_id)

        embed = discord.Embed(title="Pinball bot configuration", color=embeds.BLURPLE)
        embed.add_field(
            name="Pinball channel",
            value=f"<#{channel_id}>" if channel_id else "**not set** — `/config pinball-channel`",
            inline=False,
        )
        embed.add_field(
            name="Admin roles",
            value=", ".join(f"<@&{r}>" for r in roles) or "_none (Manage Server only)_",
            inline=False,
        )
        embed.add_field(
            name=f"Tables ({len(tables)})",
            value="\n".join(f"• {t.name}" for t in tables) or "**none** — `/table add`",
            inline=False,
        )
        if tournament is None:
            state = "no tournament yet — `/tournament start`"
        elif tournament.is_open:
            ends = (
                embeds.ts(tournament.ends_at, "R") if tournament.ends_at else "no scheduled end"
            )
            state = f"**{tournament.label}** running, ends {ends}"
        else:
            state = f"**{tournament.label}** ended {embeds.ts(tournament.ended_at, 'R')}"
        embed.add_field(name="Tournament", value=state, inline=False)
        from .vision import is_available

        if not self.store.get_vision_enabled(guild_id):
            check = "off — `/config vision on`"
        elif not is_available():
            # Enabled in the database but dead on this host. Silently doing
            # nothing is exactly how this gets discovered after an event.
            check = (
                "**on, but not running here** — this host has no `anthropic` "
                "package or `ANTHROPIC_API_KEY`"
            )
        else:
            pending = len(self.store.pending_flags(guild_id, tournament.id)) if tournament else 0
            check = "on"
            if pending:
                check += f" — **{pending}** waiting for review (`/flagged`)"
            last = self._last_check()
            if last is None:
                check += "\n_no photo checked yet since the last restart_"
            else:
                verdict, reasoning, when = last
                detail = f" ({reasoning})" if verdict == "unavailable" and reasoning else ""
                check += f"\nlast check {embeds.ts(when, 'R')}: **{verdict}**{detail}"
        embed.add_field(name="Photo cross-check", value=check, inline=False)
        embed.add_field(
            name="Flag pings",
            value=", ".join(f"<@&{r}>" for r in roles)
            or "_no admin role set — flags will mention the player only_",
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    # ------------------------------------------------------------------ /audit

    audit_group = app_commands.Group(
        name="audit",
        description="Admin: the record of admin actions",
        guild_only=True,
    )

    @audit_group.command(name="show", description="Show recent admin actions.")
    @app_commands.describe(limit="How many entries (default 15, max 40)")
    async def audit_show(self, interaction: discord.Interaction, limit: int = 15) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        entries = self.store.audit_entries(guild_id, limit=max(1, min(limit, 40)))
        await interaction.response.send_message(
            embed=embeds.audit_embed(
                entries,
                self.store.audit_count(guild_id),
                links=self._audit_links(guild_id, entries),
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _audit_links(self, guild_id: int, entries: list) -> dict[str, str]:
        """Jump URLs for the audit entries that point at a proof post."""
        urls = self.store.proof_links(guild_id, embeds.audit_submission_ids(entries))
        return {
            f"{embeds.SUBMISSION_TARGET}{sid}": url for sid, url in urls.items()
        }

    @audit_group.command(
        name="clear", description="Admin: erase the audit trail once the event is settled."
    )
    async def audit_clear(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        count = self.store.audit_count(guild_id)
        if count == 0:
            await interaction.response.send_message(
                "The audit log is already empty.", ephemeral=True
            )
            return

        async def do_it(modal_interaction: discord.Interaction) -> None:
            cleared = self.store.clear_audit(guild_id)
            # Record the clear itself. A log that can be emptied without trace
            # isn't worth keeping, so the new log's first entry says what went.
            self.store.log(
                guild_id,
                actor_id=modal_interaction.user.id,
                action="audit.clear",
                detail=f"erased {cleared} entr(ies)",
            )
            self.store.vacuum()
            await modal_interaction.response.send_message(
                f"Erased {cleared} audit entr(ies). The log now starts fresh with a "
                "single entry recording this clear.",
                ephemeral=True,
            )

        await interaction.response.send_modal(
            TypedConfirmModal(
                title="Erase the audit trail?",
                phrase=str(count),
                prompt=f"Type {count} to erase {count} entr(ies)",
                on_confirm=do_it,
            )
        )

    # ------------------------------------------------------------- /tournament

    tournament_group = app_commands.Group(
        name="tournament",
        description="Admin: open and close the submission window",
        guild_only=True,
    )

    @tournament_group.command(name="start", description="Open the submission window.")
    @app_commands.describe(
        name="Optional name, e.g. Spring Open 2026",
        ends_in="Optional scheduled end, e.g. 36h, 2d, 2d 4h 30m",
    )
    async def tournament_start(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        ends_in: str | None = None,
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        await self._adopt_channel(interaction)

        ends_at: int | None = None
        if ends_in:
            try:
                delta = parse_duration(ends_in)
            except DurationFormatError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            ends_at = now() + int(delta.total_seconds())

        try:
            tournament = self.store.start_tournament(
                guild_id, name=name, started_by=interaction.user.id, ends_at=ends_at
            )
        except InvalidName as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except StoreError as exc:
            await interaction.response.send_message(
                f"{exc} Use `/tournament status` to see it, or `/tournament end` "
                "to close it first.",
                ephemeral=True,
            )
            return

        self.store.log(
            guild_id,
            actor_id=interaction.user.id,
            action="tournament.start",
            target=f"tournament:{tournament.id}",
            detail=name,
        )

        tables = self.store.list_tables(guild_id)
        warnings: list[str] = []
        if not tables:
            warnings.append("No tables are set up yet — add them with `/table add`.")
        if self.store.get_channel_id(guild_id) is None:
            warnings.append(
                "No pinball channel is set — run `/config pinball-channel` or "
                "submissions will be refused."
            )
        confirmation = f"**{tournament.label}** is open."
        if ends_at:
            confirmation += f" Ends {embeds.ts(ends_at, 'f')}."
        if warnings:
            confirmation += "\n\n" + "\n".join(f"\N{WARNING SIGN} {w}" for w in warnings)
        await interaction.response.send_message(confirmation, ephemeral=True)

        await self._announce(
            guild_id,
            embed=embeds.tournament_started_embed(tournament, [t.name for t in tables]),
            fallback=interaction.channel,
        )

    @tournament_group.command(
        name="end", description="Close the window and announce the winners."
    )
    async def tournament_end(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        tournament = self.store.active_tournament(guild_id)
        if tournament is None:
            await interaction.response.send_message(
                "No tournament is running here.", ephemeral=True
            )
            return

        view = ConfirmView(interaction.user.id, confirm_label="End it")
        await interaction.response.send_message(
            f"End **{tournament.label}** now and announce the winners? "
            "No further scores will be accepted (`/tournament extend` reopens it).",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return

        # Last chance to land the final results somewhere permanent: if the
        # channel was never set, this is the most important post of the event.
        await self._adopt_channel(interaction)
        await self._finish(guild_id, tournament, ended_by=interaction.user.id,
                           fallback=interaction.channel)
        await interaction.edit_original_response(
            content=f"**{tournament.label}** is closed and the results are posted.",
            view=None,
        )

    @tournament_group.command(
        name="extend", description="Push the end out, or reopen an ended tournament."
    )
    @app_commands.describe(duration="How much longer, e.g. 2h, 1d, 90m")
    async def tournament_extend(
        self, interaction: discord.Interaction, duration: str
    ) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        try:
            delta = parse_duration(duration)
        except DurationFormatError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        tournament = self.store.latest_tournament(guild_id)
        if tournament is None:
            await interaction.response.send_message(
                "No tournament has run here yet. Start one with `/tournament start`.",
                ephemeral=True,
            )
            return

        # Extend from whichever is later: the existing deadline or now. That way
        # extending a long-expired tournament gives you the full extra time
        # rather than a deadline already in the past.
        await self._adopt_channel(interaction)
        base = max(tournament.ends_at or 0, tournament.ended_at or 0, now())
        new_ends_at = base + int(delta.total_seconds())
        was_closed = not tournament.is_open
        updated = self.store.extend_tournament(guild_id, tournament.id, new_ends_at)
        self.store.log(
            guild_id,
            actor_id=interaction.user.id,
            action="tournament.extend",
            target=f"tournament:{tournament.id}",
            detail=f"+{format_duration(delta)}",
        )

        verb = "reopened and extended" if was_closed else "extended"
        await interaction.response.send_message(
            f"**{updated.label}** {verb} by {format_duration(delta)} — now ends "
            f"{embeds.ts(new_ends_at, 'f')}.",
            ephemeral=True,
        )
        note = (
            f"\N{GAME DIE} **{updated.label}** is open again until "
            f"{embeds.ts(new_ends_at, 'f')} — get your scores in."
            if was_closed
            else f"\N{ALARM CLOCK} **{updated.label}** now runs until "
            f"{embeds.ts(new_ends_at, 'f')}."
        )
        await self._announce(guild_id, content=note, fallback=interaction.channel)

    @tournament_group.command(
        name="reset",
        description="Admin: discard a tournament and its scores without announcing results.",
    )
    async def tournament_reset(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        tournament = self.store.latest_tournament(guild_id)
        if tournament is None:
            await interaction.response.send_message(
                "There's no tournament here to discard.", ephemeral=True
            )
            return
        count = self.store.submission_count(guild_id, tournament.id)
        label = tournament.label

        async def do_it(modal_interaction: discord.Interaction) -> None:
            discarded = self.store.delete_tournament(guild_id, tournament.id)
            self.store.vacuum()
            self.store.log(
                guild_id,
                actor_id=modal_interaction.user.id,
                action="tournament.reset",
                target=f"tournament:{tournament.id}",
                detail=f"discarded {discarded} submissions",
            )
            # Deliberately silent in the channel: the point of discarding a test
            # run is that it never happened, so announcing it defeats the
            # purpose. The audit row is the record.
            await modal_interaction.response.send_message(
                f"Discarded **{label}** and {discarded} score(s). Your tables, "
                "channel, and admin roles are untouched — `/tournament start` "
                "when you're ready.\nThe proof photos are still in the channel; "
                "I don't delete message history.",
                ephemeral=True,
            )

        await interaction.response.send_modal(
            TypedConfirmModal(
                title=f"Discard {label}?",
                phrase=str(count),
                prompt=f"Type {count} to discard {count} score(s)",
                on_confirm=do_it,
            )
        )

    @tournament_group.command(name="status", description="Show the tournament state.")
    async def tournament_status(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        tournament = self.store.latest_tournament(guild_id)
        submissions = (
            self.store.submission_count(guild_id, tournament.id) if tournament else 0
        )
        await interaction.response.send_message(
            embed=embeds.status_embed(
                tournament, submissions, len(self.store.list_tables(guild_id))
            ),
            ephemeral=True,
        )

    async def _finish(
        self,
        guild_id: int,
        tournament: Tournament,
        *,
        ended_by: int,
        fallback: discord.abc.Messageable | None = None,
    ) -> None:
        """Close a tournament and post the final results. Shared by /end and autoclose."""
        results = self._results(guild_id, tournament)
        ended = self.store.end_tournament(guild_id, tournament.id, ended_by=ended_by)
        self.store.log(
            guild_id,
            actor_id=ended_by,
            action="tournament.end",
            target=f"tournament:{tournament.id}",
        )
        await self._announce(
            guild_id,
            embed=embeds.final_results_embed(ended, results),
            fallback=fallback,
        )

    # ------------------------------------------------------ destructive purges

    @app_commands.command(
        name="drophs", description="Admin: delete all high scores for this tournament."
    )
    @app_commands.guild_only()
    async def drophs(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        tournament = self.store.latest_tournament(guild_id)
        if tournament is None:
            await interaction.response.send_message(
                "No tournament has run here yet — nothing to clear.", ephemeral=True
            )
            return
        count = self.store.submission_count(guild_id, tournament.id)
        if count == 0:
            await interaction.response.send_message(
                f"**{tournament.label}** has no scores recorded.", ephemeral=True
            )
            return

        async def do_it(modal_interaction: discord.Interaction) -> None:
            deleted = self.store.drop_all_scores(guild_id, tournament.id)
            self.store.vacuum()
            self.store.log(
                guild_id,
                actor_id=modal_interaction.user.id,
                action="drophs",
                target=f"tournament:{tournament.id}",
                detail=f"deleted {deleted} submissions",
            )
            await modal_interaction.response.send_message(
                f"Deleted {deleted} score(s) from **{tournament.label}**. The proof "
                "photos are still in the channel — I don't delete message history.",
                ephemeral=True,
            )

        await interaction.response.send_modal(
            TypedConfirmModal(
                title="Delete all high scores",
                phrase=str(count),
                prompt=f"Type {count} to delete {count} score(s)",
                on_confirm=do_it,
            )
        )

    @app_commands.command(
        name="droptables",
        description="Admin: delete all tables AND their scores. Implies /drophs.",
    )
    @app_commands.guild_only()
    async def droptables(self, interaction: discord.Interaction) -> None:
        if not await require_admin(self.store, interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        tables = self.store.list_tables(guild_id, include_inactive=True)
        if not tables:
            await interaction.response.send_message(
                "There are no tables to delete.", ephemeral=True
            )
            return
        count = len(tables)

        async def do_it(modal_interaction: discord.Interaction) -> None:
            dropped_tables, dropped_subs = self.store.delete_all_tables(guild_id)
            self.store.vacuum()
            self.store.log(
                guild_id,
                actor_id=modal_interaction.user.id,
                action="droptables",
                detail=f"deleted {dropped_tables} tables, {dropped_subs} submissions",
            )
            await modal_interaction.response.send_message(
                f"Deleted {dropped_tables} table(s) and {dropped_subs} score(s). "
                "Add tables again with `/table add`. The proof photos are still in "
                "the channel — I don't delete message history.",
                ephemeral=True,
            )

        await interaction.response.send_modal(
            TypedConfirmModal(
                title="Delete all tables and scores",
                phrase=str(count),
                prompt=f"Type {count} to delete {count} table(s) + scores",
                on_confirm=do_it,
            )
        )

    @app_commands.command(
        name="reset-all",
        description="Admin: factory reset — scores, tables, tournaments, config, audit log.",
    )
    @app_commands.guild_only()
    async def reset_all(self, interaction: discord.Interaction) -> None:
        # Manage Server only, for the same reason as /config reset: this clears
        # the admin-role list, so a role-only admin would be revoking their own
        # access with nothing left to grant it back.
        if not await require_manage_guild(interaction):
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id

        totals = self.store.guild_totals(guild_id)
        if totals.total == 0:
            await interaction.response.send_message(
                "There's nothing here to reset — this server is already a clean slate.",
                ephemeral=True,
            )
            return
        running = self.store.active_tournament(guild_id)

        async def do_it(modal_interaction: discord.Interaction) -> None:
            wiped = self.store.wipe_guild(guild_id)
            # Same reasoning as /audit clear: the wipe itself is the first entry
            # in the new log, so a factory reset can't be done without a trace.
            self.store.log(
                guild_id,
                actor_id=modal_interaction.user.id,
                action="reset.all",
                detail=(
                    f"wiped {wiped.submissions} score(s), {wiped.tables} table(s), "
                    f"{wiped.tournaments} tournament(s), {wiped.settings} setting(s), "
                    f"{wiped.audit} audit entr(ies)"
                ),
            )
            self.store.vacuum()
            note = (
                f"\N{BROOM} **Factory reset complete.**\n"
                f"• {wiped.submissions} score(s)\n"
                f"• {wiped.tables} table(s)\n"
                f"• {wiped.tournaments} tournament(s)\n"
                f"• {wiped.settings} setting(s) — pinball channel and admin roles "
                "included, so only **Manage Server** grants admin access now\n"
                f"• {wiped.audit} audit entr(ies)\n\n"
                "This server is back to how it was the moment I joined. Start over "
                "with `/config pinball-channel`, `/table add`, `/tournament start`.\n"
                "The proof photos are still in the channel — I don't delete message "
                "history."
            )
            if running is not None:
                note += (
                    f"\n\n\N{WARNING SIGN} **{running.label}** was still running and "
                    "was discarded without final results."
                )
            await modal_interaction.response.send_message(note, ephemeral=True)

        # The typed number is the grand total across every table, so it can't be
        # guessed from any one command's output — you have to read this prompt.
        title = (
            f"Factory reset — discards {running.label}!"
            if running is not None
            else "Factory reset: erase everything?"
        )
        await interaction.response.send_modal(
            TypedConfirmModal(
                title=title,
                phrase=str(totals.total),
                prompt=f"Type {totals.total} to erase all of it",
                on_confirm=do_it,
            )
        )

    # ------------------------------------------------------------- auto-close

    @tasks.loop(seconds=30)
    async def autoclose(self) -> None:
        """Close tournaments whose scheduled end has passed.

        The only background work the process does. Deliberately cheap: one
        indexed query every 30 seconds, and nothing to do in the common case.
        """
        try:
            due = self.store.due_tournaments()
        except Exception:  # noqa: BLE001 - a loop that dies stops closing tournaments
            log.exception("autoclose query failed")
            return
        for tournament in due:
            try:
                await self._finish(
                    tournament.guild_id,
                    tournament,
                    ended_by=self.bot.user.id if self.bot.user else 0,
                )
                log.info(
                    "auto-closed tournament %s in guild %s",
                    tournament.id,
                    tournament.guild_id,
                )
            except Exception:  # noqa: BLE001 - one bad guild must not stall the rest
                log.exception("failed to auto-close tournament %s", tournament.id)

    @autoclose.before_loop
    async def before_autoclose(self) -> None:
        await self.bot.wait_until_ready()


async def setup_admin(
    bot: commands.Bot, store: Store, urls: proofs.ProofURLCache
) -> AdminCog:
    cog = AdminCog(bot, store, urls)
    await bot.add_cog(cog)
    return cog
