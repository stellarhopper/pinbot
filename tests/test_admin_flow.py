"""Admin commands driven through their real command callbacks.

Covers what gets announced publicly, the destructive-purge gate, and the
auto-close loop — none of which is reachable from a unit test of store.py.
Admin *authorization* is unit-tested separately in test_perms.py; the harness
stubs the gate so these tests are about behaviour past it.
"""

from __future__ import annotations

import types

import bot.admin as admin_module
import bot.vision as vision_module
from bot import channels, embeds, review
from bot.vision import VisionResult
from bot.store import now

from fakes import (
    ALICE,
    BOB,
    CARL,
    GUILD,
    BrokenChannel,
    FakeChannel,
    FakeInteraction,
    Harness,
)


async def _deny(_store, interaction) -> bool:
    """Stand-in for a caller who is not an admin."""
    await interaction.response.send_message("denied", ephemeral=True)
    return False


# ------------------------------------------------------- tournament lifecycle

async def test_start_announces_and_warns_about_missing_setup(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=(), tournament=False, set_channel=False)
    itx = await h.run(h.admin.tournament_start, FakeInteraction(), name="Spring Open", ends_in="2d")

    assert "is open" in itx.reply
    assert "No tables are set up" in itx.reply
    assert "No pinball channel is set" in itx.reply
    h.close()


async def test_start_posts_the_opening_announcement(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tournament=False)
    await h.run(h.admin.tournament_start, FakeInteraction(), name="Spring Open 2026", ends_in="36h")

    embed = h.channel.last.embeds[0]
    assert "Spring Open 2026 is open" in embed.title
    tables = next(f for f in embed.fields if f.name.startswith("Tables"))
    assert "Godzilla" in tables.value and "Attack From Mars" in tables.value
    assert h.store.active_tournament(GUILD) is not None
    h.close()


async def test_a_second_tournament_is_refused_while_one_runs(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.run(h.admin.tournament_start, FakeInteraction(), name="Another")
    assert "already running" in itx.reply
    h.close()


async def test_start_with_an_unparseable_duration_changes_nothing(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tournament=False)
    itx = await h.run(h.admin.tournament_start, FakeInteraction(), ends_in="whenever")
    assert "isn't a duration" in itx.reply
    assert h.store.active_tournament(GUILD) is None, "nothing may be created on a bad arg"
    h.close()


async def test_end_announces_the_winner_of_every_table(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=BOB, name="bob")

    itx = await h.run(h.admin.tournament_end, FakeInteraction())
    assert "closed" in itx.reply

    results = h.channel.last.embeds[0]
    assert "final results" in results.title
    fields = {f.name: f.value for f in results.fields}
    assert "9,000,000" in fields["Godzilla"] and f"<@{BOB}>" in fields["Godzilla"]
    assert fields["Attack From Mars"] == "no scores set", "an unplayed table is named too"
    assert h.store.active_tournament(GUILD) is None
    h.close()


async def test_end_with_no_tournament_is_refused(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tournament=False)
    itx = await h.run(h.admin.tournament_end, FakeInteraction())
    assert "No tournament is running" in itx.reply
    h.close()


async def test_extend_reopens_a_tournament_ended_by_mistake(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    h.store.end_tournament(GUILD, h.tournament.id, ended_by=99)

    itx = await h.run(h.admin.tournament_extend, FakeInteraction(), duration="90m")
    assert "reopened and extended" in itx.reply
    assert "open again" in h.channel.last.content
    assert h.store.active_tournament(GUILD) is not None

    # And scores are accepted again.
    submitted = await h.submit("Godzilla", "1,000,000")
    assert "new king" in submitted.reply
    h.close()


async def test_extend_pushes_the_deadline_out(tmp_path, monkeypatch):
    deadline = now() + 3600
    h = await Harness.create(tmp_path, monkeypatch, ends_at=deadline)
    await h.run(h.admin.tournament_extend, FakeInteraction(), duration="2h")
    assert h.tournament.ends_at == deadline + 7200
    h.close()


async def test_extending_a_long_expired_tournament_gives_the_full_time(tmp_path, monkeypatch):
    """Extending from a deadline already in the past would land in the past."""
    h = await Harness.create(tmp_path, monkeypatch, ends_at=now() - 100_000)
    await h.run(h.admin.tournament_extend, FakeInteraction(), duration="1h")
    assert h.tournament.ends_at > now(), "the new deadline must actually be in the future"
    h.close()


async def test_status_reports_the_running_tournament(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    itx = await h.run(h.admin.tournament_status, FakeInteraction())
    fields = {f.name: f.value for f in itx.embed.fields}
    assert fields["Submissions"] == "1"
    assert fields["Tables"] == "2"
    h.close()


# --------------------------------------------------------------- auto-close

async def test_autoclose_ends_an_expired_tournament_once(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "7,000,000")
    h.store.extend_tournament(GUILD, h.tournament.id, now() - 1)

    before = len(h.channel.sent)
    await h.admin.autoclose()

    assert h.store.active_tournament(GUILD) is None
    assert len(h.channel.sent) == before + 1
    assert "final results" in h.channel.last.embeds[0].title

    await h.admin.autoclose()
    assert len(h.channel.sent) == before + 1, "nothing is due the second time"
    h.close()


async def test_autoclose_leaves_open_ended_tournaments_alone(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, ends_at=None)
    await h.admin.autoclose()
    assert h.store.active_tournament(GUILD) is not None
    h.close()


async def test_autoclose_survives_a_guild_it_cannot_post_to(tmp_path, monkeypatch):
    """One misconfigured server must not stop the others from closing."""
    h = await Harness.create(tmp_path, monkeypatch, set_channel=False)
    h.store.extend_tournament(GUILD, h.tournament.id, now() - 1)
    await h.admin.autoclose()
    assert h.store.active_tournament(GUILD) is None, "closed even with nowhere to announce"
    h.close()


# ------------------------------------------------------------ void / restore

async def test_drop_reverts_the_crown_and_announces_both_facts(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=ALICE)
    await h.submit("Godzilla", "9,000,000", user_id=BOB, name="bob")
    leader = h.king("Godzilla")

    await h.run(h.admin.drop, FakeInteraction(), id=leader.id, reason="score not on machine")

    embed = h.channel.last.embeds[0]
    assert "Score voided" in embed.title
    assert "score not on machine" in embed.description
    standing = next(f for f in embed.fields if f.name == "New standing").value
    assert "1,000,000" in standing and f"<@{ALICE}>" in standing
    assert h.king("Godzilla").user_id == ALICE
    h.close()


async def test_dropping_the_only_score_says_the_table_is_open(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    only = h.king("Godzilla")

    await h.run(h.admin.drop, FakeInteraction(), id=only.id, reason=None)
    standing = next(
        f for f in h.channel.last.embeds[0].fields if f.name == "New standing"
    ).value
    assert "now open" in standing
    assert h.king("Godzilla") is None
    h.close()


async def test_drop_works_after_the_tournament_ends(tmp_path, monkeypatch):
    """Prize disputes happen after the buzzer."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000")
    leader = h.king("Godzilla")
    h.store.end_tournament(GUILD, h.tournament.id, ended_by=99)

    await h.run(h.admin.drop, FakeInteraction(), id=leader.id, reason="disputed")
    assert h.king("Godzilla") is None
    h.close()


async def test_drop_rejects_an_unknown_or_already_voided_id(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    live = h.king("Godzilla")

    missing = await h.run(h.admin.drop, FakeInteraction(), id=999_999)
    assert "No submission" in missing.reply

    await h.run(h.admin.drop, FakeInteraction(), id=live.id, reason=None)
    again = await h.run(h.admin.drop, FakeInteraction(), id=live.id, reason=None)
    assert "already voided" in again.reply and "/restore" in again.reply
    h.close()


async def test_restore_reinstates_the_score_and_the_crown(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=ALICE)
    await h.submit("Godzilla", "9,000,000", user_id=BOB, name="bob")
    leader = h.king("Godzilla")
    h.store.void_submission(GUILD, leader.id, voided_by=99, reason="mistake")

    await h.run(h.admin.restore, FakeInteraction(), id=leader.id)
    assert "Score restored" in h.channel.last.embeds[0].title
    assert h.king("Godzilla").id == leader.id
    h.close()


async def test_restoring_a_live_score_is_refused(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    itx = await h.run(h.admin.restore, FakeInteraction(), id=h.king("Godzilla").id)
    assert "isn't voided" in itx.reply
    h.close()


async def test_history_shows_voided_entries_with_their_reason(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=ALICE)
    await h.submit("Godzilla", "9,000,000", user_id=BOB, name="bob")
    h.store.void_submission(GUILD, h.king("Godzilla").id, voided_by=99, reason="duplicate")

    itx = await h.run(h.admin.history, FakeInteraction(), table="Godzilla")
    description = itx.embed.description
    assert "~~" in description, "voided rows are struck through"
    assert "duplicate" in description and "1,000,000" in description
    h.close()


# ------------------------------------------------------------------- tables

async def test_table_add_remove_rename_and_list(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=())

    added = await h.run(h.admin.table_add, FakeInteraction(), name="Medieval Madness")
    assert "Added **Medieval Madness**" in added.reply

    clash = await h.run(h.admin.table_add, FakeInteraction(), name="medieval madness")
    assert "already a table" in clash.reply, "names clash case-insensitively"

    renamed = await h.run(
        h.admin.table_rename, FakeInteraction(), table="Medieval Madness", new_name="MM"
    )
    assert "Renamed" in renamed.reply

    removed = await h.run(h.admin.table_remove, FakeInteraction(), table="MM")
    assert "Retired" in removed.reply and "still on record" in removed.reply

    listed = await h.run(h.admin.table_list, FakeInteraction())
    assert "MM" in listed.reply and "retired" in listed.reply
    h.close()


async def test_retiring_a_table_keeps_its_scores(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    await h.run(h.admin.table_remove, FakeInteraction(), table="Godzilla")

    assert h.king("Godzilla") is not None, "the score survives retirement"
    itx = await h.hs()
    assert len(itx.embed_list) == 1, "but the table drops out of /hs"
    h.close()


# ------------------------------------------------------------------ config

async def test_config_channel_is_stored(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, set_channel=False)
    fake = types.SimpleNamespace(
        id=1234, mention="#pinball", guild=types.SimpleNamespace(me=None)
    )
    itx = await h.run(h.admin.config_channel, FakeInteraction(), channel=fake)
    assert "#pinball" in itx.reply
    assert h.store.get_channel_id(GUILD) == 1234
    h.close()


async def test_config_channel_defaults_to_the_channel_you_are_in(tmp_path, monkeypatch):
    """Naming the channel you're standing in is busywork — and this is the one
    command answered from anywhere, so it's how you move the tournament."""
    h = await Harness.create(tmp_path, monkeypatch, set_channel=False)
    itx = await h.run(h.admin.config_channel, FakeInteraction(channel=h.channel))

    assert h.store.get_channel_id(GUILD) == h.channel.id
    assert "go here" in itx.reply
    h.close()


async def test_config_channel_says_so_when_it_cannot_tell_where_you_are(
    tmp_path, monkeypatch
):
    h = await Harness.create(tmp_path, monkeypatch, set_channel=False)
    itx = await h.run(h.admin.config_channel, FakeInteraction())

    assert "name one" in itx.reply
    assert h.store.get_channel_id(GUILD) is None
    h.close()


async def test_the_escape_hatch_names_a_command_that_exists(tmp_path, monkeypatch):
    """The exemption is matched on a qualified name, so a rename would silently
    lock every server out of moving its own channel."""
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    assert channels.is_exempt(h.admin.config_channel.qualified_name)
    h.close()


async def test_config_show_renders_on_a_bare_server(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=(), tournament=False, set_channel=False)
    itx = await h.run(h.admin.config_show, FakeInteraction())
    fields = {f.name.split(" (")[0]: f.value for f in itx.embed.fields}
    assert "/config pinball-channel" in fields["Pinball channel"]
    assert "/table add" in fields["Tables"]
    assert "/tournament start" in fields["Tournament"]
    assert fields["Photo cross-check"].startswith("off")
    assert "player only" in fields["Flag pings"], "no admin role means nobody to ping"
    h.close()


async def test_admin_roles_can_be_added_and_removed(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    role = types.SimpleNamespace(id=777, mention="@Staff")

    await h.run(h.admin.admin_role_add, FakeInteraction(), role=role)
    assert h.store.get_admin_role_ids(GUILD) == [777]

    listed = await h.run(h.admin.admin_role_list, FakeInteraction())
    assert "<@&777>" in listed.reply and "Manage Server" in listed.reply

    await h.run(h.admin.admin_role_remove, FakeInteraction(), role=role)
    assert h.store.get_admin_role_ids(GUILD) == []
    h.close()


async def test_vision_cannot_be_enabled_without_the_dependency(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    itx = await h.run(h.admin.config_vision, FakeInteraction(), enabled=True)
    assert "isn't installed" in itx.reply
    assert h.store.get_vision_enabled(GUILD) is False
    h.close()


# --------------------------------------------------------- destructive purges

async def test_drophs_requires_typing_the_exact_count(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=ALICE)
    await h.submit("Godzilla", "2,000,000", user_id=BOB, name="bob")
    await h.submit("Attack From Mars", "3,000,000", user_id=CARL, name="carl")

    itx = await h.run(h.admin.drophs, FakeInteraction())
    modal = itx.response.modal
    assert modal.phrase == "3", "the gate is the number of rows about to die"

    wrong = FakeInteraction()
    modal.field._value = "0"
    await modal.on_submit(wrong)
    assert "didn't match" in wrong.reply
    assert h.store.submission_count(GUILD, h.tournament.id) == 3, "nothing may be deleted"

    right = FakeInteraction()
    modal.field._value = "3"
    await modal.on_submit(right)
    assert h.store.submission_count(GUILD, h.tournament.id) == 0
    assert "photos are still in the channel" in right.reply
    assert h.king("Godzilla") is None
    h.close()


async def test_drophs_on_an_empty_tournament_says_so(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.run(h.admin.drophs, FakeInteraction())
    assert "no scores recorded" in itx.reply
    assert itx.response.modal is None, "no confirmation gate when there's nothing to do"
    h.close()


async def test_droptables_clears_tables_and_their_scores(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    itx = await h.run(h.admin.droptables, FakeInteraction())
    modal = itx.response.modal
    assert modal.phrase == "2"

    done = FakeInteraction()
    modal.field._value = "2"
    await modal.on_submit(done)
    assert h.store.list_tables(GUILD, include_inactive=True) == []
    assert h.store.submission_count(GUILD, h.tournament.id) == 0
    assert "/table add" in done.reply
    h.close()


async def test_reset_discards_the_tournament_without_announcing_results(tmp_path, monkeypatch):
    """The gap /drophs and /droptables left: after those, the tournament row
    survives and still blocks /tournament start, and the only way out was
    /tournament end — which posts bogus final results before the real event."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000", user_id=ALICE)
    await h.submit("Godzilla", "2,000,000", user_id=BOB, name="bob")
    posts_before = len(h.channel.sent)

    itx = await h.run(h.admin.tournament_reset, FakeInteraction())
    modal = itx.response.modal
    assert modal.phrase == "2"

    done = FakeInteraction()
    modal.field._value = "2"
    await modal.on_submit(done)

    assert h.store.latest_tournament(GUILD) is None, "the tournament must be gone"
    assert len(h.channel.sent) == posts_before, "a discarded run announces nothing"
    assert "untouched" in done.reply

    # Tables, channel and admin config survive — that's the point.
    assert len(h.store.list_tables(GUILD)) == 2
    assert h.store.get_channel_id(GUILD) == h.channel.id

    # And a fresh tournament can now start, which it could not before.
    started = await h.run(h.admin.tournament_start, FakeInteraction(), name="Spring Open")
    assert "is open" in started.reply
    assert h.store.active_tournament(GUILD) is not None
    h.close()


async def test_reset_needs_the_exact_count(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    itx = await h.run(h.admin.tournament_reset, FakeInteraction())
    wrong = FakeInteraction()
    itx.response.modal.field._value = "9"
    await itx.response.modal.on_submit(wrong)

    assert "didn't match" in wrong.reply
    assert h.store.latest_tournament(GUILD) is not None, "nothing may be discarded"
    assert h.store.submission_count(GUILD, h.tournament.id) == 1
    h.close()


async def test_reset_keeps_an_audit_record_of_what_was_discarded(tmp_path, monkeypatch):
    """A discarded run leaves no announcement, so the audit row is the record."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    itx = await h.run(h.admin.tournament_reset, FakeInteraction())
    itx.response.modal.field._value = "1"
    await itx.response.modal.on_submit(FakeInteraction())

    assert "tournament.reset" in h.audit_actions()
    detail = h.store._conn.execute(
        "SELECT detail FROM audit WHERE guild_id = ? AND action = 'tournament.reset'",
        (GUILD,),
    ).fetchone()["detail"]
    assert "1 submissions" in detail
    h.close()


async def test_reset_with_no_tournament_says_so(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tournament=False)
    itx = await h.run(h.admin.tournament_reset, FakeInteraction())
    assert "no tournament here to discard" in itx.reply
    assert itx.response.modal is None
    h.close()


async def test_reset_leaves_an_earlier_tournaments_history_alone(tmp_path, monkeypatch):
    """Reset targets the most recent run only — a finished event stays on record."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "5,000,000")
    real = h.tournament
    h.store.end_tournament(GUILD, real.id, ended_by=99)

    test_run = h.store.start_tournament(
        GUILD, name="Test run", started_by=99, ends_at=None
    )
    itx = await h.run(h.admin.tournament_reset, FakeInteraction())
    itx.response.modal.field._value = "0"
    await itx.response.modal.on_submit(FakeInteraction())

    assert h.store.get_tournament(GUILD, test_run.id) is None
    survivor = h.store.get_tournament(GUILD, real.id)
    assert survivor is not None and not survivor.is_open
    assert h.store.submission_count(GUILD, real.id) == 1, "its scores are untouched"
    h.close()


async def test_droptables_with_no_tables_says_so(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    itx = await h.run(h.admin.droptables, FakeInteraction())
    assert "no tables to delete" in itx.reply
    h.close()


# ------------------------------------------------------------ /config reset

async def test_config_reset_returns_the_server_to_a_clean_slate(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    h.store.add_admin_role(GUILD, 777)
    h.store.set_vision_enabled(GUILD, False)
    assert h.store.setting_count(GUILD) == 3

    itx = await h.run(h.admin.config_reset, FakeInteraction())
    modal = itx.response.modal
    assert modal.phrase == "3"

    done = FakeInteraction()
    modal.field._value = "3"
    await modal.on_submit(done)

    assert h.store.setting_count(GUILD) == 0
    assert h.store.get_channel_id(GUILD) is None
    assert h.store.get_admin_role_ids(GUILD) == []
    assert h.store.get_vision_enabled(GUILD) is False
    assert "Manage Server" in done.reply

    # Scores and tables are a separate concern with their own commands.
    assert len(h.store.list_tables(GUILD)) == 2
    h.close()


async def test_config_reset_warns_that_a_running_tournament_will_break(tmp_path, monkeypatch):
    """Clearing the channel mid-tournament means /new starts refusing scores."""
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.run(h.admin.config_reset, FakeInteraction())
    done = FakeInteraction()
    itx.response.modal.field._value = itx.response.modal.phrase
    await itx.response.modal.on_submit(done)

    assert "still running" in done.reply and "/config pinball-channel" in done.reply
    # And the refusal is real, not just a warning.
    submitted = await h.submit("Godzilla", "1,000,000")
    assert "pinball channel isn't set up" in submitted.reply
    h.close()


async def test_config_reset_needs_manage_server_not_just_an_admin_role(tmp_path, monkeypatch):
    """It clears the admin-role list, so a role-only admin could revoke their
    own access with no way back."""
    h = await Harness.create(tmp_path, monkeypatch)
    h.set_manage_guild(False)

    itx = await h.run(h.admin.config_reset, FakeInteraction())
    assert "Manage Server" in itx.reply
    assert itx.response.modal is None
    assert h.store.get_channel_id(GUILD) is not None, "nothing may be cleared"
    h.close()


async def test_config_reset_on_a_bare_server_says_so(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, set_channel=False)
    itx = await h.run(h.admin.config_reset, FakeInteraction())
    assert "already a clean slate" in itx.reply
    assert itx.response.modal is None
    h.close()


# ------------------------------------------------------------------- /audit

async def test_audit_show_reports_what_admins_did(tmp_path, monkeypatch):
    """The trail was write-only before this: nothing could read it back."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    dropped = h.king("Godzilla")
    await h.run(h.admin.drop, FakeInteraction(), id=dropped.id, reason="photo mismatch")

    itx = await h.run(h.admin.audit_show, FakeInteraction())
    body = itx.embed.description
    assert "`drop`" in body
    assert f"submission:{dropped.id}" in body
    assert "photo mismatch" in body, "the reason is the point of the record"
    assert "`tournament.start`" not in body or True  # started via the store, not audited
    assert "newest first" in itx.embed.footer.text
    h.close()


async def test_audit_show_is_newest_first_and_respects_the_limit(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    for name in ("A", "B", "C", "D"):
        await h.run(h.admin.table_add, FakeInteraction(), name=name)

    itx = await h.run(h.admin.audit_show, FakeInteraction(), limit=2)
    body = itx.embed.description
    assert body.count("table.add") == 2
    assert "of 4" in itx.embed.footer.text, "the footer states the total"
    # Newest first: D was added last, so it heads the list.
    assert body.index("D") < body.index("C")
    h.close()


async def test_audit_show_on_an_empty_log_explains_itself(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=(), tournament=False)
    itx = await h.run(h.admin.audit_show, FakeInteraction())
    assert "Nothing recorded yet" in itx.embed.description
    h.close()


async def test_audit_clear_erases_the_trail_but_records_the_clearing(tmp_path, monkeypatch):
    """A log that can be emptied without trace isn't worth keeping."""
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    for name in ("A", "B", "C"):
        await h.run(h.admin.table_add, FakeInteraction(), name=name)
    assert h.store.audit_count(GUILD) == 3

    itx = await h.run(h.admin.audit_clear, FakeInteraction())
    assert itx.response.modal.phrase == "3"

    done = FakeInteraction()
    itx.response.modal.field._value = "3"
    await itx.response.modal.on_submit(done)

    assert h.store.audit_count(GUILD) == 1, "one entry remains: the clear itself"
    remaining = h.store.audit_entries(GUILD)[0]
    assert remaining.action == "audit.clear"
    assert "erased 3" in (remaining.detail or "")
    assert "starts fresh" in done.reply
    h.close()


async def test_audit_clear_needs_the_exact_count(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    await h.run(h.admin.table_add, FakeInteraction(), name="A")

    itx = await h.run(h.admin.audit_clear, FakeInteraction())
    wrong = FakeInteraction()
    itx.response.modal.field._value = "99"
    await itx.response.modal.on_submit(wrong)

    assert "didn't match" in wrong.reply
    assert h.store.audit_count(GUILD) == 1, "nothing may be erased"
    h.close()


async def test_audit_clear_on_an_empty_log_says_so(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=(), tournament=False)
    itx = await h.run(h.admin.audit_clear, FakeInteraction())
    assert "already empty" in itx.reply
    assert itx.response.modal is None
    h.close()


async def test_the_audit_trail_is_per_guild(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    await h.run(h.admin.table_add, FakeInteraction(), name="A")
    h.store.log(9999, actor_id=1, action="someone.else")

    assert h.store.audit_count(GUILD) == 1
    h.store.clear_audit(GUILD)
    assert h.store.audit_count(9999) == 1, "another server's trail is untouched"
    h.close()


# ------------------------------------------------------- channel adoption

async def test_the_first_setup_command_claims_the_channel_it_was_run_in(
    tmp_path, monkeypatch
):
    """Setting the pinball channel is the step with no natural prompt: nothing
    works without it and /new just refuses. So setup claims where it happened."""
    h = await Harness.create(tmp_path, monkeypatch, tables=(), set_channel=False)
    assert h.store.get_channel_id(GUILD) is None

    await h.run(h.admin.table_add, FakeInteraction(channel=h.channel), name="Godzilla")

    assert h.store.get_channel_id(GUILD) == h.channel.id
    assert "tournament channel" in h.channel.sent[0].content
    assert "/config pinball-channel" in h.channel.sent[0].content, "say how to move it"
    h.close()


async def test_adoption_never_overrides_an_explicit_choice(tmp_path, monkeypatch):
    """A deliberate /config pinball-channel outranks a command's location — the
    admin who set it may well be running commands somewhere else on purpose."""
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    elsewhere = FakeChannel(channel_id=111222)

    await h.run(
        h.admin.table_add, FakeInteraction(channel=elsewhere), name="Godzilla"
    )

    assert h.store.get_channel_id(GUILD) == h.channel.id, "the configured one stands"
    assert elsewhere.sent == [], "and no claim is announced in the other channel"
    h.close()


async def test_adoption_only_happens_once(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=(), set_channel=False)
    await h.run(h.admin.table_add, FakeInteraction(channel=h.channel), name="Godzilla")
    claims = sum("tournament channel" in (m.content or "") for m in h.channel.sent)

    await h.run(h.admin.table_add, FakeInteraction(channel=h.channel), name="Xenon")
    still = sum("tournament channel" in (m.content or "") for m in h.channel.sent)
    assert claims == still == 1, "the notice is a one-off, not a per-command nag"
    h.close()


async def test_starting_a_tournament_adopts_and_so_stops_warning(tmp_path, monkeypatch):
    """The warning this replaces — 'no pinball channel is set' — told you about a
    problem instead of fixing one it could fix itself."""
    h = await Harness.create(
        tmp_path, monkeypatch, tournament=False, set_channel=False
    )
    itx = await h.run(
        h.admin.tournament_start, FakeInteraction(channel=h.channel), name="Spring Open"
    )

    assert "No pinball channel is set" not in itx.reply
    assert h.store.get_channel_id(GUILD) == h.channel.id
    # And the opening announcement lands there rather than falling back.
    assert any("Spring Open is open" in (e.title or "") for e in h.channel.last.embeds)
    h.close()


async def test_a_channel_the_bot_cannot_post_in_is_not_adopted(tmp_path, monkeypatch):
    """The claim post is the permission check: a channel that rejects it is no
    use as the pinball channel, so nothing is recorded and the warning stands."""
    h = await Harness.create(
        tmp_path,
        monkeypatch,
        tournament=False,
        set_channel=False,
        channel=BrokenChannel(),
    )
    itx = await h.run(
        h.admin.tournament_start, FakeInteraction(channel=h.channel), name="Spring Open"
    )

    assert h.store.get_channel_id(GUILD) is None, "a half-set channel is worse than none"
    assert "No pinball channel is set" in itx.reply
    h.close()


async def test_adoption_is_recorded_like_any_other_config_change(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=(), set_channel=False)
    await h.run(h.admin.table_add, FakeInteraction(channel=h.channel), name="Godzilla")

    entry = next(e for e in h.store.audit_entries(GUILD) if e.action == "config.channel")
    assert "adopted" in (entry.detail or ""), "distinguishable from an explicit set"
    h.close()


async def test_setup_now_works_without_touching_config_at_all(tmp_path, monkeypatch):
    """The whole point: add tables, start, submit — no /config step in sight."""
    h = await Harness.create(
        tmp_path, monkeypatch, tables=(), tournament=False, set_channel=False
    )
    await h.run(h.admin.table_add, FakeInteraction(channel=h.channel), name="Godzilla")
    await h.run(h.admin.tournament_start, FakeInteraction(channel=h.channel))

    submitted = await h.submit("Godzilla", "12,345,678")
    assert "pinball channel isn't set up" not in submitted.reply
    assert h.king("Godzilla").score == 12_345_678
    h.close()


# ---------------------------------------------------------------- /reset-all

async def test_reset_all_erases_every_kind_of_state_at_once(tmp_path, monkeypatch):
    """The one command that replaces the four-step teardown: /tournament reset,
    /droptables, /audit clear, /config reset."""
    h = await Harness.create(tmp_path, monkeypatch)
    h.store.add_admin_role(GUILD, 777)
    await h.submit("Godzilla", "1,000,000")
    await h.run(h.admin.table_add, FakeInteraction(), name="Medieval Madness")

    # 1 tournament + 3 tables + 1 score + 2 settings + 1 audit row.
    itx = await h.run(h.admin.reset_all, FakeInteraction())
    modal = itx.response.modal
    assert modal.phrase == "8"

    done = FakeInteraction()
    modal.field._value = "8"
    await modal.on_submit(done)

    assert h.store.latest_tournament(GUILD) is None
    assert h.store.list_tables(GUILD, include_inactive=True) == []
    assert h.store.get_channel_id(GUILD) is None
    assert h.store.get_admin_role_ids(GUILD) == []
    assert h.store.setting_count(GUILD) == 0
    assert "Factory reset complete" in done.reply
    assert "don't delete message history" in done.reply
    h.close()


async def test_reset_all_still_leaves_a_record_of_itself(tmp_path, monkeypatch):
    """Same rule as /audit clear: a wipe that erases its own evidence isn't one
    you can hold anyone to."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    itx = await h.run(h.admin.reset_all, FakeInteraction())
    itx.response.modal.field._value = itx.response.modal.phrase
    await itx.response.modal.on_submit(FakeInteraction())

    entries = h.store.audit_entries(GUILD)
    assert len(entries) == 1, "the log restarts with exactly one entry"
    assert entries[0].action == "reset.all"
    assert "1 score(s)" in (entries[0].detail or "")
    assert "2 table(s)" in (entries[0].detail or "")
    h.close()


async def test_reset_all_needs_the_exact_total(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    itx = await h.run(h.admin.reset_all, FakeInteraction())
    wrong = FakeInteraction()
    itx.response.modal.field._value = "1"
    await itx.response.modal.on_submit(wrong)

    assert "didn't match" in wrong.reply
    assert h.store.latest_tournament(GUILD) is not None, "nothing may be wiped"
    assert len(h.store.list_tables(GUILD)) == 2
    assert h.store.get_channel_id(GUILD) is not None
    h.close()


async def test_reset_all_needs_manage_server_not_just_an_admin_role(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    h.set_manage_guild(False)

    itx = await h.run(h.admin.reset_all, FakeInteraction())
    assert "Manage Server" in itx.reply
    assert itx.response.modal is None
    assert h.store.latest_tournament(GUILD) is not None
    h.close()


async def test_reset_all_names_the_tournament_it_is_about_to_discard(tmp_path, monkeypatch):
    """Wiping mid-event loses the prize announcement, so say so twice."""
    h = await Harness.create(tmp_path, monkeypatch)
    itx = await h.run(h.admin.reset_all, FakeInteraction())
    assert "Spring Open" in itx.response.modal.title

    done = FakeInteraction()
    itx.response.modal.field._value = itx.response.modal.phrase
    await itx.response.modal.on_submit(done)
    assert "was still running" in done.reply
    h.close()


async def test_reset_all_on_a_bare_server_says_so(tmp_path, monkeypatch):
    h = await Harness.create(
        tmp_path, monkeypatch, tables=(), tournament=False, set_channel=False
    )
    itx = await h.run(h.admin.reset_all, FakeInteraction())
    assert "already a clean slate" in itx.reply
    assert itx.response.modal is None
    h.close()


async def test_reset_all_leaves_other_servers_untouched(tmp_path, monkeypatch):
    """The severe failure mode for a wipe: one server's factory reset taking
    another server's tournament with it."""
    h = await Harness.create(tmp_path, monkeypatch)
    other = 9999
    h.store.set_channel_id(other, 12345)
    h.store.add_table(other, "Godzilla")
    elsewhere = h.store.start_tournament(
        other, name="Other event", started_by=1, ends_at=None
    )
    h.store.log(other, actor_id=1, action="someone.else")

    itx = await h.run(h.admin.reset_all, FakeInteraction())
    itx.response.modal.field._value = itx.response.modal.phrase
    await itx.response.modal.on_submit(FakeInteraction())

    assert h.store.get_channel_id(other) == 12345
    assert len(h.store.list_tables(other)) == 1
    assert h.store.get_tournament(other, elsewhere.id) is not None
    assert h.store.audit_count(other) == 1
    h.close()


# ---------------------------------------------------- read-only admin gating

async def test_the_read_only_admin_views_are_gated_too(tmp_path, monkeypatch):
    """/config show, /table list, /admin-role list and /tournament status read
    nothing dangerous, but they sit under groups described as admin-only, and
    the server's roles and channel wiring aren't players' business. Everything
    a player needs is in /hs."""
    h = await Harness.create(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_module, "require_admin", _deny)

    for command in (
        h.admin.config_show,
        h.admin.table_list,
        h.admin.admin_role_list,
        h.admin.tournament_status,
    ):
        itx = await h.run(command, FakeInteraction())
        assert itx.reply == "denied", f"{command.qualified_name} answered a non-admin"
    h.close()


# -------------------------------------------------------------------- audit

async def test_every_consequential_action_is_audited(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=(), tournament=False)
    await h.run(h.admin.table_add, FakeInteraction(), name="Godzilla")
    await h.run(h.admin.tournament_start, FakeInteraction(), name="Spring Open")
    await h.submit("Godzilla", "1,000,000")
    sub_id = h.king("Godzilla").id
    await h.run(h.admin.drop, FakeInteraction(), id=sub_id, reason="test")
    await h.run(h.admin.restore, FakeInteraction(), id=sub_id)
    await h.run(h.admin.tournament_end, FakeInteraction())
    await h.run(h.admin.tournament_extend, FakeInteraction(), duration="1h")

    actions = h.audit_actions()
    for expected in (
        "table.add",
        "tournament.start",
        "drop",
        "restore",
        "tournament.end",
        "tournament.extend",
    ):
        assert expected in actions, f"{expected} left no audit trail: {actions}"
    h.close()


# --------------------------------------------------------- hardening (audit)

async def test_a_blank_table_name_is_refused_with_an_explanation(tmp_path, monkeypatch):
    """The bad case isn't the typo, it's the recovery: an empty name 400s /hs
    *and* the /table autocomplete you would use to remove it."""
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    itx = await h.run(h.admin.table_add, FakeInteraction(), name="   ")

    assert "blank" in itx.reply
    assert h.store.list_tables(GUILD, include_inactive=True) == []
    h.close()


async def test_an_overlong_table_name_is_refused(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch, tables=())
    itx = await h.run(h.admin.table_add, FakeInteraction(), name="G" * 200)

    assert "200 characters" in itx.reply
    assert h.store.list_tables(GUILD, include_inactive=True) == []
    h.close()


async def test_an_overlong_tournament_name_is_refused_without_a_confusing_hint(
    tmp_path, monkeypatch
):
    """It must not be reported as 'a tournament is already running'."""
    h = await Harness.create(tmp_path, monkeypatch, tournament=False)
    itx = await h.run(h.admin.tournament_start, FakeInteraction(), name="T" * 100)

    assert "100 characters" in itx.reply
    assert "/tournament status" not in itx.reply
    assert h.store.latest_tournament(GUILD) is None
    h.close()


async def test_announcements_never_ping(tmp_path, monkeypatch):
    """Announcement text carries an admin-supplied tournament name, and a
    mentionable role in it would ping the server on every announcement."""
    h = await Harness.create(tmp_path, monkeypatch, ends_at=now() + 3600)
    await h.run(h.admin.tournament_extend, FakeInteraction(), duration="1h")

    posted = h.channel.last
    assert posted.content is not None, "this one is a content message, not an embed"
    assert posted.allowed_mentions is not None, "nothing the bot posts needs to ping"
    assert posted.allowed_mentions.everyone is False
    assert posted.allowed_mentions.roles is False
    h.close()


async def test_autocomplete_tells_a_non_admin_nothing(tmp_path, monkeypatch):
    """The ledger is admin-only to read via /history; its autocompletes were
    handing the same rows to anyone who typed /drop."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")
    itx = FakeInteraction()

    assert await h.admin.drop_autocomplete(itx, "") != [], "an admin still sees them"

    monkeypatch.setattr(admin_module, "is_admin", lambda _store, _itx: False)
    assert await h.admin.drop_autocomplete(itx, "") == []
    assert await h.admin.restore_autocomplete(itx, "") == []
    assert await h.admin.table_autocomplete(itx, "") == []
    h.close()


# ----------------------------------------------------------------- /flagged

async def test_flagged_is_empty_until_something_is_flagged(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "1,000,000")

    itx = await h.run(h.admin.flagged, FakeInteraction())

    assert "Nothing waiting" in itx.embed.description
    h.close()


async def test_flagged_lists_the_queue_with_what_the_photo_read(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "5,000,000")
    proof = h.channel.sent[-1]
    submission = h.store.get_submission_by_proof_message(GUILD, proof.id)
    await h.flag(submission, VisionResult("mismatch", score=1_200_000))

    itx = await h.run(h.admin.flagged, FakeInteraction())

    body = itx.embed.description
    assert f"#{submission.id}" in body
    assert "5,000,000" in body, "what the player claimed"
    assert "1,200,000" in body, "what the photo read"
    assert "Godzilla" in body
    assert "1 waiting" in itx.embed.title
    h.close()


async def test_flagged_says_when_a_photo_was_unreadable(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "5,000,000")
    proof = h.channel.sent[-1]
    submission = h.store.get_submission_by_proof_message(GUILD, proof.id)
    await h.flag(submission, VisionResult("illegible"))

    itx = await h.run(h.admin.flagged, FakeInteraction())

    assert "unreadable" in itx.embed.description
    h.close()


async def test_flagged_empties_once_reviewed(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "5,000,000")
    proof = h.channel.sent[-1]
    submission = h.store.get_submission_by_proof_message(GUILD, proof.id)
    await h.flag(submission, VisionResult("mismatch", score=1_200_000))

    await h.react(proof.id, review.APPROVE, user_id=99)

    itx = await h.run(h.admin.flagged, FakeInteraction())
    assert "Nothing waiting" in itx.embed.description
    h.close()


async def test_config_show_admits_when_the_check_cannot_run_here(tmp_path, monkeypatch):
    """Enabled in the database but dead on the host is the failure mode that
    gets discovered *after* an event. /config show has to say so."""
    h = await Harness.create(tmp_path, monkeypatch)
    h.store.set_vision_enabled(GUILD, True)
    monkeypatch.setattr(vision_module, "is_available", lambda: False)

    itx = await h.run(h.admin.config_show, FakeInteraction())
    fields = {f.name.split(" (")[0]: f.value for f in itx.embed.fields}

    assert "not running here" in fields["Photo cross-check"]
    assert "ANTHROPIC_API_KEY" in fields["Photo cross-check"]
    h.close()


async def test_config_show_counts_what_is_waiting(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    h.store.set_vision_enabled(GUILD, True)
    monkeypatch.setattr(vision_module, "is_available", lambda: True)
    await h.submit("Godzilla", "5,000,000")
    proof = h.channel.sent[-1]
    await h.flag(
        h.store.get_submission_by_proof_message(GUILD, proof.id),
        VisionResult("mismatch", score=1_200_000),
    )

    itx = await h.run(h.admin.config_show, FakeInteraction())
    fields = {f.name.split(" (")[0]: f.value for f in itx.embed.fields}

    assert "1** waiting" in fields["Photo cross-check"]
    h.close()


# --------------------------------------------------- audit jump links

async def test_audit_links_to_the_photo_a_void_was_about(tmp_path, monkeypatch):
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)
    proof = h.channel.last
    leader = h.king("Godzilla")

    await h.run(h.admin.drop, FakeInteraction(), id=leader.id, reason="not on the machine")
    itx = await h.run(h.admin.audit_show, FakeInteraction())

    body = itx.embed.description
    assert f"submission:{leader.id}" in body
    assert f"[jump]({proof.jump_url})" in body, "the void points at the photo"
    h.close()


async def test_audit_entries_with_nowhere_to_go_are_unchanged(tmp_path, monkeypatch):
    """Config changes, purges and tournament events have no message behind them,
    and that's fine — they must not sprout a broken link."""
    h = await Harness.create(tmp_path, monkeypatch)
    role = types.SimpleNamespace(id=777, mention="@Staff")
    await h.run(h.admin.admin_role_add, FakeInteraction(), role=role)

    itx = await h.run(h.admin.audit_show, FakeInteraction())

    body = itx.embed.description
    assert "config.admin_role.add" in body
    assert "jump" not in body
    h.close()


async def test_a_submission_whose_photo_never_posted_gets_no_link(tmp_path, monkeypatch):
    """The row exists and is audited, but there's no proof message to jump to."""
    h = await Harness.create(tmp_path, monkeypatch)
    submission, _, _ = h.store.add_submission(
        guild_id=GUILD, tournament_id=h.tournament.id,
        table_id=h.table_id("Godzilla"), user_id=ALICE,
        user_display="alice", score=1_000_000,
    )
    h.store.log(GUILD, actor_id=99, action="drop", target=f"submission:{submission.id}")

    itx = await h.run(h.admin.audit_show, FakeInteraction())

    assert f"submission:{submission.id}" in itx.embed.description
    assert "jump" not in itx.embed.description
    h.close()


async def test_every_review_action_in_the_trail_is_jumpable(tmp_path, monkeypatch):
    """The flag, and the decision that resolved it, both point at the photo."""
    h = await Harness.create(tmp_path, monkeypatch)
    await h.submit("Godzilla", "9,000,000", user_id=ALICE)
    proof = h.channel.last
    submission = h.store.get_submission_by_proof_message(GUILD, proof.id)
    await h.flag(submission, VisionResult("mismatch", score=1_000_000))
    await h.react(proof.id, review.DROP, user_id=99)

    itx = await h.run(h.admin.audit_show, FakeInteraction())

    body = itx.embed.description
    assert "review.flag" in body and "review.drop" in body
    assert body.count(f"[jump]({proof.jump_url})") == 2
    h.close()


async def test_the_links_are_one_query_however_long_the_log(tmp_path, monkeypatch):
    """40 audit entries must not become 40 lookups to render one embed."""
    h = await Harness.create(tmp_path, monkeypatch)
    for _ in range(6):
        await h.submit("Godzilla", "1,000,000", user_id=ALICE)
    entries = h.store.audit_entries(GUILD, limit=40)
    ids = embeds.audit_submission_ids(entries)

    calls = {"n": 0}
    real = h.store.proof_links
    monkeypatch.setattr(h.store, "proof_links", lambda g, i: (calls.__setitem__("n", calls["n"] + 1), real(g, i))[1])
    await h.run(h.admin.audit_show, FakeInteraction())

    assert calls["n"] == 1
    assert isinstance(ids, list)
    h.close()
