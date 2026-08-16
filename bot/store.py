"""SQLite persistence.

Two invariants shape this module.

**Everything is keyed by guild_id.** The bot is built so anyone can add it to
their own server and configure it entirely through slash commands, which means
no query may ever reach across servers. A missing ``guild_id`` in a WHERE
clause would be silent and severe, so tests/test_store.py checks isolation
explicitly.

**Submissions are an append-only ledger.** Voiding a score sets ``voided_at``
instead of deleting the row, so "who holds the crown" is always the derived
query in :meth:`Store.current_king`. That single decision is what makes
"revert to the previous high score" free, and it handles voiding a score that
was never king, voiding several in a row, and voiding everything. It also
removes the read-modify-write race between two simultaneous submissions:
nothing stores who is king, so each submission inserts and re-derives.

Calls are synchronous. At tournament scale — a few writes a minute — each one
is sub-millisecond and blocking the event loop is not a concern; going async
here would add a dependency and a failure mode for no measurable gain.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 2 added submissions.user_avatar. 3 added the photo-review columns. Bump
# alongside a matching _MIGRATIONS entry.
SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS tables (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tables_guild_name
    ON tables (guild_id, name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS tournaments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT,
    started_at INTEGER NOT NULL,
    started_by INTEGER NOT NULL,
    ends_at    INTEGER,
    ended_at   INTEGER,
    ended_by   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tournaments_guild
    ON tournaments (guild_id, ended_at);

CREATE TABLE IF NOT EXISTS submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    tournament_id    INTEGER NOT NULL,
    table_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    user_display     TEXT    NOT NULL,
    user_avatar      TEXT,
    score            INTEGER NOT NULL,
    note             TEXT,
    created_at       INTEGER NOT NULL,
    proof_channel_id INTEGER,
    proof_message_id INTEGER,
    proof_jump_url   TEXT,
    proof_filename   TEXT,
    voided_at        INTEGER,
    voided_by        INTEGER,
    void_reason      TEXT,
    vision_score     INTEGER,
    vision_verdict   TEXT,
    flagged_at       INTEGER,
    reviewed_at      INTEGER,
    reviewed_by      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_submissions_standings
    ON submissions (guild_id, tournament_id, table_id, voided_at, score DESC);
CREATE INDEX IF NOT EXISTS idx_submissions_voided
    ON submissions (guild_id, tournament_id, voided_at);
-- Backs the reaction -> submission lookup: every reaction in the pinball
-- channel hits this, including the many on messages that aren't proof posts.
CREATE INDEX IF NOT EXISTS idx_submissions_proof
    ON submissions (guild_id, proof_message_id);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    at       INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    action   TEXT    NOT NULL,
    target   TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit (guild_id, at DESC);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add a
# column to a database that already exists, so every one of these has to be
# applied separately — a live tournament's scores are not disposable.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("submissions", "user_avatar", "TEXT"),
    ("submissions", "flagged_at", "INTEGER"),
    ("submissions", "reviewed_at", "INTEGER"),
    ("submissions", "reviewed_by", "INTEGER"),
)

# Setting keys, so typos surface here rather than as a silently missing value.
CHANNEL_KEY = "pinball_channel_id"
ADMIN_ROLES_KEY = "admin_role_ids"
# Who gets pinged about a flagged photo is deliberately *not* a setting: it is
# whoever /config admin-role names, since that is the same group who can act on
# it. A second knob for the same audience is a second thing to get wrong.
VISION_KEY = "vision_enabled"

# `meta` key, not a guild setting — see Store.dev_synced_guilds.
DEV_SYNC_KEY = "dev_synced_guilds"


class StoreError(Exception):
    """Base class for expected, user-facing storage failures."""


class TableExists(StoreError):
    """A table with that name already exists in this guild."""


class TournamentRunning(StoreError):
    """A tournament is already running in this guild."""


class InvalidName(StoreError):
    """A table or tournament name that Discord would refuse to render."""


# Discord caps embed field names at 256 characters, embed titles at 256, and
# autocomplete choice labels at 100 — and rejects an *empty* field name
# outright. A name that violates any of those doesn't fail at the point it is
# typed; it fails later, with a 400, on every /hs and on the very autocomplete
# you would use to remove it. 64 is far more than any real machine needs
# ("Teenage Mutant Ninja Turtles" is 28) and leaves room for the decoration the
# embeds add around it.
MAX_NAME_LENGTH = 64

_WHITESPACE = re.compile(r"\s+")


def clean_name(raw: str, *, what: str = "name") -> str:
    """Normalise and validate a user-supplied name, or raise InvalidName.

    Collapses internal whitespace too, so a newline in a name can't break the
    line-per-entry rendering used by /hs and the tournament announcements.
    """
    collapsed = _WHITESPACE.sub(" ", raw).strip()
    if not collapsed:
        raise InvalidName(
            f"That {what} is blank — give me something to call it."
        )
    if len(collapsed) > MAX_NAME_LENGTH:
        raise InvalidName(
            f"That {what} is {len(collapsed)} characters. "
            f"Keep it to {MAX_NAME_LENGTH} or fewer."
        )
    return collapsed


def now() -> int:
    return int(time.time())


@dataclass(frozen=True, slots=True)
class Table:
    id: int
    guild_id: int
    name: str
    active: bool
    sort_order: int
    created_at: int


@dataclass(frozen=True, slots=True)
class Tournament:
    id: int
    guild_id: int
    name: str | None
    started_at: int
    started_by: int
    ends_at: int | None
    ended_at: int | None
    ended_by: int | None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def label(self) -> str:
        return self.name or "the tournament"


@dataclass(frozen=True, slots=True)
class TableStats:
    attempts: int
    players: int
    challenges: int


@dataclass(frozen=True, slots=True)
class GuildTotals:
    """Everything this bot holds for one guild, counted or destroyed."""

    tournaments: int
    tables: int
    submissions: int
    settings: int
    audit: int

    @property
    def total(self) -> int:
        return (
            self.tournaments
            + self.tables
            + self.submissions
            + self.settings
            + self.audit
        )


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    guild_id: int
    at: int
    actor_id: int
    action: str
    target: str | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class Submission:
    id: int
    guild_id: int
    tournament_id: int
    table_id: int
    user_id: int
    user_display: str
    user_avatar: str | None
    score: int
    note: str | None
    created_at: int
    proof_channel_id: int | None
    proof_message_id: int | None
    proof_jump_url: str | None
    proof_filename: str | None
    voided_at: int | None
    voided_by: int | None
    void_reason: str | None
    vision_score: int | None
    vision_verdict: str | None
    flagged_at: int | None
    reviewed_at: int | None
    reviewed_by: int | None

    @property
    def is_voided(self) -> bool:
        return self.voided_at is not None

    @property
    def is_pending_review(self) -> bool:
        """Flagged by the photo check and not yet decided by a human.

        This is the gate on the whole reaction flow: without it, a stray ❌ on
        any proof post at all would void a score.
        """
        return self.flagged_at is not None and self.reviewed_at is None


def _table(row: sqlite3.Row) -> Table:
    return Table(
        id=row["id"],
        guild_id=row["guild_id"],
        name=row["name"],
        active=bool(row["active"]),
        sort_order=row["sort_order"],
        created_at=row["created_at"],
    )


def _tournament(row: sqlite3.Row) -> Tournament:
    return Tournament(
        id=row["id"],
        guild_id=row["guild_id"],
        name=row["name"],
        started_at=row["started_at"],
        started_by=row["started_by"],
        ends_at=row["ends_at"],
        ended_at=row["ended_at"],
        ended_by=row["ended_by"],
    )


def _submission(row: sqlite3.Row) -> Submission:
    return Submission(**{key: row[key] for key in Submission.__dataclass_fields__})


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Left at the default synchronous=FULL deliberately. Store calls run on
        # the event loop, so a slow commit would stall every other coroutine —
        # but measured at 0.004 ms per commit, roughly three of which back a
        # single submission. Trading durability for that is not a trade.
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _migrate(self) -> None:
        """Add columns missing from an existing database. Idempotent."""
        for table, column, declaration in _MIGRATIONS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                )

    def close(self) -> None:
        self._conn.close()

    # -------------------------------------------------------- internal state
    #
    # `meta` is the bot's own bookkeeping, deliberately separate from
    # `settings`: nothing here is a guild's configuration, so none of it should
    # show up in /config show, be counted by /config reset, or be cleared by it.

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def dev_synced_guilds(self) -> list[int]:
        """Guilds that currently hold a guild-scoped copy of the command tree.

        Remembered across restarts because leaving dev mode has to undo it, and
        by then DEV_GUILD_ID is gone from the environment — without this, the
        guild copies linger next to the global ones and every command appears
        twice.
        """
        raw = self.get_meta(DEV_SYNC_KEY)
        if not raw:
            return []
        try:
            return [int(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            return []

    def set_dev_synced_guilds(self, guild_ids: Iterable[int]) -> None:
        self.set_meta(DEV_SYNC_KEY, json.dumps(sorted(set(guild_ids))))

    # ---------------------------------------------------------------- settings

    def get_setting(self, guild_id: int, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, guild_id: int, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, key, value),
            )

    def delete_setting(self, guild_id: int, key: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM settings WHERE guild_id = ? AND key = ?", (guild_id, key)
            )

    def get_channel_id(self, guild_id: int) -> int | None:
        raw = self.get_setting(guild_id, CHANNEL_KEY)
        return int(raw) if raw else None

    def set_channel_id(self, guild_id: int, channel_id: int) -> None:
        self.set_setting(guild_id, CHANNEL_KEY, str(channel_id))

    def get_admin_role_ids(self, guild_id: int) -> list[int]:
        raw = self.get_setting(guild_id, ADMIN_ROLES_KEY)
        if not raw:
            return []
        try:
            return [int(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            return []

    def add_admin_role(self, guild_id: int, role_id: int) -> list[int]:
        ids = self.get_admin_role_ids(guild_id)
        if role_id not in ids:
            ids.append(role_id)
            self.set_setting(guild_id, ADMIN_ROLES_KEY, json.dumps(ids))
        return ids

    def remove_admin_role(self, guild_id: int, role_id: int) -> list[int]:
        ids = [x for x in self.get_admin_role_ids(guild_id) if x != role_id]
        self.set_setting(guild_id, ADMIN_ROLES_KEY, json.dumps(ids))
        return ids

    def get_vision_enabled(self, guild_id: int) -> bool:
        return self.get_setting(guild_id, VISION_KEY) == "1"

    def set_vision_enabled(self, guild_id: int, enabled: bool) -> None:
        self.set_setting(guild_id, VISION_KEY, "1" if enabled else "0")

    def setting_count(self, guild_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return row["n"]

    # ------------------------------------------------------------------ tables

    def add_table(self, guild_id: int, name: str) -> Table:
        name = clean_name(name, what="table name")
        timestamp = now()
        try:
            with self._conn:
                order_row = self._conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next FROM tables "
                    "WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                cursor = self._conn.execute(
                    "INSERT INTO tables (guild_id, name, active, sort_order, created_at) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (guild_id, name, order_row["next"], timestamp),
                )
        except sqlite3.IntegrityError:
            existing = self.get_table_by_name(guild_id, name)
            if existing and not existing.active:
                # Reactivate rather than refuse: the organizer's intent is clear
                # and its old submissions are still on the ledger.
                self.set_table_active(existing.id, True)
                reactivated = self.get_table(guild_id, existing.id)
                assert reactivated is not None
                return reactivated
            raise TableExists(f"There's already a table called **{name}**.") from None
        created = self.get_table(guild_id, cursor.lastrowid)
        assert created is not None
        return created

    def list_tables(self, guild_id: int, *, include_inactive: bool = False) -> list[Table]:
        sql = "SELECT * FROM tables WHERE guild_id = ?"
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY sort_order, id"
        return [_table(r) for r in self._conn.execute(sql, (guild_id,))]

    def get_table(self, guild_id: int, table_id: int) -> Table | None:
        row = self._conn.execute(
            "SELECT * FROM tables WHERE guild_id = ? AND id = ?", (guild_id, table_id)
        ).fetchone()
        return _table(row) if row else None

    def get_table_by_name(self, guild_id: int, name: str) -> Table | None:
        row = self._conn.execute(
            "SELECT * FROM tables WHERE guild_id = ? AND name = ? COLLATE NOCASE",
            (guild_id, name.strip()),
        ).fetchone()
        return _table(row) if row else None

    def rename_table(self, guild_id: int, table_id: int, new_name: str) -> Table:
        new_name = clean_name(new_name, what="table name")
        clash = self.get_table_by_name(guild_id, new_name)
        if clash and clash.id != table_id:
            raise TableExists(f"There's already a table called **{new_name}**.")
        with self._conn:
            self._conn.execute(
                "UPDATE tables SET name = ? WHERE guild_id = ? AND id = ?",
                (new_name, guild_id, table_id),
            )
        renamed = self.get_table(guild_id, table_id)
        assert renamed is not None
        return renamed

    def set_table_active(self, table_id: int, active: bool) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE tables SET active = ? WHERE id = ?", (1 if active else 0, table_id)
            )

    def delete_all_tables(self, guild_id: int) -> tuple[int, int]:
        """Delete every table and submission for a guild. Returns (tables, submissions)."""
        with self._conn:
            subs = self._conn.execute(
                "DELETE FROM submissions WHERE guild_id = ?", (guild_id,)
            ).rowcount
            tables = self._conn.execute(
                "DELETE FROM tables WHERE guild_id = ?", (guild_id,)
            ).rowcount
        return tables, subs

    # ------------------------------------------------------------- tournaments

    def start_tournament(
        self,
        guild_id: int,
        *,
        name: str | None,
        started_by: int,
        ends_at: int | None,
        at: int | None = None,
    ) -> Tournament:
        if name is not None:
            # Same failure as an unrenderable table name: it lands in embed
            # titles and in the confirmation modals for the reset commands.
            name = clean_name(name, what="tournament name")
        if self.active_tournament(guild_id) is not None:
            raise TournamentRunning("A tournament is already running here.")
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO tournaments (guild_id, name, started_at, started_by, ends_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, at or now(), started_by, ends_at),
            )
        started = self.get_tournament(guild_id, cursor.lastrowid)
        assert started is not None
        return started

    def get_tournament(self, guild_id: int, tournament_id: int) -> Tournament | None:
        row = self._conn.execute(
            "SELECT * FROM tournaments WHERE guild_id = ? AND id = ?",
            (guild_id, tournament_id),
        ).fetchone()
        return _tournament(row) if row else None

    def active_tournament(self, guild_id: int) -> Tournament | None:
        row = self._conn.execute(
            "SELECT * FROM tournaments WHERE guild_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        return _tournament(row) if row else None

    def latest_tournament(self, guild_id: int) -> Tournament | None:
        """The running tournament, or the most recently ended one."""
        row = self._conn.execute(
            "SELECT * FROM tournaments WHERE guild_id = ? "
            "ORDER BY (ended_at IS NULL) DESC, started_at DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        return _tournament(row) if row else None

    def end_tournament(
        self, guild_id: int, tournament_id: int, *, ended_by: int, at: int | None = None
    ) -> Tournament:
        with self._conn:
            self._conn.execute(
                "UPDATE tournaments SET ended_at = ?, ended_by = ? "
                "WHERE guild_id = ? AND id = ? AND ended_at IS NULL",
                (at or now(), ended_by, guild_id, tournament_id),
            )
        ended = self.get_tournament(guild_id, tournament_id)
        assert ended is not None
        return ended

    def extend_tournament(self, guild_id: int, tournament_id: int, new_ends_at: int) -> Tournament:
        """Push the scheduled end out, reopening the tournament if it had ended.

        This is the recovery path for ending early by accident.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE tournaments SET ends_at = ?, ended_at = NULL, ended_by = NULL "
                "WHERE guild_id = ? AND id = ?",
                (new_ends_at, guild_id, tournament_id),
            )
        extended = self.get_tournament(guild_id, tournament_id)
        assert extended is not None
        return extended

    def delete_tournament(self, guild_id: int, tournament_id: int) -> int:
        """Discard a tournament and its scores as though it never ran.

        For abandoning a test run or a false start. Distinct from ending one:
        ending is a result worth announcing and keeping, this is not. Tables,
        settings, and the audit trail are deliberately untouched — you don't
        want to re-configure the channel, and an audit log you can erase isn't
        one. Returns the number of submissions discarded.
        """
        with self._conn:
            discarded = self._conn.execute(
                "DELETE FROM submissions WHERE guild_id = ? AND tournament_id = ?",
                (guild_id, tournament_id),
            ).rowcount
            self._conn.execute(
                "DELETE FROM tournaments WHERE guild_id = ? AND id = ?",
                (guild_id, tournament_id),
            )
        return discarded

    def due_tournaments(self, at: int | None = None) -> list[Tournament]:
        """Open tournaments whose scheduled end has passed, across all guilds."""
        rows = self._conn.execute(
            "SELECT * FROM tournaments WHERE ended_at IS NULL "
            "AND ends_at IS NOT NULL AND ends_at <= ?",
            (at or now(),),
        )
        return [_tournament(r) for r in rows]

    # ------------------------------------------------------------- submissions

    def add_submission(
        self,
        *,
        guild_id: int,
        tournament_id: int,
        table_id: int,
        user_id: int,
        user_display: str,
        score: int,
        user_avatar: str | None = None,
        note: str | None = None,
        proof_channel_id: int | None = None,
        proof_message_id: int | None = None,
        proof_jump_url: str | None = None,
        proof_filename: str | None = None,
        at: int | None = None,
    ) -> tuple[Submission, Submission | None, Submission | None]:
        """Record a submission and re-derive the crown in the same transaction.

        Returns (submission, previous_king, new_king). The submission took the
        crown when ``new_king.id == submission.id``.
        """
        with self._conn:
            previous = self._current_king(guild_id, tournament_id, table_id)
            cursor = self._conn.execute(
                "INSERT INTO submissions ("
                " guild_id, tournament_id, table_id, user_id, user_display,"
                " user_avatar, score, note,"
                " created_at, proof_channel_id, proof_message_id, proof_jump_url,"
                " proof_filename"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guild_id,
                    tournament_id,
                    table_id,
                    user_id,
                    user_display,
                    user_avatar,
                    score,
                    note,
                    at or now(),
                    proof_channel_id,
                    proof_message_id,
                    proof_jump_url,
                    proof_filename,
                ),
            )
            submission = self._get_submission(guild_id, cursor.lastrowid)
            assert submission is not None
            king = self._current_king(guild_id, tournament_id, table_id)
        return submission, previous, king

    def attach_proof(
        self,
        guild_id: int,
        submission_id: int,
        *,
        channel_id: int,
        message_id: int,
        jump_url: str,
        filename: str,
    ) -> None:
        """Record *where* the proof photo lives — never its URL.

        Attachment CDN URLs are signed and expire in about 24 hours, so the
        channel/message IDs (which don't) are what gets stored; a fresh URL is
        re-derived on demand. See bot/proofs.py.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE submissions SET proof_channel_id = ?, proof_message_id = ?, "
                "proof_jump_url = ?, proof_filename = ? WHERE guild_id = ? AND id = ?",
                (channel_id, message_id, jump_url, filename, guild_id, submission_id),
            )

    def delete_submission(self, guild_id: int, submission_id: int) -> None:
        """Remove a submission outright.

        Only used to roll back a row whose proof photo failed to post, so the
        bot never claims to have recorded a score it didn't.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM submissions WHERE guild_id = ? AND id = ?",
                (guild_id, submission_id),
            )

    def _current_king(
        self, guild_id: int, tournament_id: int, table_id: int
    ) -> Submission | None:
        row = self._conn.execute(
            "SELECT * FROM submissions "
            "WHERE guild_id = ? AND tournament_id = ? AND table_id = ? "
            "  AND voided_at IS NULL "
            "ORDER BY score DESC, created_at ASC, id ASC LIMIT 1",
            (guild_id, tournament_id, table_id),
        ).fetchone()
        return _submission(row) if row else None

    def current_king(
        self, guild_id: int, tournament_id: int, table_id: int
    ) -> Submission | None:
        """The standing high score for a table: highest live score, earliest wins ties."""
        return self._current_king(guild_id, tournament_id, table_id)

    def standings(
        self, guild_id: int, tournament_id: int, table_id: int, limit: int = 5
    ) -> list[Submission]:
        rows = self._conn.execute(
            "SELECT * FROM submissions "
            "WHERE guild_id = ? AND tournament_id = ? AND table_id = ? "
            "  AND voided_at IS NULL "
            "ORDER BY score DESC, created_at ASC, id ASC LIMIT ?",
            (guild_id, tournament_id, table_id, limit),
        )
        return [_submission(r) for r in rows]

    def attempt_count(self, guild_id: int, tournament_id: int, table_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM submissions "
            "WHERE guild_id = ? AND tournament_id = ? AND table_id = ?",
            (guild_id, tournament_id, table_id),
        ).fetchone()
        return row["n"]

    def table_stats(
        self,
        guild_id: int,
        tournament_id: int,
        table_id: int,
        *,
        king: Submission | None = None,
    ) -> TableStats:
        """Attempts, distinct players, and challenges survived since the crown.

        "Challenges" is the king-of-the-hill number worth showing: live scores
        posted on this table after the current king took it, all of which failed
        to beat it. Takes the king as an argument so the whole thing is one
        query rather than a second lookup.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS attempts,"
            "       COUNT(DISTINCT user_id) AS players,"
            "       SUM(CASE WHEN voided_at IS NULL AND created_at > ? AND id <> ?"
            "                THEN 1 ELSE 0 END) AS challenges "
            "FROM submissions "
            "WHERE guild_id = ? AND tournament_id = ? AND table_id = ?",
            (
                king.created_at if king else 1 << 62,  # nothing is "since" when open
                king.id if king else -1,
                guild_id,
                tournament_id,
                table_id,
            ),
        ).fetchone()
        return TableStats(
            attempts=row["attempts"] or 0,
            players=row["players"] or 0,
            challenges=row["challenges"] or 0,
        )

    def tournament_stats(self, guild_id: int, tournament_id: int) -> tuple[int, int]:
        """(live submissions, distinct players) for the /hs listing header."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS players "
            "FROM submissions "
            "WHERE guild_id = ? AND tournament_id = ? AND voided_at IS NULL",
            (guild_id, tournament_id),
        ).fetchone()
        return row["n"] or 0, row["players"] or 0

    def submission_count(self, guild_id: int, tournament_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM submissions "
            "WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id),
        ).fetchone()
        return row["n"]

    def history(
        self, guild_id: int, tournament_id: int, table_id: int, limit: int = 15
    ) -> list[Submission]:
        """Most recent ``limit`` submissions including voided, oldest-first."""
        rows = self._conn.execute(
            "SELECT * FROM ("
            "  SELECT * FROM submissions "
            "  WHERE guild_id = ? AND tournament_id = ? AND table_id = ? "
            "  ORDER BY created_at DESC, id DESC LIMIT ?"
            ") ORDER BY created_at ASC, id ASC",
            (guild_id, tournament_id, table_id, limit),
        )
        return [_submission(r) for r in rows]

    def get_submission(self, guild_id: int, submission_id: int) -> Submission | None:
        return self._get_submission(guild_id, submission_id)

    def _get_submission(self, guild_id: int, submission_id: int) -> Submission | None:
        row = self._conn.execute(
            "SELECT * FROM submissions WHERE guild_id = ? AND id = ?",
            (guild_id, submission_id),
        ).fetchone()
        return _submission(row) if row else None

    def void_submission(
        self,
        guild_id: int,
        submission_id: int,
        *,
        voided_by: int,
        reason: str | None,
        at: int | None = None,
    ) -> Submission | None:
        """Void a score. Returns None if that submission isn't in this guild.

        Submission IDs arrive from a user-supplied command option, so a wrong
        guild has to be a quiet no-op rather than an internal error.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE submissions SET voided_at = ?, voided_by = ?, void_reason = ? "
                "WHERE guild_id = ? AND id = ? AND voided_at IS NULL",
                (at or now(), voided_by, reason, guild_id, submission_id),
            )
        return self._get_submission(guild_id, submission_id)

    def restore_submission(self, guild_id: int, submission_id: int) -> Submission | None:
        """Un-void a score. Returns None if that submission isn't in this guild."""
        with self._conn:
            self._conn.execute(
                "UPDATE submissions "
                "SET voided_at = NULL, voided_by = NULL, void_reason = NULL "
                "WHERE guild_id = ? AND id = ?",
                (guild_id, submission_id),
            )
        return self._get_submission(guild_id, submission_id)

    def set_vision_result(
        self, guild_id: int, submission_id: int, *, score: int | None, verdict: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE submissions SET vision_score = ?, vision_verdict = ? "
                "WHERE guild_id = ? AND id = ?",
                (score, verdict, guild_id, submission_id),
            )

    # ---------------------------------------------------------- photo review

    def get_submission_by_proof_message(
        self, guild_id: int, message_id: int
    ) -> Submission | None:
        """Find the submission whose proof photo is that message.

        Guild-scoped like every other lookup here, so a reaction in one server
        can never reach another server's ledger.
        """
        row = self._conn.execute(
            "SELECT * FROM submissions WHERE guild_id = ? AND proof_message_id = ?",
            (guild_id, message_id),
        ).fetchone()
        return _submission(row) if row else None

    def flag_submission(
        self, guild_id: int, submission_id: int, *, at: int | None = None
    ) -> Submission | None:
        """Mark a submission as needing a human look. Returns None if not found."""
        with self._conn:
            self._conn.execute(
                "UPDATE submissions SET flagged_at = ? WHERE guild_id = ? AND id = ?",
                (at or now(), guild_id, submission_id),
            )
        return self._get_submission(guild_id, submission_id)

    def review_submission(
        self, guild_id: int, submission_id: int, *, by: int, at: int | None = None
    ) -> Submission | None:
        """Claim a pending flag for ``by``. Returns None if there was none.

        The claim is the whole point: the UPDATE only matches a flag that is
        still undecided, so of two admins reacting at the same instant exactly
        one gets a Submission back and the other gets None. Acting only on the
        non-None result is what stops a score being voided twice, or an
        approval and a drop both being announced.

        What was decided is deliberately not stored: an approved score is one
        that is reviewed and not voided, and a dropped one goes through the
        ordinary void path so the crown reverts and the usual announcement
        fires with no new code.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE submissions SET reviewed_at = ?, reviewed_by = ? "
                "WHERE guild_id = ? AND id = ? "
                "AND flagged_at IS NOT NULL AND reviewed_at IS NULL",
                (at or now(), by, guild_id, submission_id),
            )
            if cursor.rowcount == 0:
                return None
        return self._get_submission(guild_id, submission_id)

    def pending_flags(self, guild_id: int, tournament_id: int) -> list[Submission]:
        """Flagged, undecided submissions, oldest first — the review queue."""
        rows = self._conn.execute(
            "SELECT * FROM submissions "
            "WHERE guild_id = ? AND tournament_id = ? "
            "AND flagged_at IS NOT NULL AND reviewed_at IS NULL "
            "ORDER BY flagged_at ASC, id ASC",
            (guild_id, tournament_id),
        )
        return [_submission(r) for r in rows]

    def drop_all_scores(self, guild_id: int, tournament_id: int) -> int:
        with self._conn:
            return self._conn.execute(
                "DELETE FROM submissions WHERE guild_id = ? AND tournament_id = ?",
                (guild_id, tournament_id),
            ).rowcount

    # --------------------------------------------------------- autocompletion

    def drop_candidates(
        self, guild_id: int, tournament_id: int, query: str = "", limit: int = 25
    ) -> list[tuple[Submission, str]]:
        """Live submissions, highest score first — current kings at the top.

        The score you are asked to void is almost always the one standing on
        the board, so it should be the first thing offered.
        """
        rows = self._conn.execute(
            "SELECT s.*, t.name AS table_name FROM submissions s "
            "JOIN tables t ON t.id = s.table_id "
            "WHERE s.guild_id = ? AND s.tournament_id = ? AND s.voided_at IS NULL "
            "ORDER BY s.score DESC, s.created_at ASC",
            (guild_id, tournament_id),
        ).fetchall()
        return _filtered(rows, query, limit)

    def restore_candidates(
        self, guild_id: int, tournament_id: int, query: str = "", limit: int = 25
    ) -> list[tuple[Submission, str]]:
        """Voided submissions, most recently voided first.

        An unwanted drop is usually the one you just made.
        """
        rows = self._conn.execute(
            "SELECT s.*, t.name AS table_name FROM submissions s "
            "JOIN tables t ON t.id = s.table_id "
            "WHERE s.guild_id = ? AND s.tournament_id = ? AND s.voided_at IS NOT NULL "
            "ORDER BY s.voided_at DESC, s.id DESC",
            (guild_id, tournament_id),
        ).fetchall()
        return _filtered(rows, query, limit)

    # ------------------------------------------------------------------- audit

    def log(
        self,
        guild_id: int,
        *,
        actor_id: int,
        action: str,
        target: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO audit (guild_id, at, actor_id, action, target, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, now(), actor_id, action, target, detail),
            )

    def audit_entries(self, guild_id: int, limit: int = 15) -> list[AuditEntry]:
        """Most recent audit rows first."""
        rows = self._conn.execute(
            "SELECT * FROM audit WHERE guild_id = ? ORDER BY at DESC, id DESC LIMIT ?",
            (guild_id, limit),
        )
        return [
            AuditEntry(
                id=row["id"],
                guild_id=row["guild_id"],
                at=row["at"],
                actor_id=row["actor_id"],
                action=row["action"],
                target=row["target"],
                detail=row["detail"],
            )
            for row in rows
        ]

    def audit_count(self, guild_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM audit WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return row["n"]

    def clear_audit(self, guild_id: int) -> int:
        """Erase this guild's audit trail. Returns the number of rows removed."""
        with self._conn:
            return self._conn.execute(
                "DELETE FROM audit WHERE guild_id = ?", (guild_id,)
            ).rowcount

    def clear_settings(self, guild_id: int) -> int:
        """Return a guild to its just-added state. Returns settings removed.

        Tables, scores, and the audit trail are separate concerns with their own
        commands — this only clears configuration.
        """
        with self._conn:
            return self._conn.execute(
                "DELETE FROM settings WHERE guild_id = ?", (guild_id,)
            ).rowcount

    # -------------------------------------------------------- the master reset

    _GUILD_TABLES = ("tournaments", "tables", "submissions", "settings", "audit")

    def guild_totals(self, guild_id: int) -> GuildTotals:
        """Count everything held for a guild, without touching any of it."""
        counts = {
            name: self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {name} WHERE guild_id = ?", (guild_id,)
            ).fetchone()["n"]
            for name in self._GUILD_TABLES
        }
        return GuildTotals(**counts)

    def wipe_guild(self, guild_id: int) -> GuildTotals:
        """Delete every trace of a guild: the full factory reset.

        Deliberately one transaction rather than a sequence of the individual
        resets. A master reset that half-completed — scores gone, tables kept,
        or the admin-role list cleared while a tournament stayed running — would
        leave a state no single command can explain, which is exactly the thing
        someone reaching for a factory reset is trying to escape.

        Discord message history is untouched; the proof photos stay in the
        channel. Returns what was destroyed.
        """
        with self._conn:
            counts = {
                name: self._conn.execute(
                    f"DELETE FROM {name} WHERE guild_id = ?", (guild_id,)
                ).rowcount
                for name in self._GUILD_TABLES
            }
        return GuildTotals(**counts)

    def vacuum(self) -> None:
        """Return freed pages to the filesystem after a destructive purge."""
        self._conn.execute("VACUUM")


def _filtered(
    rows: list[sqlite3.Row], query: str, limit: int
) -> list[tuple[Submission, str]]:
    """Apply a plain substring filter over the rendered label, preserving order."""
    needle = query.strip().lower().lstrip("#")
    out: list[tuple[Submission, str]] = []
    for row in rows:
        submission = _submission(row)
        table_name = row["table_name"]
        haystack = f"{submission.id} {table_name} {submission.score} {submission.user_display}"
        if needle and needle not in haystack.lower():
            continue
        out.append((submission, table_name))
        if len(out) >= limit:
            break
    return out
