"""Avatar resolution for the standings embeds.

Regression cover: the first six scores of the real tournament were recorded
before the avatar was snapshotted, so every one of them had nothing to show and
`/hs` displayed no faces at all. A snapshot is the fast path, not the only path.
"""

from __future__ import annotations

import types

import discord
import pytest

from bot.avatars import AvatarCache

from test_embeds import sub

FETCHED = "https://cdn.discordapp.com/avatars/1/fetched.png"
CACHED = "https://cdn.discordapp.com/avatars/1/from-cache.png"


class FakeClient:
    """Counts lookups so tests can prove what did and didn't hit the API."""

    def __init__(self, *, in_cache: bool = False, fails: bool = False) -> None:
        self.in_cache = in_cache
        self.fails = fails
        self.get_calls = 0
        self.fetch_calls = 0

    def get_user(self, user_id: int):
        self.get_calls += 1
        if not self.in_cache:
            return None
        return types.SimpleNamespace(display_avatar=types.SimpleNamespace(url=CACHED))

    async def fetch_user(self, user_id: int):
        self.fetch_calls += 1
        if self.fails:
            raise discord.NotFound(
                types.SimpleNamespace(status=404, reason="Not Found"), "unknown user"
            )
        return types.SimpleNamespace(display_avatar=types.SimpleNamespace(url=FETCHED))


async def test_a_snapshotted_avatar_costs_no_lookup():
    client = FakeClient()
    cache = AvatarCache()
    url = await cache.url_for(client, sub(1_000, avatar="https://snap/shot.png"))
    assert url == "https://snap/shot.png"
    assert (client.get_calls, client.fetch_calls) == (0, 0)


async def test_a_legacy_row_falls_back_to_asking_discord():
    """The exact case that showed no avatar: user_avatar is NULL."""
    client = FakeClient()
    cache = AvatarCache()
    assert await cache.url_for(client, sub(1_000, avatar=None)) == FETCHED
    assert client.fetch_calls == 1


async def test_the_member_cache_is_preferred_over_an_http_call():
    client = FakeClient(in_cache=True)
    cache = AvatarCache()
    assert await cache.url_for(client, sub(1_000, avatar=None)) == CACHED
    assert client.fetch_calls == 0, "no HTTP call when the user is already cached"


async def test_a_resolved_avatar_is_cached():
    client = FakeClient()
    cache = AvatarCache()
    for _ in range(4):
        await cache.url_for(client, sub(1_000, avatar=None))
    assert client.fetch_calls == 1, "four tables must not mean four API calls"


async def test_a_failed_lookup_is_cached_too():
    """A deleted account shouldn't be re-fetched on every /hs."""
    client = FakeClient(fails=True)
    cache = AvatarCache()
    assert await cache.url_for(client, sub(1_000, avatar=None)) is None
    assert await cache.url_for(client, sub(1_000, avatar=None)) is None
    assert client.fetch_calls == 1


async def test_the_cache_expires():
    client = FakeClient()
    cache = AvatarCache(ttl_seconds=0)
    await cache.url_for(client, sub(1_000, avatar=None))
    await cache.url_for(client, sub(1_000, avatar=None))
    assert client.fetch_calls == 2


async def test_no_submission_means_no_avatar():
    client = FakeClient()
    assert await AvatarCache().url_for(client, None) is None
    assert (client.get_calls, client.fetch_calls) == (0, 0)


@pytest.mark.parametrize("avatar", ["", None])
async def test_a_blank_snapshot_is_treated_as_missing(avatar):
    client = FakeClient()
    assert await AvatarCache().url_for(client, sub(1_000, avatar=avatar)) == FETCHED
