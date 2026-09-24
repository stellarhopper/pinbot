"""The live scoreboard: top 3 per table, published to MQTT for the venue TV.

The bot publishes; a listener at the venue (``display/server.py``) subscribes
and serves the page. Both ends make outbound connections to the broker, so
nothing has to be reachable at home or at the event.

**Poll and compare, not hooks.** Around fifteen code paths change the standings
— /new, void, restore, the ✅/❌ review, table edits, tournament start/end/
delete, drop-all, wipe — and a publish hook at each would be one more thing
for the next command to forget. Instead the snapshot is rebuilt every second
and published only when it differs from the last one. That is a handful of
indexed reads per table per second, and it cannot miss a change.

**Retained messages.** The broker keeps the latest snapshot per guild, so a
listener that starts, restarts, or loses Wi-Fi has the current board the moment
it reconnects, without the bot having to notice.

**Avatars travel as their own messages**, as raw image bytes on
``<topic>/<guild>/avatar/<user>``, sent once per player and again only when the
avatar changes. Keeping them out of the snapshot keeps every score update a
couple of kilobytes, and the venue never depends on Discord's CDN or on an
avatar URL that has since gone stale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from discord.ext import commands, tasks

from .config import Config
from .embeds import table_color
from .store import Store, Submission, Table, Tournament

log = logging.getLogger(__name__)

TOP_N = 3
# A player whose avatar couldn't be fetched shows initials; try again after this
# rather than every second.
AVATAR_RETRY_SECONDS = 600

FetchAvatar = Callable[[str], Awaitable[bytes]]
ChannelName = Callable[[int], str | None]


# ------------------------------------------------------------------ snapshot


def avatar_key(submission: Submission) -> str | None:
    """Which version of a player's avatar this score was posted with.

    Derived from the snapshotted URL, whose path carries Discord's avatar hash,
    so it changes exactly when the picture does — and costs no API call.
    """
    if not submission.user_avatar:
        return None
    path = urlsplit(submission.user_avatar).path
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def avatar_fetch_url(url: str) -> str:
    """The snapshotted avatar URL, asked for as a small WebP.

    Discord's default avatars (``/embed/avatars/N.png``) only come as PNG, so
    those keep their extension; everything else is re-requested as WebP.
    """
    parts = urlsplit(url)
    path = parts.path
    if "/embed/avatars/" not in path:
        path = re.sub(r"\.(png|jpe?g|gif|webp)$", ".webp", path)
    return urlunsplit((parts.scheme, parts.netloc, path, "size=128", ""))


def _top_submissions(
    store: Store, guild_id: int
) -> tuple[Tournament | None, list[tuple[Table, list[Submission]]]]:
    tournament = store.latest_tournament(guild_id)
    return tournament, [
        (
            table,
            store.standings(guild_id, tournament.id, table.id, limit=TOP_N)
            if tournament
            else [],
        )
        for table in store.list_tables(guild_id)
    ]


def _latest_per_player(
    tables: list[tuple[Table, list[Submission]]],
) -> dict[int, Submission]:
    """Each player's most recent score on the board.

    A player can hold several slots, posted with different avatars; the board
    shows one face per player, and it should be the one they have now.
    """
    latest: dict[int, Submission] = {}
    for _, top in tables:
        for s in top:
            held = latest.get(s.user_id)
            if held is None or (s.created_at, s.id) > (held.created_at, held.id):
                latest[s.user_id] = s
    return latest


def _snapshot(
    guild_id: int,
    guild_name: str,
    tournament: Tournament | None,
    tables: list[tuple[Table, list[Submission]]],
    channel: str | None = None,
) -> dict[str, Any]:
    latest = _latest_per_player(tables)
    return {
        "v": 1,
        # Discord IDs as strings: they exceed 2**53, and the TV page's
        # JSON.parse would silently round them to someone else's ID.
        "guild": {"id": str(guild_id), "name": guild_name},
        # Where scores are posted, for the TV's "post with /new in #…" line.
        "channel": channel,
        "tournament": (
            {
                "name": tournament.name,
                "open": tournament.is_open,
                "started_at": tournament.started_at,
                "ends_at": tournament.ends_at,
                "ended_at": tournament.ended_at,
            }
            if tournament
            else None
        ),
        "tables": [
            {
                "id": table.id,
                "name": table.name,
                # The same accent /hs gives this table, so the TV and Discord
                # agree on which colour is which machine.
                "color": f"#{table_color(table).value:06x}",
                "top": [
                    {
                        "score": s.score,
                        "player": s.user_display,
                        "user_id": str(s.user_id),
                        "avatar_key": avatar_key(latest[s.user_id]),
                        "at": s.created_at,
                    }
                    for s in top
                ],
            }
            for table, top in tables
        ],
    }


def build_snapshot(
    store: Store, guild_id: int, guild_name: str, channel: str | None = None
) -> dict[str, Any]:
    """Everything the TV shows for one guild.

    Same tournament choice and same ordering as ``/hs``: the running
    tournament, else the one that ended last, and the top submissions (not the
    top players) with ties going to whoever posted first.
    """
    return _snapshot(guild_id, guild_name, *_top_submissions(store, guild_id), channel)


# ----------------------------------------------------------------- publisher


class ScoreboardPublisher:
    """Rebuilds snapshots and publishes the ones that changed.

    Knows nothing about Discord beyond a list of (guild id, name) pairs, an
    avatar fetcher and a channel-name lookup, so tests can drive it with fakes.
    """

    def __init__(
        self,
        store: Store,
        client: Any,
        topic: str,
        fetch_avatar: FetchAvatar,
        channel_name: ChannelName = lambda _guild_id: None,
    ) -> None:
        self.store = store
        self.client = client
        self.topic = topic.rstrip("/")
        self.fetch_avatar = fetch_avatar
        self.channel_name = channel_name
        # guild id -> the payload last published for it.
        self._snapshots: dict[int, str] = {}
        # (guild id, user id) -> (avatar key, image bytes) last published.
        self._avatars: dict[tuple[int, int], tuple[str, bytes]] = {}
        # (guild id, user id, avatar key) -> monotonic time to try again.
        self._avatar_retry: dict[tuple[int, int, str], float] = {}

    def snapshot_topic(self, guild_id: int) -> str:
        return f"{self.topic}/{guild_id}"

    def avatar_topic(self, guild_id: int, user_id: int) -> str:
        return f"{self.topic}/{guild_id}/avatar/{user_id}"

    async def run_once(self, guilds: Iterable[tuple[int, str]]) -> None:
        for guild_id, name in guilds:
            try:
                await self._publish_guild(guild_id, name)
            except Exception:  # noqa: BLE001 - one guild must not stall the rest
                log.exception("scoreboard publish failed for guild %s", guild_id)

    async def _publish_guild(self, guild_id: int, name: str) -> None:
        tournament, tables = _top_submissions(self.store, guild_id)
        snapshot = _snapshot(
            guild_id, name, tournament, tables, self.channel_name(guild_id)
        )
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        # Avatars first, so a listener never holds a snapshot naming a picture
        # it hasn't been sent yet. Checked on every tick, not just on a change,
        # so a failed fetch is retried even while the board stays quiet.
        await self._publish_avatars(guild_id, _latest_per_player(tables).values())
        if self._snapshots.get(guild_id) == payload:
            return
        self._publish(self.snapshot_topic(guild_id), payload)
        self._snapshots[guild_id] = payload

    async def _publish_avatars(
        self, guild_id: int, shown: Iterable[Submission]
    ) -> None:
        for s in shown:
            key = avatar_key(s)
            if key is None or s.user_avatar is None:
                continue
            published = self._avatars.get((guild_id, s.user_id))
            if published and published[0] == key:
                continue
            retry_at = self._avatar_retry.get((guild_id, s.user_id, key))
            if retry_at and retry_at > time.monotonic():
                continue
            try:
                image = await self.fetch_avatar(avatar_fetch_url(s.user_avatar))
            except Exception as exc:  # noqa: BLE001 - initials are fine
                log.warning("could not fetch avatar for user %s: %s", s.user_id, exc)
                self._avatar_retry[(guild_id, s.user_id, key)] = (
                    time.monotonic() + AVATAR_RETRY_SECONDS
                )
                continue
            self._publish(self.avatar_topic(guild_id, s.user_id), image)
            self._avatars[(guild_id, s.user_id)] = (key, image)

    def republish(self) -> None:
        """Send everything again, e.g. after reconnecting to the broker.

        Retained messages normally survive on the broker, but a broker restart
        or a message dropped while disconnected would otherwise leave the TV
        stale until the next score.
        """
        for (guild_id, user_id), (_, image) in list(self._avatars.items()):
            self._publish(self.avatar_topic(guild_id, user_id), image)
        for guild_id, payload in list(self._snapshots.items()):
            self._publish(self.snapshot_topic(guild_id), payload)

    def _publish(self, topic: str, payload: str | bytes) -> None:
        self.client.publish(topic, payload, qos=1, retain=True)


# ---------------------------------------------------------------------- mqtt


def build_client(config: Config) -> Any:
    """A TLS client that works on both paho-mqtt 1.x and 2.x.

    The client ID carries the hostname: the broker drops the older of two
    connections sharing an ID, so the Pi and a dev machine running the bot
    would otherwise knock each other off in a loop.
    """
    import paho.mqtt.client as mqtt

    client_id = f"pinbot-scoreboard-{socket.gethostname()}"
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        client = mqtt.Client(client_id=client_id)
    client.username_pw_set(config.mqtt_username, config.mqtt_password)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


# ----------------------------------------------------------------------- cog


class ScoreboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot, publisher: ScoreboardPublisher) -> None:
        self.bot = bot
        self.publisher = publisher
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        client = self.publisher.client
        client.disconnect()
        client.loop_stop()

    @tasks.loop(seconds=1)
    async def tick(self) -> None:
        await self.publisher.run_once((g.id, g.name) for g in self.bot.guilds)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()


async def setup_scoreboard(
    bot: commands.Bot, store: Store, config: Config
) -> ScoreboardCog | None:
    if not config.mqtt_broker:
        log.info("scoreboard off: MQTT_BROKER is not set")
        return None
    missing = [
        name
        for name, value in (
            ("MQTT_USERNAME", config.mqtt_username),
            ("MQTT_PASSWORD", config.mqtt_password),
        )
        if not value
    ]
    if missing:
        log.warning("scoreboard off: %s not set", ", ".join(missing))
        return None

    client = build_client(config)

    async def fetch_avatar(url: str) -> bytes:
        return await bot.http.get_from_cdn(url)

    def channel_name(guild_id: int) -> str | None:
        # From the cache only: this runs every second, and a channel the bot
        # can't see just drops the "in #…" from the TV's hint.
        channel_id = store.get_channel_id(guild_id)
        channel = bot.get_channel(channel_id) if channel_id else None
        return getattr(channel, "name", None)

    publisher = ScoreboardPublisher(
        store, client, config.scoreboard_topic, fetch_avatar, channel_name
    )

    # paho calls these from its own network thread. publish() is thread-safe,
    # and republish() only reads what the event loop has finished writing.
    def on_connect(_client, _userdata, _flags, rc, *_args) -> None:
        if rc == 0:
            log.info(
                "scoreboard connected to %s:%s, publishing to %s/<guild>",
                config.mqtt_broker,
                config.mqtt_port,
                publisher.topic,
            )
            publisher.republish()
        else:
            log.warning("scoreboard connection refused (%s)", rc)

    def on_disconnect(_client, _userdata, *args) -> None:
        # paho 2.x passes (flags, reason, properties); 1.x passes (rc,).
        reason = args[1] if len(args) >= 2 else (args[0] if args else "?")
        log.warning("scoreboard disconnected (%s) — paho will reconnect", reason)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect_async(config.mqtt_broker, config.mqtt_port, keepalive=60)
    client.loop_start()

    cog = ScoreboardCog(bot, publisher)
    await bot.add_cog(cog)
    return cog
