"""Fake Discord objects for driving real command callbacks.

These exist because both of the bugs found during the first live run were in the
seam between the ledger and Discord — a command that announced the wrong thing,
and an attribute that didn't exist on a response object. Neither is reachable
from a unit test of `store.py`, and neither needs a network connection to catch.

The fakes are deliberately shallow: they record what the bot tried to send and
hand back objects shaped like the ones discord.py would.
"""

from __future__ import annotations

import types
from pathlib import Path

import discord

import bot.admin as admin_module
import bot.perms as perms_module
from bot.admin import AdminCog, setup_admin
from bot.config import Config
from bot.scores import ScoresCog, setup_scores
from bot.store import Store, Submission, Tournament
from bot.__main__ import PinballBot

GUILD = 500
CHANNEL = 900
BOT_USER = 4242

ALICE, BOB, CARL = 1, 2, 3


class FakeAttachment:
    """Stands in for discord.Attachment on a /new invocation."""

    def __init__(
        self,
        data: bytes = b"\x89PNG\r\n\x1a\n-pretend-this-is-a-score-screen",
        content_type: str = "image/jpeg",
        filename: str = "IMG_2044.JPEG",
    ) -> None:
        self._data = data
        self.content_type = content_type
        self.filename = filename
        self.size = len(data)

    async def read(self) -> bytes:
        return self._data


class FakeMessage:
    _next_id = 7000

    def __init__(self, channel: FakeChannel, content, embed, embeds, file) -> None:
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = channel
        self.content = content
        self.embeds = [embed] if embed else list(embeds or [])
        self.attachments = []
        if file is not None:
            # Shaped like a real attachment URL: signed, and therefore expiring.
            # Nothing in the bot may persist this.
            url = (
                f"https://cdn.discordapp.com/attachments/{channel.id}/{self.id}/"
                f"{file.filename}?ex=deadbeef&is=cafe&hm=abc123"
            )
            embedded = next(
                (
                    e
                    for e in self.embeds
                    if e.image and (e.image.url or "").startswith("attachment://")
                ),
                None,
            )
            if embedded is not None:
                # Discord moves an attachment referenced as attachment:// into
                # the embed and drops it from the attachments list. Reproducing
                # that here is load-bearing: a fake that keeps the attachment
                # hides the fact that /hs can't find the photo.
                embedded.set_image(url=url)
                self.embedded_image_url = url
            else:
                self.attachments.append(
                    types.SimpleNamespace(url=url, filename=file.filename)
                )
        self.jump_url = f"https://discord.com/channels/{GUILD}/{channel.id}/{self.id}"


class FakeChannel(discord.abc.Messageable):
    """A channel that records sends and can be re-read like the real thing.

    Subclasses discord.abc.Messageable because the bot isinstance-checks for it
    before posting.
    """

    def __init__(self, channel_id: int = CHANNEL) -> None:
        self.id = channel_id
        self.name = "pinball"
        self.sent: list[FakeMessage] = []
        self.fetch_count = 0

    async def send(
        self, content=None, *, embed=None, embeds=None, file=None, allowed_mentions=None
    ) -> FakeMessage:
        message = FakeMessage(self, content, embed, embeds, file)
        self.sent.append(message)
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        self.fetch_count += 1
        for message in self.sent:
            if message.id == message_id:
                return message
        raise discord.NotFound(
            types.SimpleNamespace(status=404, reason="Not Found"), "unknown message"
        )

    async def _get_channel(self):
        return self

    @property
    def last(self) -> FakeMessage:
        assert self.sent, "nothing was posted"
        return self.sent[-1]


class BrokenChannel(FakeChannel):
    """A channel the bot may not post in — e.g. Attach Files was revoked."""

    async def send(self, *args, **kwargs):
        raise discord.Forbidden(
            types.SimpleNamespace(status=403, reason="Forbidden"), "missing permissions"
        )


class NoHistoryChannel(FakeChannel):
    """A channel where the bot lacks Read Message History.

    The real symptom this reproduces: /hs cannot re-read its own proof messages,
    so no fresh image URL is available and the standings lose every photo.
    """

    async def fetch_message(self, message_id: int):
        self.fetch_count += 1
        raise discord.Forbidden(
            types.SimpleNamespace(status=403, reason="Forbidden"),
            "missing read_message_history",
        )


class FakeResponse:
    def __init__(self, record: list) -> None:
        self._record = record
        self._done = False
        self.modal = None

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs) -> None:
        self._done = True

    async def send_message(self, content=None, **kwargs) -> None:
        self._done = True
        view = kwargs.get("view")
        if view is not None:
            # Auto-approve ConfirmView so the command under test proceeds. The
            # cancel path is covered separately by driving the button directly.
            view.value = True
            view.stop()
        self._record.append(("response", content, kwargs.get("embed"), kwargs.get("embeds")))

    async def send_modal(self, modal) -> None:
        self._done = True
        self.modal = modal
        self._record.append(("modal", modal.phrase, None, None))


class FakeInteraction:
    """Enough of discord.Interaction for the command callbacks to run."""

    def __init__(self, user_id: int = ALICE, name: str = "alice", *, channel=None) -> None:
        self.guild_id = GUILD
        self.user = types.SimpleNamespace(
            id=user_id,
            display_name=name,
            name=name,
            display_avatar=types.SimpleNamespace(
                url=f"https://cdn.discordapp.com/avatars/{user_id}/hash.png"
            ),
        )
        self.record: list = []
        self.response = FakeResponse(self.record)
        self.channel = channel
        self.followup = types.SimpleNamespace(send=self._followup)

    async def _followup(self, content=None, **kwargs) -> None:
        self.record.append(("followup", content, kwargs.get("embed"), kwargs.get("embeds")))

    async def edit_original_response(self, **kwargs) -> None:
        self.record.append(("edit", kwargs.get("content"), kwargs.get("embed"), None))

    # -- what the bot said, from the test's point of view -------------------

    @property
    def reply(self) -> str:
        """Text of the last thing sent back to the invoking user."""
        for kind, content, _embed, _embeds in reversed(self.record):
            if content is not None:
                return content
        raise AssertionError(f"no textual reply in {self.record}")

    @property
    def embed(self) -> discord.Embed:
        for _kind, _content, embed, _embeds in reversed(self.record):
            if embed is not None:
                return embed
        raise AssertionError(f"no embed in {self.record}")

    @property
    def embed_list(self) -> list[discord.Embed]:
        for _kind, _content, _embed, embeds in reversed(self.record):
            if embeds:
                return list(embeds)
        raise AssertionError(f"no embed list in {self.record}")


class Harness:
    """A configured guild with the cogs loaded and a fake channel wired up."""

    def __init__(
        self,
        store: Store,
        bot: PinballBot,
        scores: ScoresCog,
        admin: AdminCog,
        channel: FakeChannel,
    ) -> None:
        self.store = store
        self.bot = bot
        self.scores = scores
        self.admin = admin
        self.channel = channel

    @classmethod
    async def create(
        cls,
        tmp_path: Path,
        monkeypatch,
        *,
        tables: tuple[str, ...] = ("Godzilla", "Attack From Mars"),
        tournament: bool = True,
        ends_at: int | None = None,
        set_channel: bool = True,
        channel: FakeChannel | None = None,
    ) -> Harness:
        store = Store(tmp_path / "harness.db")
        bot = PinballBot(Config(), store)
        scores = await setup_scores(bot, store, bot.urls)
        admin = await setup_admin(bot, store, bot.urls)
        # Nothing here wants the 30s auto-close ticking underneath it; the tests
        # that care invoke it directly.
        admin.autoclose.cancel()

        # bot.user reads through to the connection state, which never gets
        # populated without a real login.
        bot._connection.user = types.SimpleNamespace(id=BOT_USER)

        chan = channel if channel is not None else FakeChannel()
        monkeypatch.setattr(
            bot, "get_channel", lambda cid: chan if cid == chan.id else None
        )
        # Admin *authorization* is unit-tested in test_perms.py; these tests are
        # about what the commands do once you're through the gate.
        async def allow(_store, _interaction):
            return True

        monkeypatch.setattr(admin_module, "require_admin", allow)

        # /config reset has a stricter gate — Manage Server only, because it
        # clears the admin-role list. Flip it with harness.set_manage_guild().
        gate = {"manage_guild": True}

        async def allow_manage(interaction):
            if gate["manage_guild"]:
                return True
            await interaction.response.send_message(
                perms_module.MANAGE_GUILD_ONLY, ephemeral=True
            )
            return False

        monkeypatch.setattr(admin_module, "require_manage_guild", allow_manage)

        if set_channel:
            store.set_channel_id(GUILD, chan.id)
        for name in tables:
            store.add_table(GUILD, name)
        if tournament:
            store.start_tournament(
                GUILD, name="Lanfest", started_by=99, ends_at=ends_at
            )
        harness = cls(store, bot, scores, admin, chan)
        harness._gate = gate
        return harness

    def set_manage_guild(self, allowed: bool) -> None:
        """Simulate a caller with, or without, the Manage Server permission."""
        self._gate["manage_guild"] = allowed

    def close(self) -> None:
        self.store.close()

    # -- invoking commands --------------------------------------------------

    async def run(self, command, interaction: FakeInteraction, **kwargs) -> FakeInteraction:
        """Call a command's real callback. Cog callbacks take `self` explicitly."""
        await command.callback(command.binding, interaction, **kwargs)
        return interaction

    async def submit(
        self,
        table: str,
        score: str,
        *,
        user_id: int = ALICE,
        name: str = "alice",
        proof: FakeAttachment | None = None,
        note: str | None = None,
    ) -> FakeInteraction:
        return await self.run(
            self.scores.new,
            FakeInteraction(user_id, name),
            table=table,
            score=score,
            proof=proof if proof is not None else FakeAttachment(),
            note=note,
        )

    async def hs(self, table: str | None = None, **kwargs) -> FakeInteraction:
        return await self.run(
            self.scores.hs, FakeInteraction(**kwargs), table=table
        )

    # -- inspecting state ---------------------------------------------------

    @property
    def tournament(self) -> Tournament:
        found = self.store.latest_tournament(GUILD)
        assert found is not None
        return found

    def table_id(self, name: str) -> int:
        table = self.store.get_table_by_name(GUILD, name)
        assert table is not None, f"no table {name!r}"
        return table.id

    def king(self, table: str) -> Submission | None:
        return self.store.current_king(GUILD, self.tournament.id, self.table_id(table))

    def audit_actions(self) -> list[str]:
        rows = self.store._conn.execute(
            "SELECT action FROM audit WHERE guild_id = ? ORDER BY id", (GUILD,)
        )
        return [row["action"] for row in rows]
