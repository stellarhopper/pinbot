"""Embed and message builders.

Timestamps are always Discord's dynamic ``<t:epoch:style>`` markup so every
viewer sees their own local time — the alternative is picking a timezone and
being wrong for anyone who travelled to the LAN.

The high-score embeds try to say more than a number. In a king-of-the-hill
tournament the interesting facts are *how long* someone has held a table, *how
close* the chasers are, and *how many* challengers have already failed — all of
which the ledger already knows.
"""

from __future__ import annotations

from datetime import timedelta

import discord

from .durations import format_duration
from .scoring import format_score
from .store import AuditEntry, Submission, Table, TableStats, Tournament, now
from .vision import VisionResult

GOLD = discord.Color.from_rgb(240, 185, 40)
GREY = discord.Color.from_rgb(120, 125, 135)
RED = discord.Color.from_rgb(215, 80, 70)
GREEN = discord.Color.from_rgb(85, 175, 110)
BLURPLE = discord.Color.blurple()

CROWN = "\N{CROWN}"
# Leading markers for the history ledger, so state is scannable down the column.
VOID_MARK = "\N{CROSS MARK}"
BULLET = "\N{BLACK SMALL SQUARE}"
MEDALS = ("\N{FIRST PLACE MEDAL}", "\N{SECOND PLACE MEDAL}", "\N{THIRD PLACE MEDAL}")

# A stable accent per table, so several embeds in one /hs listing are tellable
# apart at a glance.
#
# Ordered by adjacent contrast, not by hue family: tables are assigned
# *consecutive* slots, so the first few entries are the ones that will actually
# appear side by side, and each should look nothing like the one before it.
# None of them may be mistaken for the state colours above — GOLD is a crown,
# RED is a void, GREEN is a restore.
_ACCENTS = (
    0x5865F2,  # blurple
    0xF47B67,  # coral
    0x57F287,  # mint
    0xEB459E,  # fuchsia
    0xFEE75C,  # yellow
    0xA652FF,  # violet
    0x1ABC9C,  # teal
    0xE67E22,  # orange
    0x00A8FC,  # azure
    0xFF6FA5,  # rose
    0x8DC63F,  # lime
    0x9B6DFF,  # periwinkle
)


def table_color(table: Table) -> discord.Color:
    """The accent stripe for one table's embed.

    Keyed on position, not on the name. Hashing the name into the palette
    collided on the third table added to a real server — with eight slots, three
    tables collide about a third of the time, which is just the birthday
    paradox. `sort_order` is unique per guild, never reused, and survives a
    retire/re-add, so consecutive tables get consecutive accents and no two
    share a colour until the palette is exhausted.
    """
    return discord.Color(_ACCENTS[(table.sort_order - 1) % len(_ACCENTS)])


def ts(epoch: int | None, style: str = "f") -> str:
    """Render a unix timestamp as Discord dynamic markup."""
    return f"<t:{int(epoch)}:{style}>" if epoch else "unknown"


def _held_for(submission: Submission, until: int | None = None) -> str:
    seconds = max(0, (until or now()) - submission.created_at)
    return format_duration(timedelta(seconds=seconds)) if seconds >= 60 else "just now"


def _rank(index: int) -> str:
    return MEDALS[index] if index < len(MEDALS) else f"`{index + 1}.`"


def _crown_author(
    embed: discord.Embed,
    submission: Submission,
    label: str,
    avatar_url: str | None = None,
) -> None:
    """Put the holder's name and avatar on the embed's author line.

    Prefers a URL the caller has already resolved (see bot/avatars.py), then the
    snapshot taken at submission time. Scores recorded before the snapshot
    existed have neither, and simply show no icon.
    """
    embed.set_author(name=label, icon_url=avatar_url or submission.user_avatar or None)


def _margin_line(king: Submission, runner_up: Submission | None) -> str:
    if runner_up is None:
        return "unchallenged so far"
    lead = king.score - runner_up.score
    if lead == 0:
        return "tied on score — held on the earlier attempt"
    return f"leading by {format_score(lead)}"


def _proof_link(submission: Submission) -> str | None:
    return f"[view proof]({submission.proof_jump_url})" if submission.proof_jump_url else None


# ------------------------------------------------------------- announcements

def crown_embed(
    table_name: str,
    submission: Submission,
    previous: Submission | None,
    filename: str,
    avatar_url: str | None = None,
) -> discord.Embed:
    """The loud announcement when a submission takes the crown.

    The image is referenced as ``attachment://`` rather than by CDN URL so this
    message still renders its photo days later.
    """
    lines = [f"# {format_score(submission.score)}"]
    if previous is not None:
        lines.append(
            f"Dethrones <@{previous.user_id}> by "
            f"**{format_score(submission.score - previous.score)}**, who held it "
            f"for {_held_for(previous, submission.created_at)}."
        )
    else:
        lines.append("**First on the board** — the table is claimed.")

    embed = discord.Embed(
        title=f"{CROWN} {table_name}",
        description="\n".join(lines),
        color=GOLD,
    )
    _crown_author(
        embed, submission, f"{submission.user_display} takes the crown", avatar_url
    )
    if submission.note:
        embed.add_field(name="Note", value=submission.note[:1000], inline=False)
    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(text=f"submission #{submission.id}")
    embed.timestamp = discord.utils.utcnow()
    return embed


def logged_line(table_name: str, submission: Submission, king: Submission | None) -> str:
    """Compact public line for a submission that did not take the crown.

    Still public, because the message is the only durable home for the photo —
    but deliberately quiet next to the crown announcement.
    """
    if king is None:
        return (
            f"\N{CAMERA} <@{submission.user_id}> · **{table_name}** · "
            f"{format_score(submission.score)} · `#{submission.id}`"
        )
    gap = king.score - submission.score
    return (
        f"\N{CAMERA} <@{submission.user_id}> · **{table_name}** · "
        f"{format_score(submission.score)} — **{format_score(gap)}** short of "
        f"<@{king.user_id}>'s {format_score(king.score)} · `#{submission.id}`"
    )


# -------------------------------------------------------------- photo check

# Discord renders "-# " as small grey subtext, which is exactly the weight this
# deserves: present on every photo, but never competing with the score itself.
VISION_PREFIX = "-# \N{CAMERA WITH FLASH} Photo check:"
VISION_FIELD = "Photo check"


def vision_status(result: VisionResult, table_name: str | None = None) -> str:
    """One line describing what the photo check made of a submission.

    Written to be read by players, not just admins: it appears under every
    photo once the check has run. A verdict of "unavailable" is the bot's own
    failure, so it says so plainly and makes clear the score is unaffected —
    silence there is what made a broken check indistinguishable from a working
    one.
    """
    if result.verdict == "match":
        text = f"matches — read {format_score(result.score)} off the photo."
    elif result.verdict == "mismatch":
        text = (
            f"the photo reads **{format_score(result.score)}** — flagged for a "
            "second look."
        )
    elif result.verdict == "illegible":
        text = "couldn't read the display — flagged for a second look."
    else:
        text = "couldn't run just now. The score stands."

    if table_name and result.wrong_table(table_name):
        text += f" The machine looks like **{result.table_name}**."
    return text


def vision_reviewed(
    approved: bool, reviewer_id: int, *, withdrawn: bool = False
) -> str:
    """Replaces the status line once a human has ruled on a flag."""
    if approved:
        return f"reviewed by <@{reviewer_id}> — the score stands."
    if withdrawn:
        return f"withdrawn by <@{reviewer_id}>."
    return f"reviewed by <@{reviewer_id}> — score dropped."


def strip_vision_line(content: str | None) -> str:
    """Remove any status line already there, so re-annotating can't stack them."""
    if not content:
        return ""
    kept = [ln for ln in content.split("\n") if not ln.startswith(VISION_PREFIX)]
    return "\n".join(kept).rstrip()


# Discord's two limits on a single message, both of which /hs can reach on a
# real event: ten embeds, and 6000 characters summed across all of them. The
# character one is the easy one to miss, because it only bites once table names
# and standings get long — i.e. at the event, not in testing.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_CHARS_PER_MESSAGE = 6000


def chunk_embeds(built: list[discord.Embed]) -> list[list[discord.Embed]]:
    """Split embeds into messages that Discord will actually accept.

    Returns at least one page for a non-empty input. An embed too large to
    share a message with anything gets one of its own rather than being
    dropped: silently losing a table from the standings is the failure this
    exists to prevent.
    """
    pages: list[list[discord.Embed]] = []
    page: list[discord.Embed] = []
    used = 0
    for embed in built:
        size = len(embed)
        if page and (
            len(page) >= MAX_EMBEDS_PER_MESSAGE
            or used + size > MAX_EMBED_CHARS_PER_MESSAGE
        ):
            pages.append(page)
            page, used = [], 0
        page.append(embed)
        used += size
    if page:
        pages.append(page)
    return pages


# ----------------------------------------------------------------- standings

def table_summary_embed(
    table: Table,
    king: Submission | None,
    runner_up: Submission | None,
    stats: TableStats,
    fresh_url: str | None,
    avatar_url: str | None = None,
) -> discord.Embed:
    """One table's standing, for the `/hs` listing."""
    if king is None:
        embed = discord.Embed(
            title=table.name,
            description="No scores yet — **wide open**.",
            color=GREY,
        )
        embed.set_footer(text="be the first to claim it")
        return embed

    lines = [
        f"# {format_score(king.score)}",
        f"{CROWN} <@{king.user_id}>",
        f"held for **{_held_for(king)}** · {_margin_line(king, runner_up)}",
    ]
    if link := _proof_link(king):
        lines.append(link)

    embed = discord.Embed(
        title=table.name,
        description="\n".join(lines),
        color=table_color(table),
    )
    _crown_author(embed, king, f"{king.user_display} — king of the hill", avatar_url)
    if fresh_url:
        embed.set_thumbnail(url=fresh_url)

    footer = f"{stats.attempts} attempt(s) · {stats.players} player(s)"
    if stats.challenges:
        footer += f" · survived {stats.challenges} challenge(s)"
    embed.set_footer(text=footer)
    return embed


def table_detail_embed(
    table: Table,
    king: Submission | None,
    standings: list[Submission],
    stats: TableStats,
    fresh_url: str | None,
    avatar_url: str | None = None,
) -> discord.Embed:
    """One table in detail, for `/hs table:`."""
    if king is None:
        embed = discord.Embed(
            title=table.name,
            description="No scores yet — **wide open**. Post one with `/new`.",
            color=GREY,
        )
        return embed

    chasers = standings[1:]
    runner_up = chasers[0] if chasers else None

    lines = [f"# {format_score(king.score)}", f"{CROWN} <@{king.user_id}>"]
    if link := _proof_link(king):
        lines.append(link)
    if king.note:
        lines.append(f"> {king.note[:200]}")

    embed = discord.Embed(
        title=table.name,
        description="\n".join(lines),
        color=table_color(table),
    )
    _crown_author(embed, king, f"{king.user_display} — king of the hill", avatar_url)

    embed.add_field(name="Held for", value=_held_for(king), inline=True)
    embed.add_field(
        name="Margin",
        value=(
            format_score(king.score - runner_up.score) if runner_up else "unchallenged"
        ),
        inline=True,
    )
    embed.add_field(
        name="Challenges survived", value=str(stats.challenges), inline=True
    )

    if chasers:
        embed.add_field(
            name="Chasing",
            value="\n".join(
                f"{_rank(i + 1)} **{format_score(s.score)}** — <@{s.user_id}> "
                f"(needs +{format_score(king.score - s.score + 1)})"
                for i, s in enumerate(chasers)
            ),
            inline=False,
        )

    if fresh_url:
        embed.set_image(url=fresh_url)
    embed.set_footer(
        text=(
            f"submission #{king.id} · {stats.attempts} attempt(s) by "
            f"{stats.players} player(s)"
        )
    )
    embed.timestamp = discord.utils.snowflake_time(king.proof_message_id) if king.proof_message_id else None
    return embed


def standings_header(
    tournament: Tournament, submissions: int, players: int, tables: int
) -> str:
    """The one-line summary above an `/hs` listing."""
    state = "current standings" if tournament.is_open else "final standings"
    bits = [
        f"**{tournament.label}** — {state}",
        f"{tables} table(s) · {submissions} score(s) · {players} player(s)",
    ]
    if tournament.is_open and tournament.ends_at:
        bits.append(f"ends {ts(tournament.ends_at, 'R')}")
    return " · ".join(bits)


SUBMISSION_TARGET = "submission:"


def audit_submission_ids(entries: list[AuditEntry]) -> list[int]:
    """Submission IDs referenced by these audit entries.

    The ``target`` format is written in half a dozen places; this is the one
    place that reads it, so a change to the format breaks here rather than
    silently producing an audit log with no links in it.
    """
    found: list[int] = []
    for entry in entries:
        if not entry.target or not entry.target.startswith(SUBMISSION_TARGET):
            continue
        try:
            found.append(int(entry.target[len(SUBMISSION_TARGET):]))
        except ValueError:
            continue
    return found


def audit_embed(
    entries: list[AuditEntry],
    total: int,
    cleared_note: str | None = None,
    links: dict[str, str] | None = None,
) -> discord.Embed:
    """The admin action trail: who did what, when, and why.

    Exists because everything else in the bot writes to this log and nothing
    could read it — the record was only reachable by opening the database.

    ``links`` maps a target to the proof post it refers to. Plenty of entries
    have nowhere to jump — a config change, a purge, a score whose photo never
    posted — and those simply read as they did before.
    """
    if not entries:
        return discord.Embed(
            title="Audit log",
            description=(
                cleared_note
                or "Nothing recorded yet. Admin actions — voids, purges, "
                "config and tournament changes — show up here."
            ),
            color=GREY,
        )

    lines = []
    for entry in entries:
        parts = [f"{ts(entry.at, 'f')} · <@{entry.actor_id}> · `{entry.action}`"]
        if entry.target:
            parts.append(f"`{entry.target}`")
            if url := (links or {}).get(entry.target):
                parts.append(f"[jump]({url})")
        line = " · ".join(parts)
        if entry.detail:
            line += f"\n　　{entry.detail}"
        lines.append(line)

    embed = discord.Embed(
        title="Audit log",
        description="\n".join(lines)[:4000],
        color=BLURPLE,
    )
    suffix = f" of {total}" if total > len(entries) else ""
    embed.set_footer(text=f"showing {len(entries)}{suffix} entr(ies), newest first")
    return embed


def history_embed(
    table: Table, submissions: list[Submission], king: Submission | None = None
) -> discord.Embed:
    """Full ledger for one table, voided entries included. Admin-only view.

    The standing score is marked, because with voided entries interleaved and
    ties broken by timestamp, the top of the ledger is not necessarily the
    crown — which is exactly the thing an admin is checking.
    """
    if not submissions:
        return discord.Embed(
            title=f"{table.name} — history",
            description="No submissions on this table yet.",
            color=GREY,
        )

    lines: list[str] = []
    for sub in submissions:
        if sub.is_voided:
            reason = f" — {sub.void_reason}" if sub.void_reason else ""
            head = (
                f"{VOID_MARK} ~~`#{sub.id}` {format_score(sub.score)}~~ — "
                f"<@{sub.user_id}> voided by <@{sub.voided_by}>{reason}"
            )
        elif king is not None and sub.id == king.id:
            head = (
                f"{CROWN} `#{sub.id}` **{format_score(sub.score)}** — "
                f"<@{sub.user_id}> **← current high score**"
            )
        else:
            head = f"{BULLET} `#{sub.id}` {format_score(sub.score)} — <@{sub.user_id}>"
        link = _proof_link(sub)
        suffix = f" · {link}" if link else ""
        lines.append(f"{head} · {ts(sub.created_at, 'R')}{suffix}")

    embed = discord.Embed(
        title=f"{table.name} — history",
        description="\n".join(lines)[:4000],
        color=BLURPLE,
    )
    if king is not None and not any(sub.id == king.id for sub in submissions):
        # The page only holds the most recent entries, so a long-standing crown
        # can fall off the end. Saying so beats implying there isn't one.
        embed.set_footer(
            text=(
                f"current high score is #{king.id} — {format_score(king.score)} — "
                "older than this page; raise the limit to see it"
            )
        )
    elif king is None:
        embed.set_footer(text="no standing high score — every score here is voided")
    return embed


def flagged_embed(
    submissions: list[Submission], table_names: dict[int, str]
) -> discord.Embed:
    """The photo-review queue: what the check flagged and nobody has judged yet.

    Each line carries a jump link, because the review itself happens by
    reacting on the proof post rather than in here.
    """
    if not submissions:
        return discord.Embed(
            title="Photo review",
            description=(
                "Nothing waiting. Scores get flagged here when the photo doesn't "
                "match what was reported, or when it can't be read at all."
            ),
            color=GREY,
        )

    lines: list[str] = []
    for sub in submissions:
        table = table_names.get(sub.table_id, "an unknown table")
        if sub.vision_verdict == "illegible":
            finding = "photo unreadable"
        elif sub.vision_score is not None:
            finding = f"photo reads {format_score(sub.vision_score)}"
        else:
            finding = "needs a look"
        link = _proof_link(sub)
        suffix = f" · {link}" if link else " · _no proof post_"
        lines.append(
            f"{BULLET} `#{sub.id}` **{format_score(sub.score)}** on {table} — "
            f"<@{sub.user_id}> · {finding} · {ts(sub.flagged_at, 'R')}{suffix}"
        )

    embed = discord.Embed(
        title=f"Photo review ({len(submissions)} waiting)",
        description="\n".join(lines)[:4000],
        color=BLURPLE,
    )
    embed.set_footer(
        text="React ✅ or ❌ on the proof photo to keep or drop it, or use /drop"
    )
    return embed


# -------------------------------------------------------------- void/restore

def void_embed(
    table_name: str, voided: Submission, new_king: Submission | None, actor_id: int
) -> discord.Embed:
    reason = voided.void_reason or "no reason given"
    embed = discord.Embed(
        title="\N{WARNING SIGN} Score voided",
        description=(
            f"`#{voided.id}` — **{format_score(voided.score)}** by "
            f"<@{voided.user_id}> on **{table_name}** was voided by <@{actor_id}>.\n"
            f"Reason: {reason}"
        ),
        color=RED,
    )
    if new_king is None:
        embed.add_field(
            name="New standing", value=f"**{table_name}** is now open — no scores.", inline=False
        )
    else:
        embed.add_field(
            name="New standing",
            value=(
                f"{CROWN} <@{new_king.user_id}> — **{format_score(new_king.score)}** "
                f"(`#{new_king.id}`)"
            ),
            inline=False,
        )
    return embed


def restore_embed(
    table_name: str, restored: Submission, new_king: Submission | None, actor_id: int
) -> discord.Embed:
    embed = discord.Embed(
        title="\N{WHITE HEAVY CHECK MARK} Score restored",
        description=(
            f"`#{restored.id}` — **{format_score(restored.score)}** by "
            f"<@{restored.user_id}> on **{table_name}** was restored by <@{actor_id}>."
        ),
        color=GREEN,
    )
    if new_king is not None:
        embed.add_field(
            name="New standing",
            value=(
                f"{CROWN} <@{new_king.user_id}> — **{format_score(new_king.score)}** "
                f"(`#{new_king.id}`)"
            ),
            inline=False,
        )
    return embed


# -------------------------------------------------------------- tournaments

def tournament_started_embed(tournament: Tournament, table_names: list[str]) -> discord.Embed:
    embed = discord.Embed(
        title=f"\N{GAME DIE} {tournament.label} is open",
        description=(
            "Play any open table, then post your score with `/new`. "
            "A photo of the score screen is required.\n"
            "Check standings any time with `/hs`."
        ),
        color=GREEN,
    )
    embed.add_field(name="Started", value=ts(tournament.started_at, "f"), inline=True)
    embed.add_field(
        name="Ends",
        value=ts(tournament.ends_at, "f") if tournament.ends_at else "when an admin says so",
        inline=True,
    )
    embed.add_field(
        name=f"Tables ({len(table_names)})",
        value="\n".join(f"• {name}" for name in table_names) or "none configured yet",
        inline=False,
    )
    return embed


def field_name(name: str) -> str:
    """A table name that Discord will accept as an embed field name.

    Names are validated on the way *in* (store.clean_name), so this only ever
    matters for rows written before that existed — but the failure it prevents
    is the worst kind: an empty or over-long field name 400s the whole embed,
    so one bad table takes down the final results for every table.
    """
    cleaned = name.strip()
    if not cleaned:
        return "(unnamed table)"
    return cleaned if len(cleaned) <= 256 else cleaned[:253] + "..."


def final_results_embed(
    tournament: Tournament, results: list[tuple[Table, Submission | None]]
) -> discord.Embed:
    embed = discord.Embed(
        title=f"\N{TROPHY} {tournament.label} — final results",
        description=(
            f"Ran {ts(tournament.started_at, 'f')} → {ts(tournament.ended_at, 'f')}.\n"
            "Congratulations to the kings of the hill:"
        ),
        color=GOLD,
    )
    for table, king in results:
        if king is None:
            embed.add_field(name=field_name(table.name), value="no scores set", inline=False)
            continue
        value = (
            f"{CROWN} <@{king.user_id}> — **{format_score(king.score)}**\n"
            f"held for {_held_for(king, tournament.ended_at)}"
        )
        if link := _proof_link(king):
            value += f" · {link}"
        embed.add_field(name=field_name(table.name), value=value, inline=False)
    if not results:
        embed.add_field(name="Tables", value="none were configured", inline=False)
    embed.timestamp = discord.utils.utcnow()
    return embed


def status_embed(
    tournament: Tournament | None, submissions: int, table_count: int
) -> discord.Embed:
    if tournament is None:
        return discord.Embed(
            title="No tournament has run here yet",
            description="An admin can open one with `/tournament start`.",
            color=GREY,
        )
    if tournament.is_open:
        embed = discord.Embed(
            title=f"{tournament.label} is running",
            color=GREEN,
        )
        embed.add_field(name="Started", value=ts(tournament.started_at, "R"), inline=True)
        embed.add_field(
            name="Ends",
            value=ts(tournament.ends_at, "R") if tournament.ends_at else "no scheduled end",
            inline=True,
        )
    else:
        embed = discord.Embed(
            title=f"{tournament.label} has ended",
            description=f"Ended {ts(tournament.ended_at, 'R')}.",
            color=GREY,
        )
    embed.add_field(name="Submissions", value=str(submissions), inline=True)
    embed.add_field(name="Tables", value=str(table_count), inline=True)
    return embed
