"""Ledger behaviour: the crown, voiding, tournament windows, guild isolation."""

from __future__ import annotations

import pytest

from bot.store import Store, StoreError, Submission, TableExists, TournamentRunning

GUILD = 1000
OTHER_GUILD = 2000
ADMIN = 7
ALICE, BOB, CARL = 11, 12, 13


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class Fixture:
    """A guild with one tournament and one table, plus a submit() helper."""

    def __init__(self, store: Store, guild_id: int, table_name: str = "Godzilla") -> None:
        self.store = store
        self.guild_id = guild_id
        self.tournament = store.start_tournament(
            guild_id, name="Test", started_by=ADMIN, ends_at=None
        )
        self.table = store.add_table(guild_id, table_name)
        self._clock = 1_700_000_000

    def submit(self, user_id: int, score: int, *, at: int | None = None):
        if at is None:
            self._clock += 60
            at = self._clock
        return self.store.add_submission(
            guild_id=self.guild_id,
            tournament_id=self.tournament.id,
            table_id=self.table.id,
            user_id=user_id,
            user_display=f"user{user_id}",
            score=score,
            at=at,
        )

    def king(self):
        return self.store.current_king(self.guild_id, self.tournament.id, self.table.id)


@pytest.fixture()
def fx(store):
    return Fixture(store, GUILD)


# --------------------------------------------------------------- the crown

def test_first_score_takes_the_crown(fx):
    submission, previous, king = fx.submit(ALICE, 1_000_000)
    assert previous is None
    assert king is not None and king.id == submission.id


def test_lower_score_does_not_change_the_crown(fx):
    fx.submit(ALICE, 5_000_000)
    submission, previous, king = fx.submit(BOB, 1_000_000)
    assert previous is not None and previous.user_id == ALICE
    assert king is not None and king.user_id == ALICE
    assert king.id != submission.id


def test_higher_score_takes_the_crown(fx):
    fx.submit(ALICE, 1_000_000)
    submission, previous, king = fx.submit(BOB, 5_000_000)
    assert previous is not None and previous.user_id == ALICE
    assert king is not None and king.id == submission.id


def test_ties_go_to_whoever_got_there_first(fx):
    first, _, _ = fx.submit(ALICE, 4_000_000, at=1_000)
    fx.submit(BOB, 4_000_000, at=2_000)
    king = fx.king()
    assert king is not None and king.id == first.id and king.user_id == ALICE


# ------------------------------------------------------------ void / restore

def test_voiding_the_leader_reverts_to_the_runner_up(fx):
    fx.submit(ALICE, 1_000_000)
    leader, _, _ = fx.submit(BOB, 9_000_000)
    fx.store.void_submission(GUILD, leader.id, voided_by=ADMIN, reason="foul play")
    king = fx.king()
    assert king is not None and king.user_id == ALICE and king.score == 1_000_000


def test_voiding_a_non_leader_leaves_the_crown_alone(fx):
    leader, _, _ = fx.submit(ALICE, 9_000_000)
    also_ran, _, _ = fx.submit(BOB, 1_000_000)
    fx.store.void_submission(GUILD, also_ran.id, voided_by=ADMIN, reason=None)
    king = fx.king()
    assert king is not None and king.id == leader.id


def test_voiding_everything_leaves_the_table_open(fx):
    a, _, _ = fx.submit(ALICE, 1_000_000)
    b, _, _ = fx.submit(BOB, 2_000_000)
    for sub in (a, b):
        fx.store.void_submission(GUILD, sub.id, voided_by=ADMIN, reason=None)
    assert fx.king() is None


def test_successive_voids_walk_back_down_the_ledger(fx):
    a, _, _ = fx.submit(ALICE, 1_000_000)
    b, _, _ = fx.submit(BOB, 2_000_000)
    c, _, _ = fx.submit(CARL, 3_000_000)
    fx.store.void_submission(GUILD, c.id, voided_by=ADMIN, reason=None)
    assert (k := fx.king()) is not None and k.id == b.id
    fx.store.void_submission(GUILD, b.id, voided_by=ADMIN, reason=None)
    assert (k := fx.king()) is not None and k.id == a.id


def test_restore_puts_the_crown_back(fx):
    fx.submit(ALICE, 1_000_000)
    leader, _, _ = fx.submit(BOB, 9_000_000)
    fx.store.void_submission(GUILD, leader.id, voided_by=ADMIN, reason="mistake")
    fx.store.restore_submission(GUILD, leader.id)
    king = fx.king()
    assert king is not None and king.id == leader.id
    restored = fx.store.get_submission(GUILD, leader.id)
    assert restored is not None and not restored.is_voided
    assert restored.void_reason is None


# ------------------------------------------------------------- autocomplete

def test_drop_candidates_are_live_scores_highest_first(fx):
    fx.submit(ALICE, 1_000_000)
    mid, _, _ = fx.submit(BOB, 5_000_000)
    top, _, _ = fx.submit(CARL, 9_000_000)
    voided, _, _ = fx.submit(ALICE, 7_000_000)
    fx.store.void_submission(GUILD, voided.id, voided_by=ADMIN, reason=None)

    rows = fx.store.drop_candidates(GUILD, fx.tournament.id)
    ids = [sub.id for sub, _ in rows]
    assert voided.id not in ids, "voided scores must not be offered for voiding"
    assert ids[0] == top.id, "the standing king must be first"
    assert ids[1] == mid.id
    assert all(name == "Godzilla" for _, name in rows)


def test_restore_candidates_are_voided_newest_first(fx):
    live, _, _ = fx.submit(ALICE, 1_000_000)
    first, _, _ = fx.submit(BOB, 2_000_000)
    second, _, _ = fx.submit(CARL, 3_000_000)
    fx.store.void_submission(GUILD, first.id, voided_by=ADMIN, reason=None, at=5_000)
    fx.store.void_submission(GUILD, second.id, voided_by=ADMIN, reason=None, at=9_000)

    rows = fx.store.restore_candidates(GUILD, fx.tournament.id)
    ids = [sub.id for sub, _ in rows]
    assert live.id not in ids, "live scores must not be offered for restoring"
    assert ids == [second.id, first.id], "most recently voided must be first"


def test_candidate_filtering_matches_id_score_and_name(fx):
    sub, _, _ = fx.submit(ALICE, 1_234_567)
    assert fx.store.drop_candidates(GUILD, fx.tournament.id, "godzil")
    assert fx.store.drop_candidates(GUILD, fx.tournament.id, f"#{sub.id}")
    assert fx.store.drop_candidates(GUILD, fx.tournament.id, "1234567")
    assert not fx.store.drop_candidates(GUILD, fx.tournament.id, "nonsense")


# -------------------------------------------------------------- tournaments

def test_only_one_tournament_runs_at_a_time(store):
    store.start_tournament(GUILD, name="First", started_by=ADMIN, ends_at=None)
    with pytest.raises(TournamentRunning):
        store.start_tournament(GUILD, name="Second", started_by=ADMIN, ends_at=None)


def test_ending_a_tournament_closes_the_window(fx):
    assert fx.store.active_tournament(GUILD) is not None
    fx.store.end_tournament(GUILD, fx.tournament.id, ended_by=ADMIN)
    assert fx.store.active_tournament(GUILD) is None
    latest = fx.store.latest_tournament(GUILD)
    assert latest is not None and not latest.is_open


def test_extend_reopens_an_ended_tournament(fx):
    fx.store.end_tournament(GUILD, fx.tournament.id, ended_by=ADMIN)
    fx.store.extend_tournament(GUILD, fx.tournament.id, 9_999_999_999)
    reopened = fx.store.active_tournament(GUILD)
    assert reopened is not None and reopened.id == fx.tournament.id
    assert reopened.ended_at is None and reopened.ended_by is None


def test_due_tournaments_only_returns_expired_open_ones(store):
    expired = store.start_tournament(GUILD, name="Over", started_by=ADMIN, ends_at=1_000)
    store.start_tournament(
        OTHER_GUILD, name="Ongoing", started_by=ADMIN, ends_at=9_999_999_999
    )
    open_ended = store.start_tournament(3000, name="No end", started_by=ADMIN, ends_at=None)

    due = store.due_tournaments(at=5_000)
    ids = [t.id for t in due]
    assert ids == [expired.id]
    assert open_ended.id not in ids, "a tournament with no scheduled end never auto-closes"


def test_scores_are_scoped_to_their_tournament(store):
    first = Fixture(store, GUILD)
    first.submit(ALICE, 5_000_000)
    store.end_tournament(GUILD, first.tournament.id, ended_by=ADMIN)

    second = store.start_tournament(GUILD, name="Round two", started_by=ADMIN, ends_at=None)
    assert store.current_king(GUILD, second.id, first.table.id) is None
    assert store.current_king(GUILD, first.tournament.id, first.table.id) is not None


# ------------------------------------------------------------------- tables

def test_duplicate_table_names_are_refused(fx):
    with pytest.raises(TableExists):
        fx.store.add_table(GUILD, "godzilla")  # case-insensitive clash


def test_retiring_a_table_keeps_its_scores_queryable(fx):
    fx.submit(ALICE, 1_000_000)
    fx.store.set_table_active(fx.table.id, False)
    assert fx.store.list_tables(GUILD) == []
    assert len(fx.store.list_tables(GUILD, include_inactive=True)) == 1
    assert fx.king() is not None, "a retired table's high score is still on record"


def test_re_adding_a_retired_table_reactivates_it(fx):
    fx.submit(ALICE, 1_000_000)
    fx.store.set_table_active(fx.table.id, False)
    again = fx.store.add_table(GUILD, "Godzilla")
    assert again.id == fx.table.id and again.active
    assert fx.king() is not None


def test_rename_rejects_a_clash_but_allows_recasing(fx):
    fx.store.add_table(GUILD, "Attack From Mars")
    with pytest.raises(TableExists):
        fx.store.rename_table(GUILD, fx.table.id, "attack from mars")
    renamed = fx.store.rename_table(GUILD, fx.table.id, "GODZILLA")
    assert renamed.name == "GODZILLA"


# -------------------------------------------------------------------- purges

def test_drophs_clears_only_the_current_tournament(store):
    first = Fixture(store, GUILD)
    first.submit(ALICE, 1_000_000)
    store.end_tournament(GUILD, first.tournament.id, ended_by=ADMIN)
    second = store.start_tournament(GUILD, name="Two", started_by=ADMIN, ends_at=None)
    store.add_submission(
        guild_id=GUILD,
        tournament_id=second.id,
        table_id=first.table.id,
        user_id=BOB,
        user_display="bob",
        score=2_000_000,
    )

    deleted = store.drop_all_scores(GUILD, second.id)
    assert deleted == 1
    assert store.submission_count(GUILD, second.id) == 0
    assert store.submission_count(GUILD, first.tournament.id) == 1, "history is untouched"


def test_droptables_cascades_to_submissions(fx):
    fx.submit(ALICE, 1_000_000)
    fx.submit(BOB, 2_000_000)
    tables, subs = fx.store.delete_all_tables(GUILD)
    assert (tables, subs) == (1, 2)
    assert fx.store.list_tables(GUILD, include_inactive=True) == []
    assert fx.store.submission_count(GUILD, fx.tournament.id) == 0


def test_proof_location_round_trips_without_storing_a_url(fx):
    submission, _, _ = fx.submit(ALICE, 1_000_000)
    fx.store.attach_proof(
        GUILD,
        submission.id,
        channel_id=555,
        message_id=666,
        jump_url="https://discord.com/channels/1/2/3",
        filename="proof.jpg",
    )
    stored = fx.store.get_submission(GUILD, submission.id)
    assert stored is not None
    assert (stored.proof_channel_id, stored.proof_message_id) == (555, 666)
    assert stored.proof_jump_url == "https://discord.com/channels/1/2/3"
    # There is deliberately nowhere to put a signed CDN URL — it would be dead
    # in 24 hours, and this event runs for days.
    assert "proof_url" not in Submission.__dataclass_fields__


def test_rolling_back_a_submission_restores_the_previous_crown(fx):
    keeper, _, _ = fx.submit(ALICE, 1_000_000)
    doomed, _, king = fx.submit(BOB, 9_000_000)
    assert king is not None and king.id == doomed.id
    fx.store.delete_submission(GUILD, doomed.id)
    assert (k := fx.king()) is not None and k.id == keeper.id


# ----------------------------------------------------------- guild isolation

def test_guilds_with_identical_table_names_never_mix(store):
    ours = Fixture(store, GUILD, "Godzilla")
    theirs = Fixture(store, OTHER_GUILD, "Godzilla")
    assert ours.table.id != theirs.table.id

    ours.submit(ALICE, 1_000_000)
    theirs.submit(BOB, 9_000_000)

    our_king = ours.king()
    their_king = theirs.king()
    assert our_king is not None and our_king.user_id == ALICE
    assert their_king is not None and their_king.user_id == BOB

    assert store.submission_count(GUILD, ours.tournament.id) == 1
    assert store.submission_count(OTHER_GUILD, theirs.tournament.id) == 1


def test_settings_are_per_guild(store):
    store.set_channel_id(GUILD, 111)
    store.add_admin_role(GUILD, 222)
    store.set_vision_enabled(GUILD, True)

    assert store.get_channel_id(OTHER_GUILD) is None
    assert store.get_admin_role_ids(OTHER_GUILD) == []
    assert store.get_vision_enabled(OTHER_GUILD) is False


def test_one_guild_cannot_read_or_purge_another(store):
    ours = Fixture(store, GUILD)
    theirs = Fixture(store, OTHER_GUILD)
    mine, _, _ = ours.submit(ALICE, 1_000_000)
    theirs.submit(BOB, 2_000_000)

    assert store.get_submission(OTHER_GUILD, mine.id) is None
    assert store.get_table(OTHER_GUILD, ours.table.id) is None
    assert store.get_table_by_name(OTHER_GUILD, "Godzilla").id == theirs.table.id
    assert store.get_tournament(OTHER_GUILD, ours.tournament.id) is None

    # A purge in one guild must not touch the other.
    store.delete_all_tables(OTHER_GUILD)
    assert store.submission_count(GUILD, ours.tournament.id) == 1
    assert ours.king() is not None


def test_guild_totals_counts_every_kind_of_row(store):
    fx = Fixture(store, GUILD)
    fx.submit(ALICE, 1_000_000)
    fx.submit(BOB, 2_000_000)
    store.set_channel_id(GUILD, 4242)
    store.add_admin_role(GUILD, 777)
    store.log(GUILD, actor_id=ADMIN, action="table.add")

    totals = store.guild_totals(GUILD)
    assert (totals.tournaments, totals.tables, totals.submissions) == (1, 1, 2)
    assert (totals.settings, totals.audit) == (2, 1)
    assert totals.total == 7
    assert store.guild_totals(GUILD).total == 7, "counting must not consume anything"


def test_wipe_guild_removes_everything_and_reports_what_it_took(store):
    fx = Fixture(store, GUILD)
    fx.submit(ALICE, 1_000_000)
    store.set_channel_id(GUILD, 4242)
    store.log(GUILD, actor_id=ADMIN, action="table.add")

    wiped = store.wipe_guild(GUILD)
    assert (wiped.tournaments, wiped.tables, wiped.submissions) == (1, 1, 1)
    assert (wiped.settings, wiped.audit) == (1, 1)
    assert store.guild_totals(GUILD).total == 0
    assert store.latest_tournament(GUILD) is None
    assert store.list_tables(GUILD, include_inactive=True) == []
    assert store.get_channel_id(GUILD) is None
    assert store.audit_count(GUILD) == 0


def test_wipe_guild_stops_at_the_guild_boundary(store):
    """The severe version of the missing-guild_id bug: a factory reset in one
    server taking another server's live tournament with it."""
    ours = Fixture(store, GUILD)
    theirs = Fixture(store, OTHER_GUILD)
    ours.submit(ALICE, 1_000_000)
    theirs.submit(BOB, 2_000_000)
    store.set_channel_id(OTHER_GUILD, 555)
    store.log(OTHER_GUILD, actor_id=ADMIN, action="table.add")

    store.wipe_guild(GUILD)

    assert store.guild_totals(OTHER_GUILD).total == 5
    assert theirs.king() is not None
    assert store.get_channel_id(OTHER_GUILD) == 555


def test_voiding_across_guilds_is_a_no_op(store):
    ours = Fixture(store, GUILD)
    Fixture(store, OTHER_GUILD)
    mine, _, _ = ours.submit(ALICE, 1_000_000)
    # Same submission id, wrong guild: the UPDATE must match nothing.
    ours.store.void_submission(OTHER_GUILD, mine.id, voided_by=ADMIN, reason="nope")
    still_there = store.get_submission(GUILD, mine.id)
    assert still_there is not None and not still_there.is_voided


# --------------------------------------------------------------- misc / views

def test_standings_and_history_shapes(fx):
    a, _, _ = fx.submit(ALICE, 1_000_000)
    b, _, _ = fx.submit(BOB, 3_000_000)
    c, _, _ = fx.submit(CARL, 2_000_000)
    fx.store.void_submission(GUILD, c.id, voided_by=ADMIN, reason="photo mismatch")

    standings = fx.store.standings(GUILD, fx.tournament.id, fx.table.id)
    assert [s.id for s in standings] == [b.id, a.id], "voided rows excluded, score order"

    history = fx.store.history(GUILD, fx.tournament.id, fx.table.id)
    assert [h.id for h in history] == [a.id, b.id, c.id], "chronological, voided included"
    assert fx.store.attempt_count(GUILD, fx.tournament.id, fx.table.id) == 3


def test_audit_log_records_actions(fx):
    fx.store.log(GUILD, actor_id=ADMIN, action="drop", target="submission:1", detail="why")
    rows = fx.store._conn.execute(
        "SELECT * FROM audit WHERE guild_id = ?", (GUILD,)
    ).fetchall()
    assert len(rows) == 1 and rows[0]["action"] == "drop"


def test_store_errors_are_all_user_facing(fx):
    assert issubclass(TableExists, StoreError)
    assert issubclass(TournamentRunning, StoreError)


def test_table_stats_counts_attempts_players_and_failed_challenges(fx):
    first, _, _ = fx.submit(ALICE, 9_000_000)
    fx.submit(BOB, 1_000_000)       # posted after the crown, and lost
    fx.submit(BOB, 2_000_000)       # same
    dead, _, _ = fx.submit(CARL, 3_000_000)
    fx.store.void_submission(GUILD, dead.id, voided_by=ADMIN, reason=None)

    stats = fx.store.table_stats(GUILD, fx.tournament.id, fx.table.id, king=fx.king())
    assert stats.attempts == 4, "every submission counts, voided included"
    assert stats.players == 3
    assert stats.challenges == 2, "voided attempts are not challenges the king survived"
    assert fx.king().id == first.id


def test_table_stats_on_an_untouched_table(fx):
    stats = fx.store.table_stats(GUILD, fx.tournament.id, fx.table.id, king=None)
    assert (stats.attempts, stats.players, stats.challenges) == (0, 0, 0)


def test_tournament_stats_ignores_voided_scores(fx):
    fx.submit(ALICE, 1_000_000)
    fx.submit(ALICE, 2_000_000)
    dead, _, _ = fx.submit(BOB, 3_000_000)
    fx.store.void_submission(GUILD, dead.id, voided_by=ADMIN, reason=None)

    submissions, players = fx.store.tournament_stats(GUILD, fx.tournament.id)
    assert (submissions, players) == (2, 1)


# --------------------------------------------------------------- migrations

def test_migration_adds_a_column_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS won't add a column to a database that already
    exists, so an upgrade has to ALTER it — without touching live scores."""
    import sqlite3

    from bot import store as store_module

    v1_schema = store_module._SCHEMA.replace("    user_avatar      TEXT,\n", "")
    assert "user_avatar" not in v1_schema, "the v1 shape must genuinely lack the column"

    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(v1_schema)
    conn.execute(
        "INSERT INTO tables (guild_id, name, active, sort_order, created_at) "
        "VALUES (?, 'Godzilla', 1, 1, 0)",
        (GUILD,),
    )
    conn.execute(
        "INSERT INTO tournaments (guild_id, name, started_at, started_by) "
        "VALUES (?, 'Old', 0, ?)",
        (GUILD, ADMIN),
    )
    conn.execute(
        "INSERT INTO submissions "
        "(guild_id, tournament_id, table_id, user_id, user_display, score, created_at) "
        "VALUES (?, 1, 1, ?, 'alice', 3127605730, 100)",
        (GUILD, ALICE),
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        columns = {
            row["name"] for row in store._conn.execute("PRAGMA table_info(submissions)")
        }
        assert "user_avatar" in columns

        version = store._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert version == str(store_module.SCHEMA_VERSION)

        # The pre-existing score is untouched, and reads back through the
        # dataclass that now has one more field than the row was written with.
        survivor = store.get_submission(GUILD, 1)
        assert survivor is not None
        assert survivor.score == 3_127_605_730
        assert survivor.user_display == "alice"
        assert survivor.user_avatar is None, "older rows simply have no avatar"
    finally:
        store.close()

    reopened = Store(path)  # the migration must be idempotent
    try:
        assert reopened.submission_count(GUILD, 1) == 1
    finally:
        reopened.close()
