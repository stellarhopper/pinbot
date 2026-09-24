"""The live scoreboard: what the snapshot says, and when it gets published."""

from __future__ import annotations

import json

import pytest

from bot import scoreboard
from bot.config import Config
from bot.scoreboard import ScoreboardPublisher, avatar_fetch_url, build_snapshot
from bot.store import Store

GUILD = 1000
OTHER_GUILD = 2000
ADMIN = 7
ALICE, BOB, CARL, DANA = 11, 12, 13, 14

TOPIC = "pinbot/scoreboard"
_UNSET = object()


def avatar(user_id: int, version: str = "a") -> str:
    return f"https://cdn.discordapp.com/avatars/{user_id}/{version}{user_id}.png?size=1024"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class Board:
    """One guild with a table and a running tournament."""

    def __init__(self, store: Store, guild_id: int = GUILD) -> None:
        self.store = store
        self.guild_id = guild_id
        self.table = store.add_table(guild_id, "Godzilla")
        self.tournament = store.start_tournament(
            guild_id, name="Spring Open", started_by=ADMIN, ends_at=None
        )
        self._at = 1_000

    def submit(self, user_id: int, score: int, *, table=None, avatar_url=_UNSET):
        self._at += 1
        submission, _, _ = self.store.add_submission(
            guild_id=self.guild_id,
            tournament_id=self.tournament.id,
            table_id=(table or self.table).id,
            user_id=user_id,
            user_display=f"user{user_id}",
            user_avatar=avatar(user_id) if avatar_url is _UNSET else avatar_url,
            score=score,
            at=self._at,
        )
        return submission

    def snapshot(self) -> dict:
        return build_snapshot(self.store, self.guild_id, "LANfest")

    def top(self, table_index: int = 0) -> list[tuple[int, int]]:
        return [
            (int(e["user_id"]), e["score"])
            for e in self.snapshot()["tables"][table_index]["top"]
        ]


@pytest.fixture()
def board(store):
    return Board(store)


# ------------------------------------------------------------------ snapshot


def test_top_three_in_order_capped_at_three(board):
    board.submit(ALICE, 100)
    board.submit(BOB, 400)
    board.submit(CARL, 300)
    board.submit(DANA, 200)
    assert board.top() == [(BOB, 400), (CARL, 300), (DANA, 200)]


def test_one_player_can_hold_several_slots(board):
    # Same as /hs: the top submissions, not the top players.
    board.submit(ALICE, 300)
    board.submit(ALICE, 200)
    board.submit(BOB, 100)
    assert board.top() == [(ALICE, 300), (ALICE, 200), (BOB, 100)]


def test_ties_go_to_whoever_posted_first(board):
    board.submit(ALICE, 500)
    board.submit(BOB, 500)
    assert board.top() == [(ALICE, 500), (BOB, 500)]


def test_voided_scores_are_left_out(board):
    board.submit(ALICE, 100)
    best = board.submit(BOB, 900)
    board.store.void_submission(GUILD, best.id, voided_by=ADMIN, reason="blurry")
    assert board.top() == [(ALICE, 100)]


def test_empty_table_is_listed_with_no_scores(board):
    board.store.add_table(GUILD, "Attack From Mars")
    board.submit(ALICE, 100)
    tables = board.snapshot()["tables"]
    assert [t["name"] for t in tables] == ["Godzilla", "Attack From Mars"]
    assert tables[1]["top"] == []


def test_ended_tournament_shows_final_standings(board):
    board.submit(ALICE, 100)
    board.store.end_tournament(GUILD, board.tournament.id, ended_by=ADMIN)
    snap = board.snapshot()
    assert snap["tournament"]["open"] is False
    assert snap["tournament"]["name"] == "Spring Open"
    assert board.top() == [(ALICE, 100)]


def test_no_tournament_means_null_and_no_scores(store):
    store.add_table(GUILD, "Godzilla")
    snap = build_snapshot(store, GUILD, "LANfest")
    assert snap["tournament"] is None
    assert snap["tables"][0]["top"] == []


def test_guilds_never_see_each_other(store):
    ours, theirs = Board(store, GUILD), Board(store, OTHER_GUILD)
    ours.submit(ALICE, 100)
    theirs.submit(BOB, 999)
    assert ours.top() == [(ALICE, 100)]
    assert theirs.top() == [(BOB, 999)]


def test_discord_ids_are_strings(store):
    # A real snowflake is past 2**53; as a JSON number the page would round it
    # and ask for the wrong avatar.
    big = 197105676512788491
    b = Board(store, GUILD)
    b.submit(big, 100)
    snap = b.snapshot()
    assert snap["tables"][0]["top"][0]["user_id"] == str(big)
    assert snap["guild"]["id"] == str(GUILD)


def test_entry_carries_name_and_avatar_key(board):
    board.submit(ALICE, 100)
    entry = board.snapshot()["tables"][0]["top"][0]
    assert entry["player"] == f"user{ALICE}"
    assert entry["avatar_key"]


def test_avatar_key_follows_the_picture_not_the_size(board):
    first = board.submit(ALICE, 100)
    same = board.submit(ALICE, 50, avatar_url=avatar(ALICE).replace("1024", "64"))
    changed = board.submit(ALICE, 25, avatar_url=avatar(ALICE, version="b"))
    assert scoreboard.avatar_key(first) == scoreboard.avatar_key(same)
    assert scoreboard.avatar_key(first) != scoreboard.avatar_key(changed)


def test_avatar_fetch_url_asks_for_a_small_webp():
    assert (
        avatar_fetch_url("https://cdn.discordapp.com/avatars/1/abc.png?size=1024")
        == "https://cdn.discordapp.com/avatars/1/abc.webp?size=128"
    )
    # Default avatars don't come as WebP.
    assert (
        avatar_fetch_url("https://cdn.discordapp.com/embed/avatars/3.png")
        == "https://cdn.discordapp.com/embed/avatars/3.png?size=128"
    )


# ----------------------------------------------------------------- publisher


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object, int, bool]] = []
        self.fail = False

    def publish(self, topic, payload, qos=0, retain=False):
        if self.fail:
            raise OSError("broker gone")
        self.sent.append((topic, payload, qos, retain))

    def topics(self) -> list[str]:
        return [topic for topic, *_ in self.sent]

    def snapshots(self) -> list[dict]:
        return [
            json.loads(payload)
            for topic, payload, *_ in self.sent
            if "/avatar/" not in topic
        ]


class Fetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.fail:
            raise OSError("cdn down")
        return b"img:" + url.encode()


@pytest.fixture()
def pub(store):
    client, fetcher = FakeClient(), Fetcher()
    publisher = ScoreboardPublisher(store, client, TOPIC, fetcher)
    return publisher, client, fetcher


GUILDS = [(GUILD, "LANfest")]


async def test_first_run_publishes_retained(board, pub):
    publisher, client, _ = pub
    board.submit(ALICE, 100, avatar_url=None)
    await publisher.run_once(GUILDS)
    assert client.sent and all(qos == 1 and retain for _, _, qos, retain in client.sent)
    assert client.topics() == [f"{TOPIC}/{GUILD}"]


async def test_unchanged_board_publishes_nothing(board, pub):
    publisher, client, _ = pub
    board.submit(ALICE, 100)
    await publisher.run_once(GUILDS)
    before = len(client.sent)
    await publisher.run_once(GUILDS)
    await publisher.run_once(GUILDS)
    assert len(client.sent) == before


async def test_new_score_and_void_each_publish(board, pub):
    publisher, client, _ = pub
    board.submit(ALICE, 100)
    await publisher.run_once(GUILDS)
    best = board.submit(BOB, 500)
    await publisher.run_once(GUILDS)
    board.store.void_submission(GUILD, best.id, voided_by=ADMIN, reason="no")
    await publisher.run_once(GUILDS)
    tops = [[int(e["user_id"]) for e in s["tables"][0]["top"]] for s in client.snapshots()]
    assert tops == [[ALICE], [BOB, ALICE], [ALICE]]


async def test_publish_failure_does_not_stop_the_loop(board, pub):
    publisher, client, _ = pub
    board.submit(ALICE, 100)
    client.fail = True
    await publisher.run_once(GUILDS)  # logged, not raised
    client.fail = False
    await publisher.run_once(GUILDS)
    assert client.snapshots(), "the failed snapshot is retried on the next tick"


async def test_avatar_goes_out_once_and_before_the_snapshot(board, pub):
    publisher, client, fetcher = pub
    board.submit(ALICE, 100)
    await publisher.run_once(GUILDS)
    assert client.topics() == [f"{TOPIC}/{GUILD}/avatar/{ALICE}", f"{TOPIC}/{GUILD}"]
    assert fetcher.calls == [avatar_fetch_url(avatar(ALICE))]

    board.submit(ALICE, 200)
    board.submit(BOB, 50)
    await publisher.run_once(GUILDS)
    avatar_topics = [t for t in client.topics() if "/avatar/" in t]
    assert avatar_topics == [
        f"{TOPIC}/{GUILD}/avatar/{ALICE}",
        f"{TOPIC}/{GUILD}/avatar/{BOB}",
    ]


async def test_changed_avatar_is_sent_again(board, pub):
    publisher, client, _ = pub
    board.submit(ALICE, 100)
    await publisher.run_once(GUILDS)
    board.submit(ALICE, 200, avatar_url=avatar(ALICE, version="b"))
    await publisher.run_once(GUILDS)
    assert client.topics().count(f"{TOPIC}/{GUILD}/avatar/{ALICE}") == 2
    # Both of Alice's slots show the new picture, not whichever was posted with.
    keys = {e["avatar_key"] for e in client.snapshots()[-1]["tables"][0]["top"]}
    assert len(keys) == 1


async def test_old_avatar_is_not_sent_after_the_new_one(board, pub):
    publisher, client, _ = pub
    board.submit(ALICE, 100)
    board.submit(ALICE, 50, avatar_url=avatar(ALICE, version="b"))
    await publisher.run_once(GUILDS)
    (topic, image, *_), = [m for m in client.sent if "/avatar/" in m[0]]
    assert b"/b11." in image


async def test_failed_avatar_still_publishes_the_snapshot(board, pub):
    publisher, client, fetcher = pub
    fetcher.fail = True
    board.submit(ALICE, 100)
    await publisher.run_once(GUILDS)
    assert client.topics() == [f"{TOPIC}/{GUILD}"]

    # And it isn't hammered every second afterwards.
    board.submit(BOB, 50, avatar_url=None)
    await publisher.run_once(GUILDS)
    assert len(fetcher.calls) == 1


async def test_failed_avatar_is_retried_while_the_board_is_quiet(board, pub, monkeypatch):
    publisher, client, fetcher = pub
    fetcher.fail = True
    board.submit(ALICE, 100)
    await publisher.run_once(GUILDS)

    fetcher.fail = False
    clock = scoreboard.time.monotonic() + scoreboard.AVATAR_RETRY_SECONDS + 1
    monkeypatch.setattr(scoreboard.time, "monotonic", lambda: clock)
    await publisher.run_once(GUILDS)  # nothing about the board changed
    assert client.topics()[-1] == f"{TOPIC}/{GUILD}/avatar/{ALICE}"


async def test_republish_resends_everything(board, pub):
    publisher, client, _ = pub
    board.submit(ALICE, 100)
    await publisher.run_once(GUILDS)
    first = list(client.sent)
    client.sent.clear()
    publisher.republish()
    assert client.sent == first


# -------------------------------------------------------------------- config


def test_scoreboard_is_off_without_a_broker(monkeypatch):
    # The Pi runs the suite from the deployer, whose environment holds the real
    # broker credentials; this must not depend on that.
    for name in ("MQTT_BROKER", "MQTT_PORT", "SCOREBOARD_TOPIC"):
        monkeypatch.delenv(name, raising=False)
    config = Config()
    assert config.mqtt_broker is None
    assert config.mqtt_port == 8883
    assert config.scoreboard_topic == "pinbot/scoreboard"


async def test_setup_does_nothing_without_a_broker(monkeypatch, store):
    monkeypatch.delenv("MQTT_BROKER", raising=False)
    assert await scoreboard.setup_scoreboard(object(), store, Config()) is None
