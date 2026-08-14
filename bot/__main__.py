"""Entrypoint: python -m bot"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .admin import setup_admin
from .config import Config, load_env_file
from .proofs import ProofURLCache
from .scores import setup_scores
from .store import Store
from .tree import PinballTree

log = logging.getLogger("pinball")


async def publish_commands(tree, store: Store, dev_guild_id: int | None) -> None:
    """Publish the command tree, in exactly one scope.

    Discord *merges* a guild's own commands with the global set, so a tree
    registered in both scopes shows every command **twice** in the picker.
    Switching between dev and production therefore has to retract the scope it
    is leaving, not just publish the one it is entering — and by the time you
    leave dev mode, DEV_GUILD_ID is gone from the environment, so the guild it
    pointed at is remembered in the database instead.
    """
    for guild_id in store.dev_synced_guilds():
        if guild_id == dev_guild_id:
            continue  # still the dev guild; nothing to retract
        stale = discord.Object(id=guild_id)
        tree.clear_commands(guild=stale)
        await tree.sync(guild=stale)
        log.info("removed the guild-scoped commands from guild %s", guild_id)

    if dev_guild_id:
        # Guild-scoped sync applies instantly, which makes iterating on command
        # definitions tolerable. Global sync can take an hour.
        guild = discord.Object(id=dev_guild_id)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        store.set_dev_synced_guilds([dev_guild_id])
        # Copy first, then retract the global set: clear_commands drops the
        # commands from the local tree, so the other order would leave nothing
        # to copy.
        tree.clear_commands(guild=None)
        await tree.sync()
        log.info(
            "synced %d commands to dev guild %s (global set retracted)",
            len(synced),
            dev_guild_id,
        )
    else:
        synced = await tree.sync()
        store.set_dev_synced_guilds([])
        log.info("synced %d commands globally", len(synced))


class PinballBot(commands.Bot):
    def __init__(self, config: Config, store: Store) -> None:
        # Slash commands only, so no message content intent is needed — the bot
        # never reads message text. discord.py warns about the missing intent on
        # startup; it applies to prefix commands, which this bot doesn't use.
        super().__init__(
            command_prefix="!pinball-unused ",
            intents=discord.Intents.default(),
            # Turns an unhandled command failure into a message the player can
            # act on, instead of "The application did not respond".
            tree_cls=PinballTree,
        )
        self.config = config
        self.store = store
        self.urls = ProofURLCache()

    async def setup_hook(self) -> None:
        await setup_scores(self, self.store, self.urls)
        await setup_admin(self, self.store, self.urls)
        await self.sync_commands()

    async def sync_commands(self) -> None:
        await publish_commands(self.tree, self.store, self.config.dev_guild_id)

    async def on_ready(self) -> None:
        log.info("logged in as %s in %d guild(s)", self.user, len(self.guilds))

    async def close(self) -> None:
        await super().close()
        self.store.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_env_file()
    config = Config()
    token = config.require_token()
    store = Store(config.db_path)
    log.info("database at %s", config.db_path)

    bot = PinballBot(config, store)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
