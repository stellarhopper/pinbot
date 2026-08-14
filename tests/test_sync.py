"""Which scope the command tree gets published to.

Discord merges a guild's commands with the global set, so registering the same
tree in both scopes shows every command twice in the picker. Getting this wrong
is invisible in code review and obvious to every player at once, which is why
the switching is covered here rather than left to a README note.
"""

from __future__ import annotations

import pytest

from bot.__main__ import publish_commands
from bot.store import Store

DEV_GUILD = 1420254013458223158
OTHER_GUILD = 777


class FakeTree:
    """Records what was published where, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def copy_global_to(self, *, guild) -> None:
        self.calls.append(("copy", guild.id))

    def clear_commands(self, *, guild) -> None:
        self.calls.append(("clear", guild.id if guild else None))

    async def sync(self, *, guild=None):
        self.calls.append(("sync", guild.id if guild else None))
        return []

    def synced(self, scope: int | None) -> bool:
        return ("sync", scope) in self.calls


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "sync.db")
    yield s
    s.close()


async def test_production_publishes_globally(store):
    tree = FakeTree()
    await publish_commands(tree, store, None)

    assert tree.calls == [("sync", None)]
    assert store.dev_synced_guilds() == []


async def test_dev_mode_publishes_to_the_guild_and_retracts_the_global_set(store):
    tree = FakeTree()
    await publish_commands(tree, store, DEV_GUILD)

    # The global set has to be retracted, or every command shows up twice in
    # the dev guild — once from the guild copy, once from the global one.
    assert tree.calls == [
        ("copy", DEV_GUILD),
        ("sync", DEV_GUILD),
        ("clear", None),
        ("sync", None),
    ]
    assert store.dev_synced_guilds() == [DEV_GUILD]


async def test_the_copy_happens_before_the_global_clear(store):
    """clear_commands empties the local tree, so the other order publishes an
    empty command list to the dev guild — a bot with no commands at all."""
    tree = FakeTree()
    await publish_commands(tree, store, DEV_GUILD)

    assert tree.calls.index(("copy", DEV_GUILD)) < tree.calls.index(("clear", None))


async def test_leaving_dev_mode_retracts_the_guild_copy(store):
    """The reason the guild is remembered in the database: by now DEV_GUILD_ID
    is gone from the environment, and nothing else knows where to clean up."""
    await publish_commands(FakeTree(), store, DEV_GUILD)

    tree = FakeTree()
    await publish_commands(tree, store, None)

    assert tree.calls == [
        ("clear", DEV_GUILD),
        ("sync", DEV_GUILD),
        ("sync", None),
    ]
    assert store.dev_synced_guilds() == [], "and it stops being remembered"


async def test_restarting_in_dev_mode_does_not_churn(store):
    await publish_commands(FakeTree(), store, DEV_GUILD)

    tree = FakeTree()
    await publish_commands(tree, store, DEV_GUILD)

    assert ("clear", DEV_GUILD) not in tree.calls, "the dev guild is not retracted"
    assert store.dev_synced_guilds() == [DEV_GUILD]


async def test_moving_the_dev_guild_cleans_up_the_old_one(store):
    await publish_commands(FakeTree(), store, OTHER_GUILD)

    tree = FakeTree()
    await publish_commands(tree, store, DEV_GUILD)

    assert tree.calls[:2] == [("clear", OTHER_GUILD), ("sync", OTHER_GUILD)]
    assert store.dev_synced_guilds() == [DEV_GUILD], "only the new one is live"


def test_the_dev_marker_is_not_guild_configuration(store):
    """It lives in `meta`, so /config show never mentions it and /config reset
    can't clear it — it is the bot's bookkeeping, not a server's setting."""
    store.set_dev_synced_guilds([DEV_GUILD])

    assert store.setting_count(DEV_GUILD) == 0
    assert store.guild_totals(DEV_GUILD).total == 0
    store.clear_settings(DEV_GUILD)
    assert store.dev_synced_guilds() == [DEV_GUILD]


def test_a_corrupt_marker_is_ignored_rather_than_fatal(store):
    """A bad value here must not stop the bot from starting."""
    store.set_meta("dev_synced_guilds", "not json")
    assert store.dev_synced_guilds() == []
