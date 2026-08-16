# Raspberry Pi deployment

Push to `main`, and the Pi updates itself.

```
git push → GitHub Actions → tests → MQTT broker → Pi → tests → restart
```

**Nothing listens on the Pi.** It holds one outbound TLS connection to the broker,
so there's no port forwarding, no dynamic DNS, and no inbound SSH — it works
behind CGNAT and survives moving to a different network.

## First-time setup

### On the Pi

```bash
git clone https://github.com/stellarhopper/pinbot.git ~/pinbot
bash ~/pinbot/deploy/setup-pi.sh
```

It installs packages, builds the venv, prompts for the Discord token and the MQTT
credentials (writing both `chmod 600`), installs and enables the two systemd
units plus a sudoers drop-in, and starts everything. It's safe to re-run.

Then confirm the Discord side:

```bash
~/pinbot/.venv/bin/python -m bot.checkup
```

### On GitHub

Settings → Secrets and variables → Actions → **New repository secret**, three times:

| Secret | Value |
| --- | --- |
| `MQTT_BROKER` | e.g. `abc123.s1.eu.hivemq.cloud` |
| `MQTT_USERNAME` | the broker user |
| `MQTT_PASSWORD` | its password |

A free HiveMQ Cloud instance is enough — the entire traffic is one short message
per push.

## What a deployment does

`deploy.sh` runs on the Pi when the trigger arrives:

1. `git fetch` + `git reset --hard origin/main` — the Pi mirrors `main`, it isn't
   somewhere you edit.
2. If that commit changed `deploy.sh` itself, **re-exec into the new copy** (see
   below).
3. `pip install -e ".[dev,vision]"` into `.venv`.
4. **Runs the test suite.**
5. Only then `sudo systemctl restart pinbot`, and waits five seconds to confirm
   the process is still alive before declaring success.

**If step 3 or 4 fails, the checkout is rolled back to the commit that was
running and the bot is not restarted.** A tournament is live for a whole weekend;
a bad push at the wrong moment costs real scores, so the running process is left
alone unless the new code passes.

### Why step 2 exists

`deploy.sh` lives inside the checkout it replaces. Bash reads a script as it runs
and buffers it, so without the re-exec every line after the `git reset` is the
*previous* commit's version — meaning a change to this script takes effect one
deploy late, and silently: the run reports success while having done the old
thing. That is not hypothetical. It is how the fix that added `[vision]` to the
install line was deployed, logged as successful, and left `anthropic` uninstalled.

The handover compares a hash of the script taken before the reset with one taken
after, and passes `PINBOT_PREVIOUS` through the `exec` — the second pass's own
fetch is a no-op, so without it the rollback target would be the commit being
deployed rather than the one that was running, and rolling back would do nothing.

## Where things live

| Path | What |
| --- | --- |
| `~/pinbot` | the checkout, rewritten by every deployment |
| `~/pinbot/.venv` | the bot's virtualenv |
| `~/pinbot/.env` | Discord token (chmod 600, gitignored) |
| `~/pinbot/deploy/.env.mqtt` | broker credentials (chmod 600, gitignored) |
| `/var/lib/pinbot/pinball.db` | **the tournament database** |
| `/var/log/pinbot/` | `output.log`, `error.log`, `deployer.log`, `deploy.log` |

The database deliberately sits **outside** the checkout. Deployment does
`git reset --hard`, and a prize-bearing ledger has no business living in a
directory that deployment rewrites. systemd's `StateDirectory=pinbot` creates and
owns `/var/lib/pinbot`.

## The two services

| Unit | Does |
| --- | --- |
| `pinbot.service` | runs the bot (`python -m bot`), restarts on failure |
| `pinbot-deployer.service` | holds the MQTT connection, runs `deploy.sh` on a trigger |

The deployer runs on the **system** Python with `python3-paho-mqtt` from apt, not
in the bot's venv. The thing that fixes a broken deployment must not share the
broken deployment's dependencies.

Its only privilege is the sudoers drop-in in `/etc/sudoers.d/pinbot`: three exact
`systemctl` commands against `pinbot`, no wildcards.

## Day-to-day

```bash
# follow the bot
journalctl -u pinbot -f
tail -f /var/log/pinbot/output.log

# follow deployments
journalctl -u pinbot-deployer -f
tail -f /var/log/pinbot/deploy.log

# deploy by hand, without a push
bash ~/pinbot/deploy/deploy.sh

# restart / stop
sudo systemctl restart pinbot
sudo systemctl stop pinbot pinbot-deployer
```

Trigger a deployment from any machine with `mosquitto-clients`:

```bash
mosquitto_pub -h "$MQTT_BROKER" -p 8883 -t pinbot/deploy -m manual \
  -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" --capath /etc/ssl/certs/
```

## Backing up mid-event

The database is one file and SQLite is running in WAL mode, so copy it with
SQLite rather than `cp`:

```bash
sqlite3 /var/lib/pinbot/pinball.db ".backup /home/$USER/pinball-backup.db"
```

Worth doing once a day during a multi-day event. `/hs` and the audit log can be
rebuilt from nothing but this file.

## When it doesn't work

**Bot won't start.** `journalctl -u pinbot -n 50`. Almost always the token:
`~/pinbot/.venv/bin/python -m bot.checkup` will say so without printing it.

**Deployer won't connect.** `journalctl -u pinbot-deployer -n 50`. It names the
missing variable if `deploy/.env.mqtt` is incomplete. Port must be 8883 — on 1883
the password crosses the internet in the clear.

**Push didn't deploy.** Check the Actions tab first: the `notify-pi` job is gated
on the tests, so a failing test suite stops the deployment before the Pi is ever
told about it. That's intended.

**Deployed, but the bot is running old code.** Look for `tests failed` in
`/var/log/pinbot/deploy.log` — the checkout was rolled back on purpose.
