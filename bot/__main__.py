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

        if self.config.dev_guild_id:
            # Guild-scoped sync applies instantly, which makes iterating on
            # command definitions tolerable. Global sync can take an hour.
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to dev guild %s", len(synced), self.config.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("synced %d commands globally", len(synced))

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
