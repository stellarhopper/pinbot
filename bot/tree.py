"""Command tree with error handling and dispatch-latency instrumentation.

Without an `on_error` override, discord.py logs a traceback and the player sees
"The application did not respond" — no explanation, and no hint about whether
their score landed. Everything here exists to make a failure legible from both
ends.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from . import channels

log = logging.getLogger(__name__)

# Discord's JSON error code for an interaction it no longer knows about. Means
# the 3-second acknowledgement window elapsed before we responded; the token is
# dead and nothing can be sent on it.
UNKNOWN_INTERACTION = 10062

# An interaction must be acknowledged within 3s. Log anything that reaches the
# handler with less than a second to spare — by the time a defer 404s, the
# evidence of *why* is already gone.
SLOW_DISPATCH_SECONDS = 2.0

GENERIC_FAILURE = (
    "Something went wrong on my end. Check `/hs` to see whether your score "
    "landed before trying again — and tell an admin if this keeps happening."
)


def unwrap(error: BaseException) -> BaseException:
    """Strip discord.py's CommandInvokeError wrapper to get the real cause."""
    while isinstance(
        error, (app_commands.CommandInvokeError, app_commands.TransformerError)
    ) and error.__cause__ is not None:
        error = error.__cause__
    return error


def is_expired_interaction(error: BaseException) -> bool:
    """True when the failure is 'we were too slow', not 'the command is broken'."""
    original = unwrap(error)
    return (
        isinstance(original, discord.NotFound)
        and getattr(original, "code", None) == UNKNOWN_INTERACTION
    )


def dispatch_lag(interaction: discord.Interaction) -> float:
    """Seconds between Discord creating the interaction and us handling it."""
    return (discord.utils.utcnow() - interaction.created_at).total_seconds()


class PinballTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        self._measure_lag(interaction)
        return await self._right_channel(interaction)

    @staticmethod
    def _measure_lag(interaction: discord.Interaction) -> None:
        """Observe only. A slow-dispatch warning distinguishes a blocked event
        loop or a slow gateway from a genuinely broken command, which a 404
        alone cannot."""
        lag = dispatch_lag(interaction)
        if lag >= SLOW_DISPATCH_SECONDS:
            log.warning(
                "slow dispatch: %s reached the handler %.2fs after Discord created "
                "it (the acknowledgement window is 3s)",
                interaction.command.qualified_name if interaction.command else "interaction",
                lag,
            )

    async def _right_channel(self, interaction: discord.Interaction) -> bool:
        """Answer only in the guild's pinball channel, once one is set.

        Returning False makes discord.py abandon the invocation without raising,
        so the redirect below is the only thing the user sees.
        """
        if interaction.type is discord.InteractionType.autocomplete:
            # An autocomplete can only be answered with choices, never a
            # message. Let it through — the invocation it belongs to is what
            # gets stopped, and stopping it here would just hang the picker.
            return True

        store = getattr(interaction.client, "store", None)
        if interaction.guild_id is None or store is None:
            return True

        command = interaction.command
        if channels.is_exempt(command.qualified_name if command else None):
            return True

        configured = store.get_channel_id(interaction.guild_id)
        if channels.is_allowed(
            configured_id=configured,
            channel_id=interaction.channel_id,
            parent_id=getattr(interaction.channel, "parent_id", None),
        ):
            return True

        if interaction.client.get_channel(configured) is None:
            # The configured channel is deleted, archived, or no longer visible.
            # Redirecting to it would strand the guild in a channel that can't
            # answer, so fail open and let the command run where it was typed.
            log.warning(
                "guild %s: pinball channel %s is unreachable — not redirecting",
                interaction.guild_id,
                configured,
            )
            return True

        await self.notify(interaction, channels.elsewhere_notice(configured))
        return False

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        name = interaction.command.qualified_name if interaction.command else "unknown"

        if is_expired_interaction(error):
            # The token is dead, so there is no way to tell the user anything.
            # One line, no traceback: this is a transient, not a bug.
            log.warning(
                "/%s expired before it could be acknowledged (%.2fs lag, 3s limit) — "
                "nothing was recorded; the player saw no response",
                name,
                dispatch_lag(interaction),
            )
            return

        log.exception("/%s failed", name, exc_info=unwrap(error))
        await self.notify(interaction, GENERIC_FAILURE)

    @staticmethod
    async def notify(interaction: discord.Interaction, message: str) -> None:
        """Best-effort user-facing message. Never raises."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("could not deliver the failure notice to the player")
