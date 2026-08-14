"""Who counts as a tournament admin.

Two independent grants, either of which is sufficient:

* the Discord **Manage Server** permission — the permanent bootstrap, so a
  server owner can never lock themselves out of their own tournament, and
* membership in any role on the guild's admin-role allowlist — which lets an
  existing local admin/staff role referee without being handed full server
  management.

The decision itself is a pure function so it can be unit tested without
constructing Discord objects.
"""

from __future__ import annotations

from collections.abc import Iterable

import discord

from .store import Store


def has_admin_access(
    *, manage_guild: bool, member_role_ids: Iterable[int], admin_role_ids: Iterable[int]
) -> bool:
    if manage_guild:
        return True
    allowed = set(admin_role_ids)
    return any(role_id in allowed for role_id in member_role_ids)


def is_admin(store: Store, interaction: discord.Interaction) -> bool:
    if interaction.guild_id is None:
        return False
    member = interaction.user
    if not isinstance(member, discord.Member):
        # A guild-only command reached us with a bare User, which happens when
        # the member cache is cold. Fail closed.
        return False
    return has_admin_access(
        manage_guild=member.guild_permissions.manage_guild,
        member_role_ids=[role.id for role in member.roles],
        admin_role_ids=store.get_admin_role_ids(interaction.guild_id),
    )


def has_manage_guild(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return (
        isinstance(member, discord.Member) and member.guild_permissions.manage_guild
    )


DENIED = (
    "That command is for tournament admins. You need the **Manage Server** "
    "permission, or a role added with `/config admin-role add`."
)

MANAGE_GUILD_ONLY = (
    "This one needs the **Manage Server** permission specifically, not just an "
    "admin role. It clears the admin-role list, so allowing a role-only admin "
    "to run it would let them revoke their own access with no way back."
)


async def require_admin(store: Store, interaction: discord.Interaction) -> bool:
    """Check admin access, replying with an explanation if it fails.

    Returns True when the caller may proceed.
    """
    if is_admin(store, interaction):
        return True
    if interaction.response.is_done():
        await interaction.followup.send(DENIED, ephemeral=True)
    else:
        await interaction.response.send_message(DENIED, ephemeral=True)
    return False


async def require_manage_guild(interaction: discord.Interaction) -> bool:
    """Stricter gate for commands that can revoke the caller's own access."""
    if has_manage_guild(interaction):
        return True
    if interaction.response.is_done():
        await interaction.followup.send(MANAGE_GUILD_ONLY, ephemeral=True)
    else:
        await interaction.response.send_message(MANAGE_GUILD_ONLY, ephemeral=True)
    return False
