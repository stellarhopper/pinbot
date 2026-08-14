"""Resolving a player's avatar for the standings embeds.

Submissions snapshot the avatar URL at submit time, which costs no API call and
survives the player leaving the server. Two cases need a fallback anyway:

* scores recorded before the ``user_avatar`` column existed, and
* a snapshot that has gone stale because the player changed their avatar.

So: snapshot first, ask Discord otherwise, and cache the answer — including a
failure, so a deleted account isn't re-fetched on every ``/hs``.
"""

from __future__ import annotations

import logging
import time

import discord

from .store import Submission

log = logging.getLogger(__name__)


class AvatarCache:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self.ttl = ttl_seconds
        self._cache: dict[int, tuple[float, str | None]] = {}

    async def url_for(
        self, client: discord.Client, submission: Submission | None
    ) -> str | None:
        if submission is None:
            return None
        if submission.user_avatar:
            return submission.user_avatar
        return await self.for_user(client, submission.user_id)

    async def for_user(self, client: discord.Client, user_id: int) -> str | None:
        cached = self._cache.get(user_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        url: str | None = None
        user = client.get_user(user_id)
        if user is None:
            try:
                user = await client.fetch_user(user_id)
            except discord.HTTPException:
                user = None
        if user is not None:
            url = str(user.display_avatar.url)

        self._cache[user_id] = (time.monotonic() + self.ttl, url)
        return url
