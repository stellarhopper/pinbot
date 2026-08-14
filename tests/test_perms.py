"""Admin access: Manage Server OR an allowlisted role, never neither."""

from __future__ import annotations

from bot.perms import has_admin_access


def test_manage_server_is_always_enough():
    assert has_admin_access(manage_guild=True, member_role_ids=[], admin_role_ids=[])


def test_manage_server_works_even_with_an_allowlist_configured():
    # The bootstrap must never be lockable-out, or a server owner can lose
    # control of their own tournament by misconfiguring roles.
    assert has_admin_access(
        manage_guild=True, member_role_ids=[1], admin_role_ids=[999]
    )


def test_an_allowlisted_role_is_enough():
    assert has_admin_access(
        manage_guild=False, member_role_ids=[5, 6, 7], admin_role_ids=[7]
    )


def test_neither_grant_is_refused():
    assert not has_admin_access(
        manage_guild=False, member_role_ids=[5, 6], admin_role_ids=[7]
    )


def test_no_allowlist_means_manage_server_only():
    assert not has_admin_access(
        manage_guild=False, member_role_ids=[5, 6], admin_role_ids=[]
    )


def test_empty_membership_is_refused():
    assert not has_admin_access(
        manage_guild=False, member_role_ids=[], admin_role_ids=[7]
    )
