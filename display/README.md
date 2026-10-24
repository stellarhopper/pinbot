# Venue scoreboard

The top 3 for every table on a TV at the venue, updating live while the bot runs
at home.

```
bot (Pi, home) ──TLS──▶ EMQX ◀──TLS── server.py (venue) ──LAN──▶ TV browser
```

**Nothing at the venue is reachable from the internet.** `server.py` makes one
outbound connection to the broker, like the Pi's deployer does. The only port it
opens is the web page, on the venue LAN.

- **Fast.** The bot checks for changes every second, so a new score reaches the
  TV in about a second.
- **Survives restarts.** The board is a *retained* MQTT message. A listener that
  starts, restarts, or drops off the Wi-Fi gets the current board as soon as it
  reconnects, even if the bot is down.
- **Avatars included.** Players' avatars come over the same connection, so the
  TV never loads anything from Discord.

## 1. A login for the display

Use the same EMQX deployment as the GitHub deploys, with **a new login of its
own**:

1. In the EMQX Cloud console, open the deployment, then **Access Control →
   Authentication**, and add a user.
2. Under **Authorization**, allow that username **Subscribe** on
   `pinbot/scoreboard/#` and nothing else. The deployment's deny-all rule
   refuses everything else (see [`deploy/README.md`](../deploy/README.md#the-broker)).

Don't reuse the Pi's login. That one can publish, and a laptop left at the venue
shouldn't be able to trigger a deploy or overwrite the board.

## 2. The server's ID

In Discord, go to **Settings → Advanced** and turn on **Developer Mode**. Then
right-click the server icon and choose **Copy Server ID**. That's
`SCOREBOARD_GUILD_ID`.

## 3. Run it

Copy `.env.example` to `.env` (or `display.env` for Docker), fill it in, and
`chmod 600` it.

**On a laptop or Pi** (only needs Python 3 and `paho-mqtt`):

```bash
pip install paho-mqtt        # or: sudo apt install python3-paho-mqtt
python3 display/server.py    # reads display/.env
```

**In a container:**

```bash
docker build -t pinbot-display display/
docker run -d --restart unless-stopped -p 8080:8080 \
    --env-file display.env --name pinbot-display pinbot-display
```

Then point the TV's browser at `http://<that machine>:8080/`. If it's the same
machine, use kiosk mode:

```bash
chromium --kiosk --noerrdialogs --disable-session-crashed-bubble http://localhost:8080/
```

## Reading the screen

- **Green dot, "live":** connected all the way to the broker.
- **Amber dot, "reconnecting…":** either the page lost the listener or the
  listener lost the broker. The last board stays up, and both reconnect on their
  own.
- **Waiting for scores…:** connected, but nothing has been published yet. Check
  that the bot is running and that `SCOREBOARD_GUILD_ID` is the right server.

## When it doesn't work

**`connection refused` in the listener's output.** The username or password is
wrong, or the user has no Subscribe rule for `pinbot/scoreboard/#`.

**Stuck on "connecting to …".** The venue network is probably blocking outbound
port 8883. Check from that machine:

```bash
nc -vz <broker host> 8883
```

If it's blocked, ask the venue to allow outbound 8883 to the broker host, or
tether the display machine to a phone.

**Connected, but no board.** Check with `mosquitto_sub` that the bot is
publishing:

```bash
mosquitto_sub -h <broker host> -p 8883 --capath /etc/ssl/certs/ \
    -u <display user> -P <password> -t 'pinbot/scoreboard/+' -v -C 1
```

If nothing shows up, check the Pi (see `deploy/README.md`):
`grep scoreboard /var/log/pinbot/error.log`.
