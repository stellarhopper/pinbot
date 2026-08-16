"""Verify the Discord setup before running the bot: python -m bot.checkup

Logs in with the token in .env, prints the application it belongs to, generates
the exact invite URL for this bot's permissions, and lists the servers it has
been added to — plus, for each, whether the pinball channel is configured and
whether the bot can actually post there.

Only makes read-only HTTP calls. Never prints the token.
"""

from __future__ import annotations

import asyncio
import sys

import discord

from .config import Config, load_env_file
from .store import Store

# Exactly what the bot needs: post score announcements with photos, re-read its
# own proof messages to refresh expired image URLs, and put the review buttons
# on a flagged photo. Both the invite URL and the per-channel audit below are
# derived from this, so adding a permission here is the only edit needed.
REQUIRED = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    add_reactions=True,
)

OK = "\N{WHITE HEAVY CHECK MARK}"
BAD = "\N{CROSS MARK}"
WARN = "\N{WARNING SIGN}"

# A headless Pi reached over SSH hands us whatever locale the client sent,
# which is often latin-1 — and this tool died with a UnicodeEncodeError on its
# very first tick mark. A diagnostic that only runs on a UTF-8 terminal is
# useless exactly where it is needed most.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def invite_url(app_id: int) -> str:
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id}"
        f"&permissions={REQUIRED.value}&scope=bot+applications.commands"
    )


async def main() -> int:
    load_env_file()
    config = Config()

    if not config.token:
        print(f"{BAD} DISCORD_TOKEN is not set.")
        print("   Copy .env.example to .env and paste your bot token into it.")
        print("   (Developer Portal -> your app -> Bot -> Reset Token)")
        return 1

    client = discord.Client(intents=discord.Intents.default())
    try:
        try:
            await client.login(config.token)
        except discord.LoginFailure:
            print(f"{BAD} Discord rejected that token.")
            print("   Reset it in the Developer Portal (Bot -> Reset Token) and")
            print("   make sure you copied the *bot* token, not the Client Secret.")
            return 1
        except discord.HTTPException as exc:
            print(f"{BAD} Couldn't reach Discord: {exc}")
            return 1

        # login() populates client.user over HTTP, without a gateway connection.
        if client.user is None:
            print(f"{BAD} Logged in, but Discord didn't return the bot user.")
            print("   Transient — try again in a moment.")
            return 1
        bot_user = client.user

        app = await client.application_info()
        print(f"{OK} Token is valid.")
        print(f"   Application: {app.name}  (ID {app.id})")
        print(f"   Bot user:    {bot_user}  (ID {bot_user.id})")
        if app.owner is not None:
            print(f"   Owner:       {app.owner}")

        # Public Bot must be on to add it to servers you don't own.
        if app.bot_public:
            print(f"{OK} Public Bot is on — anyone can add it to their server.")
        else:
            print(f"{WARN} Public Bot is OFF — only you can add it to servers.")
            print("     Turn it on under Bot if you want others to use it.")

        # This one silently breaks invites, and the error Discord shows is unhelpful.
        if app.bot_require_code_grant:
            print(f"{BAD} 'Requires OAuth2 Code Grant' is ON — invites will fail.")
            print("     Turn it OFF under Bot. This is a common footgun.")
        else:
            print(f"{OK} Requires OAuth2 Code Grant is off (correct).")

        print()
        print("Invite URL — open this to add the bot to a server:")
        print(f"   {invite_url(app.id)}")

        print()
        guilds = [g async for g in client.fetch_guilds(limit=200)]
        if not guilds:
            print(f"{WARN} Not in any servers yet. Open the invite URL above.")
        else:
            store = Store(config.db_path)
            try:
                print(f"In {len(guilds)} server(s):")
                for guild in guilds:
                    await report_guild(client, store, guild, bot_user.id)
            finally:
                store.close()

        if config.dev_guild_id:
            ids = {g.id for g in guilds}
            if config.dev_guild_id in ids:
                print()
                print(f"{OK} DEV_GUILD_ID is set to a server the bot is in —")
                print("   slash commands will sync there instantly.")
            else:
                print()
                print(f"{WARN} DEV_GUILD_ID={config.dev_guild_id} is not a server the")
                print("     bot is in. Commands will not appear. Fix or unset it.")
        else:
            print()
            print(f"{WARN} DEV_GUILD_ID is not set, so commands sync globally and can")
            print("     take up to an hour to appear. Set it while testing.")
    finally:
        await client.close()

    print()
    print("Next: .venv/bin/python -m bot")
    return 0


async def report_guild(
    client: discord.Client, store: Store, guild: discord.Guild, bot_user_id: int
) -> None:
    print(f"   • {guild.name}  (ID {guild.id})")
    channel_id = store.get_channel_id(guild.id)
    if channel_id is None:
        print(f"     {WARN} no pinball channel set — run /config pinball-channel")
        return

    # A guild from fetch_guilds() — or the one hanging off fetch_channel() — has
    # an empty role cache, and permissions_for() against that returns fiction
    # rather than raising. fetch_guild() populates the roles, so the computation
    # is against real data.
    try:
        full = await client.fetch_guild(guild.id)
        channels = await full.fetch_channels()
    except discord.HTTPException:
        print(f"     {WARN} could not read this server's channels")
        return

    channel = next((c for c in channels if c.id == channel_id), None)
    if channel is None:
        print(f"     {BAD} configured channel {channel_id} is gone or hidden from me")
        return

    try:
        me = await full.fetch_member(bot_user_id)
    except discord.HTTPException:
        print(f"     {WARN} #{channel.name} configured, but I can't read my own member")
        return

    have = channel.permissions_for(me)
    missing = [name for name, value in REQUIRED if value and not getattr(have, name)]
    if missing:
        print(f"     {BAD} #{channel.name}: missing {', '.join(sorted(missing))}")
    else:
        print(f"     {OK} #{channel.name}: all required permissions present")

    # Read Message History is the one whose absence silently costs /hs its
    # photos, so confirm it by doing what /hs does rather than by arithmetic.
    if isinstance(channel, discord.abc.Messageable):
        try:
            [_ async for _ in channel.history(limit=1)]
            print(f"     {OK} #{channel.name}: can re-read proof photos for /hs")
        except discord.Forbidden:
            print(
                f"     {BAD} #{channel.name}: cannot read message history, so /hs "
                "will show proof links instead of photos"
            )
        except discord.HTTPException:
            print(f"     {WARN} #{channel.name}: could not verify message history access")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
