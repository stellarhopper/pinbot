# Discord Pinball Bot

A Discord bot that runs a king-of-the-hill pinball tournament. Players walk up to any
open machine, play, and post their score with `/new` — a photo of the score screen is
required in the same command. If the score beats the standing one they become that
table's king. `/hs` shows the current standings with a link back to the winning photo,
and admins can void a suspect score so the crown reverts to the previous holder.

Multi-server: add it anywhere and configure it entirely through slash commands. Nothing
needs editing on disk beyond the bot token.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/new <table> <score> <proof> [note]` | anyone | Report a score. Photo required. |
| `/hs [table]` | anyone | Current high scores — all tables, or one in detail. |
| `/tournament start [name] [ends_in]` | admin | Open the submission window. `ends_in` takes `36h`, `2d`, `2d 4h 30m`, `90m`, `1w`. |
| `/tournament end` | admin | Close it and announce the winners. |
| `/tournament extend <duration>` | admin | Push the end out — also reopens a tournament ended by mistake. |
| `/tournament status` | admin | Where things stand. |
| `/table add\|remove\|rename\|list` | admin | Manage the machines in play. |
| `/config pinball-channel [channel]` | admin | Where scores and photos are posted, and the only channel the bot answers in. Set automatically by the first setup command you run; defaults to the current channel. |
| `/config admin-role add\|remove\|list` | admin | Roles that may run admin commands. |
| `/config vision <on\|off>` | admin | Photo/score cross-check (see below). |
| `/flagged` | admin | Scores the photo check flagged and nobody has judged yet. |
| `/config show` | admin | Everything configured for this server. |
| `/drop <id> [reason]` | admin | Void a score; the crown reverts. Autocompletes with standing kings first. |
| `/restore <id>` | admin | Un-void. Autocompletes with the most recent void first. |
| `/history <table> [limit]` | admin | Full ledger for a table, voided entries included. |
| `/audit show [limit]` | admin | Who did what: voids, purges, config and tournament changes. |
| `/audit clear` | admin | Erase the trail once the event is settled. |
| `/drophs` | admin | Delete all scores for the current tournament. |
| `/droptables` | admin | Delete all tables *and* their scores. |
| `/tournament reset` | admin | Discard a tournament and its scores, silently — for a test run or a false start. |
| `/config reset` | **Manage Server** | Clear all settings: channel, admin roles, vision. Tables and scores survive. |
| `/reset-all` | **Manage Server** | Factory reset: scores, tables, tournaments, settings and the audit log, in one transaction. |

**Admins** are anyone with the Discord **Manage Server** permission, or anyone holding a
role added with `/config admin-role add`. Manage Server always works, so you cannot lock
yourself out of your own tournament.

`/new` and `/hs` are the only commands players can run; everything else is admin-gated,
including the read-only views. `/config reset` and `/reset-all` need Manage Server
specifically, because both clear the admin-role list — a role-only admin running one would
be revoking their own access with nothing left to grant it back.

Every destructive command is gated by a modal that makes you type the number of rows about
to be destroyed, and none of them delete Discord message history: the proof photos stay in
the channel.

Scores are entered as text, so `12,345,678` and `12 345 678` both work.

## Setup

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application**. Name it
   whatever players should see, then **Create**.
2. Open the **Bot** tab:
   - **Reset Token** → **Copy**. Paste it straight into `.env` (step 2 below) — never into
     a chat, a commit, or a screenshot. If it leaks, reset it; that invalidates the old one.
   - Leave **Message Content Intent**, **Server Members Intent**, and **Presence Intent**
     **off**. Most bot guides tell you to enable these; this bot uses slash commands only
     and never reads message text, so it needs none of them.
   - **Public Bot**: on if others should be able to add it to their servers, off if only you.
   - **Requires OAuth2 Code Grant**: **off**. If this is on, every invite fails with an
     unhelpful error.
3. Invite it. Take the **Application ID** from the **General Information** tab and open:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=117824&scope=bot+applications.commands
   ```

   `117824` is exactly **View Channel + Send Messages + Embed Links + Attach Files +
   Read Message History + Add Reactions** — nothing more. (You can build the same thing by
   hand under **OAuth2 → URL Generator**, but the number saves the box-ticking. To make it
   stick for future servers, set the same list under **Installation → Default Install
   Settings**.)

   **Upgrading from an older invite?** `117760` predates the photo check and lacks **Add
   Reactions**, so flagged scores can't be reviewed by reacting. Permissions are not
   retroactive: either open the new invite URL again, or tick it by hand under
   *Server Settings → Roles → your bot*. `python -m bot.checkup` reports the gap per
   channel.

**`Read Message History` is not optional, and its absence is easy to misread.** `/hs`
re-reads the bot's own proof messages to get a fresh image URL (see *Why no photos on disk*
below). Without it, standings still work but show a *"view proof"* link where the photo
should be — which looks like a design choice rather than a missing permission. The bot logs
a warning naming the permission when this happens, and `python -m bot.checkup` reports it
per channel.

Once the token is in `.env`, verify the whole setup before starting the bot:

```sh
.venv/bin/python -m bot.checkup
```

It confirms the token works, names the application, prints your ready-made invite URL,
flags the Public Bot and Code Grant settings, lists the servers it's in, and — per server —
whether the pinball channel is set and whether the bot can actually post there. It makes
read-only calls and never prints the token.

### 2. Install

```sh
git clone git@github.com:stellarhopper/pinbot.git && cd pinbot
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # then put your token in it
```

### 3. Run

```sh
.venv/bin/python -m bot
```

Set `DEV_GUILD_ID` in `.env` while you're testing: commands then sync to that one server
instantly instead of taking up to an hour to appear globally. Unset it for production.

The two scopes are exclusive, and the bot enforces that. Discord *merges* a guild's commands
with the global set, so a tree published to both shows every command twice — setting
`DEV_GUILD_ID` therefore retracts the global set, and unsetting it retracts the guild copy
(the guild is remembered in the database, because by then the variable is gone). Flipping
between dev and production is that one line plus a restart, in either direction. While it is
set, the bot has no commands in any other server it's in.

To get a server's ID: Discord **Settings → Advanced → Developer Mode** on, then right-click
the server icon → **Copy Server ID**.

If commands don't show up, it's almost always one of three things: `DEV_GUILD_ID` pointing at
a server the bot isn't in, the invite missing the `applications.commands` scope, or global
sync simply not having propagated yet. `python -m bot.checkup` tells you which.

### 4. Set it up in Discord

Go to the channel you want the tournament to live in, and:

```
/table add Godzilla
/table add Attack From Mars
/table add Medieval Madness
/table add Twilight Zone
/tournament start name:Spring Open 2026 ends_in:2d
/config admin-role add @Staff        (optional)
```

The first of those commands claims the channel you ran it in as the pinball channel and
says so, so there's no separate configuration step. If the bot can't post in that channel
it doesn't claim it, and `/tournament start` warns you instead.

**From then on, the tournament has one home.** Commands run anywhere else are declined with
a pointer to the right channel — a `/new` in the wrong place would post someone's proof
photo where nobody is looking and split the event across two channels. Threads inside the
pinball channel count as the pinball channel.

The single exception is `/config pinball-channel`, which works from any channel, because
it's the command that *moves* the home — if it were scoped too, a deleted or locked-down
channel would take the whole bot with it. With no argument it moves the tournament to
whatever channel you run it in:

```
/config pinball-channel                    (move it here)
/config pinball-channel #somewhere-else    (move it there)
```

An explicit choice always outranks adoption, and adoption only ever happens when nothing is
set. As a further backstop, if the configured channel is ever deleted or hidden from the
bot, the restriction lifts automatically rather than stranding you.

Then anyone can `/new`, and `/tournament end` (or the scheduled end) posts the winners.

## Running it on a Raspberry Pi

The bot is built for a lean always-on Pi: one process, one SQLite file, no image files on
disk, and flat memory use. At most two proof photos are held in memory at a time, and the
bytes are released as soon as they are uploaded.

It deploys itself. Push to `main` and the Pi pulls, tests, and restarts on its own —
over an outbound MQTT connection, so nothing has to be reachable from the internet:

```
git push → GitHub Actions → tests → MQTT broker → Pi → tests → restart
```

One command sets the whole thing up:

```sh
git clone https://github.com/stellarhopper/pinbot.git ~/pinbot
bash ~/pinbot/deploy/setup-pi.sh
```

That installs the packages, builds the venv, prompts for the Discord token and the broker
credentials, installs two systemd units (`pinbot`, `pinbot-deployer`) with a narrow sudoers
drop-in, and starts them. The tournament database lands in `/var/lib/pinbot/pinball.db` —
outside the checkout, because deployment does `git reset --hard` and a prize-bearing ledger
shouldn't live in a directory that deployment rewrites. A deployment whose tests fail rolls
the checkout back and leaves the running bot alone.

**[deploy/README.md](deploy/README.md) has the full picture** — the GitHub secrets to set,
where every file lives, how to back the database up mid-event, and what to check when a
push doesn't land.

### Does the database grow forever?

No. A score is roughly 250 bytes, so a thousand of them is a quarter of a megabyte and ten
thousand is a couple of megabytes — the SQLite file will never be a factor, and a weekend's
few hundred writes is nothing for an SD card. `/drophs`, `/droptables`, `/tournament reset`
and `/reset-all` hard-delete and then `VACUUM`, so space actually comes back, and every
score is tagged with its tournament so each event's data can be purged independently of the
history you want to keep.

### Starting over

| You want | Run |
| --- | --- |
| A fresh run on the same machines | `/tournament reset` |
| Same, but different machines too | `/tournament reset` then `/droptables` |
| The server handed to someone else | `/reset-all` |

`/reset-all` does the work of `/tournament reset` + `/droptables` + `/audit clear` +
`/config reset` in a single transaction, so it can't half-complete and leave a state no
command explains. It writes one audit row recording what it destroyed — a wipe that erases
its own evidence isn't one you can hold anyone to.

## Why no photos on disk

Discord's attachment URLs are signed and expire after about 24 hours, so a stored image URL
is dead by day two of a multi-day event. Rather than caching images locally, the bot makes
Discord the archive:

* It re-uploads each photo as a real attachment on its own message and stores that
  message's channel ID, message ID, and jump URL — none of which expire.
* Announcement embeds point at the photo with the relative `attachment://` scheme instead
  of an absolute URL, so scrolling back to day one still shows the image.
* `/hs` re-fetches the proof message on demand to get a freshly signed URL for its
  thumbnail, cached for ten minutes so a four-table listing isn't four API calls. If that
  lookup fails it degrades to the permanent "view proof" link rather than erroring.

**The one test worth actually doing before your event:** leave the bot running overnight,
then next morning run `/hs` and scroll back to yesterday's announcements. Both should still
show photos. Any regression to storing URLs shows up here, at the 24-hour mark — which is
exactly where it would otherwise bite you mid-tournament.

Note that `/drophs` and `/droptables` delete scores from the database but do **not** delete
message history. The proof photos stay in the channel.

## Opt-in: photo/score cross-check

The bot can read the score off each submitted photo with a vision model and compare it to
what the player typed. It's **off by default** — an API call in the submission path is a new
failure mode you want to be able to switch off from your phone mid-tournament.

**What it's for.** Catching *honest mistakes early* — a fat-fingered digit, a photo of the
wrong machine, a score typed while the ball was still in play — while the player is still
standing at the machine. It is not an integrity backstop; reconcile against the machines'
own high-score tables at the end of the event for that.

**Two invariants.** It never rejects a submission, and it never fails one. The score is on
the ledger and confirmed before the check runs; every error path records "unavailable" and
says nothing.

**What gets flagged**, when the read doesn't match the claim, or the photo can't be read at
all. An unreadable photo isn't proof, so it gets an explicit human sign-off rather than a
silent pass. The bot also reports which machine it thinks is in the photo, but never flags
on that alone — backglass art is often out of frame.

**How a flag is reviewed.** The bot replies to the proof photo, publicly, mentioning the
player and the admin roles, and puts ✅ and ❌ on the photo. An admin reacting ✅ lets the
score stand; ❌ voids it through the ordinary drop path, so the crown reverts and the usual
announcement fires. Every decision is audited. `/flagged` lists anything still waiting.

Only a score the check actually flagged can be dropped this way, and only by an admin — a
stray ❌ on an ordinary proof post does nothing.

Expect a fair number of flags, and keep that in mind when reading them: every submission is
checked and glare on a DMD is common, so a flag means "worth a look", not "cheat".

```sh
.venv/bin/pip install -e '.[vision]'   # the Pi deployment installs this already
# add ANTHROPIC_API_KEY to .env, restart, then:
/config vision on
```

Roughly 3¢ per check, so about $6 over a 200-submission weekend. Use a workspace-scoped API
key with a spend limit. `/config show` tells you if the check is switched on but can't
actually run on this host — a key without the package, or the reverse, otherwise fails
silently.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

353 tests, no Discord connection needed, under a second. Two tiers:

**Unit tests** — `test_store.py`, `test_scoring.py`, `test_durations.py`, `test_perms.py`,
`test_vision.py`. The ledger (crown, voiding, reverting, tie-breaks), tournament windows,
purges, input parsing, admin access, and cross-server isolation. `test_vision.py` injects a
fake `anthropic` into `sys.modules`, so the photo check is covered with no dependency, no
network, and no API key.

**Integration tests** — `test_submit_flow.py`, `test_admin_flow.py`, `test_review_flow.py`. These call the real
command callbacks against fake Discord objects (`tests/fakes.py`), so they cover the seam
between the ledger and Discord: what actually gets announced, how proof photos are stored
and re-resolved, the destructive-purge gate, and the auto-close loop. Both bugs found on
the first live run lived in that seam rather than in `store.py`, which is why this tier
exists — a `Harness` builds a configured guild, and `await h.submit("Godzilla", "1,234,567")`
drives `/new` end to end.

`tests/conftest.py` holds an eight-line hook so tests can be written as `async def`, rather
than taking on pytest-asyncio as a dependency.

Both tiers are mutation-checked: reintroducing the decimal-stripping bug fails 9 tests,
making `/new` always announce a new king fails 2, and removing either the admin check or the
bot's-own-reaction guard from the photo review fails 1 each.

### Layout

| File | Role |
| --- | --- |
| `bot/store.py` | SQLite. The append-only submission ledger and every query. |
| `bot/scores.py` | `/new` and `/hs`. |
| `bot/admin.py` | Tournament control, tables, config, void/restore, purges, auto-close. |
| `bot/proofs.py` | Photo validation, re-upload, and the fresh-URL cache. |
| `bot/avatars.py` | Resolves a player's avatar: snapshot first, Discord as fallback. |
| `bot/embeds.py` | Every embed and message the bot posts. |
| `bot/tree.py` | Command error handling, dispatch-latency logging, channel scope. |
| `bot/perms.py` | Who counts as an admin. |
| `bot/channels.py` | Where commands may be run, and the one command exempt from it. |
| `bot/scoring.py`, `bot/durations.py` | Input parsing. |
| `bot/checkup.py` | `python -m bot.checkup` — verify the Discord setup. |
| `bot/vision.py` | The photo check itself: reads a score off an image (optional). |
| `bot/review.py` | Flagging a doubtful photo, and the ✅/❌ review that resolves it. |
| `tests/fakes.py` | Fake Discord objects + the `Harness` the integration tests drive. |

The central design decision is in `store.py`: submissions are **append-only** and voiding a
score just sets `voided_at`, so "who holds the crown" is always a derived query —
`highest live score, earliest wins ties`. Reverting after a `/drop` is therefore free and
correct even when you void a score that was never king, void several in a row, or void
everything on a table.

## License

MIT — see [LICENSE](LICENSE).
