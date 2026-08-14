"""The command-tree error handler.

Regression cover for a live failure: `/new` raised `NotFound: 10062 Unknown
interaction` at its first line, discord.py logged a traceback, and the player
was left with "The application did not respond" and no idea whether their score
had been recorded.
"""

from __future__ import annotations

import datetime
import logging
import types

import discord
import pytest
from discord import app_commands

from bot.store import Store
from bot.tree import (
    GENERIC_FAILURE,
    UNKNOWN_INTERACTION,
    PinballTree,
    dispatch_lag,
    is_expired_interaction,
    unwrap,
)


def http_error(cls, code: int, message: str = "boom"):
    """Build a real discord.py HTTP exception with a JSON error code."""
    response = types.SimpleNamespace(status=404, reason="Not Found")
    return cls(response, {"code": code, "message": message})


def invoke_error(original: BaseException) -> app_commands.CommandInvokeError:
    """Wrap an exception the way discord.py does before calling on_error."""
    command = types.SimpleNamespace(name="new", qualified_name="new")
    wrapper = app_commands.CommandInvokeError(command, original)  # type: ignore[arg-type]
    wrapper.__cause__ = original
    return wrapper


class FakeResponse:
    def __init__(self, done: bool) -> None:
        self._done = done
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, **kwargs) -> None:
        self.sent.append(content)


class FakeClient:
    """A bot with a store, and a view of which channels still exist."""

    def __init__(self, store=None, *, visible: set[int] | None = None) -> None:
        self.store = store
        self._visible = visible

    def get_channel(self, channel_id: int):
        if self._visible is None:
            return types.SimpleNamespace(id=channel_id)
        return types.SimpleNamespace(id=channel_id) if channel_id in self._visible else None


class FakeInteraction:
    def __init__(
        self,
        *,
        done: bool = False,
        age_seconds: float = 0.1,
        command: str = "new",
        guild_id: int | None = None,
        channel_id: int | None = None,
        parent_id: int | None = None,
        client=None,
        type=discord.InteractionType.application_command,
    ) -> None:
        self.command = types.SimpleNamespace(
            name=command.split()[-1], qualified_name=command
        )
        self.created_at = discord.utils.utcnow() - datetime.timedelta(seconds=age_seconds)
        self.response = FakeResponse(done)
        self.followups: list[str] = []
        self.followup = types.SimpleNamespace(send=self._followup)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.channel = types.SimpleNamespace(id=channel_id, parent_id=parent_id)
        self.client = client if client is not None else FakeClient()
        self.type = type

    async def _followup(self, content: str, **kwargs) -> None:
        self.followups.append(content)

    @property
    def delivered(self) -> list[str]:
        return self.response.sent + self.followups


@pytest.fixture()
def tree():
    client = discord.Client(intents=discord.Intents.default())
    return PinballTree(client)


# ------------------------------------------------------------ classification

def test_unwrap_finds_the_real_cause():
    original = http_error(discord.NotFound, UNKNOWN_INTERACTION)
    assert unwrap(invoke_error(original)) is original


def test_an_expired_interaction_is_recognised():
    assert is_expired_interaction(invoke_error(http_error(discord.NotFound, UNKNOWN_INTERACTION)))


@pytest.mark.parametrize(
    "error",
    [
        # 40060: already acknowledged. A different failure — two bot processes,
        # not a slow one — and it must not be silently swallowed.
        http_error(discord.HTTPException, 40060, "already acknowledged"),
        http_error(discord.NotFound, 10008, "unknown message"),
        ValueError("a genuine bug"),
    ],
)
def test_other_failures_are_not_treated_as_expiry(error):
    assert not is_expired_interaction(invoke_error(error))


def test_dispatch_lag_measures_interaction_age():
    assert dispatch_lag(FakeInteraction(age_seconds=2.5)) == pytest.approx(2.5, abs=0.2)


# ------------------------------------------------------------------ on_error

async def test_an_expired_interaction_logs_one_warning_without_a_traceback(tree, caplog):
    """The token is dead, so there is nothing to say to the player — but the
    operator needs to know it happened, and it isn't a bug worth a traceback."""
    interaction = FakeInteraction(age_seconds=3.4)
    with caplog.at_level(logging.WARNING, logger="bot.tree"):
        await tree.on_error(interaction, invoke_error(http_error(discord.NotFound, UNKNOWN_INTERACTION)))

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None, "a transient must not log a traceback"
    assert "expired" in record.message and "nothing was recorded" in record.message
    assert interaction.delivered == [], "a dead token cannot carry a message"


async def test_an_unexpected_failure_tells_the_player_and_logs_a_traceback(tree, caplog):
    interaction = FakeInteraction()
    with caplog.at_level(logging.ERROR, logger="bot.tree"):
        await tree.on_error(interaction, invoke_error(ValueError("kaboom")))

    assert interaction.delivered == [GENERIC_FAILURE]
    assert "/hs" in GENERIC_FAILURE, "the player is told how to check if it landed"
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None, "a real bug must log a traceback"


async def test_a_failure_after_defer_uses_a_followup(tree):
    """Half the commands defer first; send_message would 404 on those."""
    interaction = FakeInteraction(done=True)
    await tree.on_error(interaction, invoke_error(ValueError("kaboom")))
    assert interaction.followups == [GENERIC_FAILURE]
    assert interaction.response.sent == []


async def test_notify_never_raises_when_discord_also_fails(tree, caplog):
    """The notice is best-effort: failing to deliver it must not mask the error."""
    interaction = FakeInteraction()

    async def refuse(*args, **kwargs):
        raise http_error(discord.HTTPException, 50013, "missing permissions")

    interaction.response.send_message = refuse
    with caplog.at_level(logging.WARNING, logger="bot.tree"):
        await tree.on_error(interaction, invoke_error(ValueError("kaboom")))
    assert any("could not deliver" in r.message for r in caplog.records)


# --------------------------------------------------------- interaction_check

async def test_measuring_lag_never_blocks_a_command(tree):
    assert await tree.interaction_check(FakeInteraction(age_seconds=9.0)) is True


async def test_a_slow_dispatch_is_logged_with_the_measured_lag(tree, caplog):
    with caplog.at_level(logging.WARNING, logger="bot.tree"):
        await tree.interaction_check(FakeInteraction(age_seconds=2.6))
    assert len(caplog.records) == 1
    assert "slow dispatch" in caplog.records[0].message
    assert "2.6" in caplog.records[0].message, "the lag itself is the diagnostic"


async def test_a_prompt_dispatch_is_not_logged(tree, caplog):
    with caplog.at_level(logging.WARNING, logger="bot.tree"):
        await tree.interaction_check(FakeInteraction(age_seconds=0.05))
    assert caplog.records == []


# ------------------------------------------------------------ channel scope

GUILD = 500
PINBALL = 900
ELSEWHERE = 901


@pytest.fixture()
def configured(tmp_path):
    """A guild whose pinball channel is set, with both channels still visible."""
    store = Store(tmp_path / "tree.db")
    store.set_channel_id(GUILD, PINBALL)
    yield store
    store.close()


def somewhere(command: str, channel_id: int, store, **kwargs) -> FakeInteraction:
    return FakeInteraction(
        command=command,
        guild_id=GUILD,
        channel_id=channel_id,
        client=FakeClient(store, visible={PINBALL, ELSEWHERE}),
        **kwargs,
    )


async def test_commands_are_answered_in_the_pinball_channel(tree, configured):
    interaction = somewhere("new", PINBALL, configured)
    assert await tree.interaction_check(interaction) is True
    assert interaction.delivered == []


async def test_commands_elsewhere_are_redirected_not_run(tree, configured):
    """A /new in the wrong channel would post someone's proof photo where no
    one is looking, and split the event across two channels."""
    interaction = somewhere("new", ELSEWHERE, configured)
    assert await tree.interaction_check(interaction) is False
    assert f"<#{PINBALL}>" in interaction.delivered[0]


async def test_a_thread_of_the_pinball_channel_still_counts(tree, configured):
    interaction = somewhere("hs", 12345, configured, parent_id=PINBALL)
    assert await tree.interaction_check(interaction) is True


async def test_setting_the_channel_works_from_anywhere(tree, configured):
    """The one exception, and the reason the guard can't strand a server: the
    command that moves the tournament has to work from outside it."""
    interaction = somewhere("config pinball-channel", ELSEWHERE, configured)
    assert await tree.interaction_check(interaction) is True


async def test_nothing_is_restricted_before_a_channel_is_set(tree, tmp_path):
    store = Store(tmp_path / "bare.db")
    try:
        interaction = somewhere("table add", ELSEWHERE, store)
        assert await tree.interaction_check(interaction) is True
    finally:
        store.close()


async def test_an_unreachable_pinball_channel_fails_open(tree, configured, caplog):
    """If the configured channel was deleted, redirecting to it would strand
    every command in a channel that can't answer."""
    interaction = FakeInteraction(
        command="hs",
        guild_id=GUILD,
        channel_id=ELSEWHERE,
        client=FakeClient(configured, visible=set()),
    )
    with caplog.at_level(logging.WARNING, logger="bot.tree"):
        assert await tree.interaction_check(interaction) is True
    assert "unreachable" in caplog.records[0].message
    assert interaction.delivered == [], "and the user isn't sent anywhere"


async def test_autocomplete_is_never_answered_with_a_message(tree, configured):
    """An autocomplete can only be answered with choices; sending it a message
    would fail and leave the picker spinning."""
    interaction = somewhere(
        "drop", ELSEWHERE, configured, type=discord.InteractionType.autocomplete
    )
    assert await tree.interaction_check(interaction) is True
    assert interaction.delivered == []


async def test_a_dm_is_left_alone(tree, configured):
    interaction = FakeInteraction(
        command="hs", guild_id=None, channel_id=ELSEWHERE, client=FakeClient(configured)
    )
    assert await tree.interaction_check(interaction) is True
