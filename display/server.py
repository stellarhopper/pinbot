#!/usr/bin/env python3
"""The venue end of the live scoreboard.

Subscribes to the bot's retained scoreboard messages on MQTT and serves a page
for the TV that updates the moment they change:

    bot (home) ──TLS──▶ broker ◀──TLS── this (venue) ──LAN──▶ TV browser

Both MQTT connections are outbound, so the venue needs no port open to the
internet; the only listening socket is this web server, on the LAN.

One file, standard library plus paho-mqtt, so it runs on whatever laptop or Pi
is plugged into the TV — or in the container described in the Dockerfile.

    MQTT_BROKER=... MQTT_USERNAME=... MQTT_PASSWORD=... \\
    SCOREBOARD_GUILD_ID=... python3 server.py
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
# How often an idle event stream sends a comment line. Keeps proxies and the
# browser from deciding the connection is dead during a quiet stretch.
HEARTBEAT_SECONDS = 15


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def load_env_file(path: Path) -> None:
    """KEY=value lines into os.environ; real environment variables win."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if key and value and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------- state


class Board:
    """The latest snapshot and avatars, shared between paho and the web server.

    ``version`` goes up on every change; event streams wait on the condition
    for it to move past the one they last sent.
    """

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.version = 0
        self.snapshot: dict | None = None
        self.avatars: dict[int, bytes] = {}
        self.connected = False
        self.received_at: float | None = None

    def _bump(self) -> None:
        self.version += 1
        self.cond.notify_all()

    def set_snapshot(self, snapshot: dict) -> None:
        with self.cond:
            self.snapshot = snapshot
            self.received_at = time.time()
            self._bump()

    def set_avatar(self, user_id: int, image: bytes) -> None:
        with self.cond:
            self.avatars[user_id] = image
        # No bump: the snapshot that names this avatar arrives right after it,
        # and carries the key the page uses to bust its cache.

    def set_connected(self, connected: bool) -> None:
        with self.cond:
            if self.connected != connected:
                self.connected = connected
                self._bump()

    def state(self) -> dict:
        return {
            "connected": self.connected,
            "received_at": self.received_at,
            "board": self.snapshot,
        }


# ----------------------------------------------------------------------- mqtt


def start_mqtt(board: Board, broker: str, port: int, username: str, password: str,
               topic: str) -> None:
    import paho.mqtt.client as mqtt

    client_id = f"pinbot-display-{os.getpid()}"
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client(client_id=client_id)
    client.username_pw_set(username, password)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    avatar_prefix = f"{topic}/avatar/"

    def on_connect(client, _userdata, _flags, rc, *_args) -> None:
        if rc == 0:
            log(f"connected to {broker}:{port}")
            # Retained, so both arrive straight away with the current state.
            client.subscribe([(topic, 1), (avatar_prefix + "+", 1)])
            log(f"subscribed to {topic} and its avatars")
            board.set_connected(True)
        else:
            log(f"connection refused ({rc}) — check the username and password")

    def on_disconnect(_client, _userdata, *args) -> None:
        # paho 2.x passes (flags, reason, properties); 1.x passes (rc,).
        reason = args[1] if len(args) >= 2 else (args[0] if args else "?")
        log(f"disconnected ({reason}) — reconnecting")
        board.set_connected(False)

    def on_message(_client, _userdata, msg) -> None:
        try:
            if msg.topic == topic:
                board.set_snapshot(json.loads(msg.payload))
            elif msg.topic.startswith(avatar_prefix):
                board.set_avatar(int(msg.topic[len(avatar_prefix):]), bytes(msg.payload))
        except (ValueError, TypeError) as exc:
            log(f"ignored a bad message on {msg.topic}: {exc}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    log(f"connecting to {broker}:{port}...")
    client.connect_async(broker, port, keepalive=30)
    client.loop_start()


# ------------------------------------------------------------------------ web


def image_type(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "application/octet-stream"


def make_handler(board: Board):
    class Handler(BaseHTTPRequestHandler):
        # Keep the console for connection events, not a line per avatar.
        def log_message(self, *_args) -> None:
            pass

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.read_bytes(),
                           cache="no-cache")
            elif path == "/state":
                with board.cond:
                    body = json.dumps(board.state()).encode()
                self._send(200, "application/json", body, cache="no-store")
            elif path == "/events":
                self._events()
            elif path.startswith("/avatar/"):
                self._avatar(path[len("/avatar/"):])
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, status: int, ctype: str, body: bytes, *, cache: str | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _avatar(self, raw_id: str) -> None:
            try:
                image = board.avatars.get(int(raw_id))
            except ValueError:
                image = None
            if image is None:
                self._send(404, "text/plain", b"no avatar")
                return
            # The page asks with ?k=<avatar key>, so a new picture is a new URL
            # and the old one can be cached for as long as the browser likes.
            self._send(200, image_type(image), image, cache="max-age=86400")

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            sent = -1
            try:
                while True:
                    with board.cond:
                        board.cond.wait_for(
                            lambda: board.version != sent, timeout=HEARTBEAT_SECONDS
                        )
                        changed = board.version != sent
                        sent = board.version
                        body = json.dumps(board.state()) if changed else None
                    if body is None:
                        self.wfile.write(b": still here\n\n")
                    else:
                        self.wfile.write(f"data: {body}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # the TV reloaded or went away

    return Handler


# ----------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="Live pinball scoreboard for a TV.")
    parser.add_argument("--host", default=os.environ.get("DISPLAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("DISPLAY_PORT", "8080")))
    parser.add_argument("--env-file", type=Path, default=HERE / ".env",
                        help="KEY=value settings; real environment variables win")
    args = parser.parse_args()

    load_env_file(args.env_file)
    broker = os.environ.get("MQTT_BROKER", "").strip()
    username = os.environ.get("MQTT_USERNAME", "").strip()
    password = os.environ.get("MQTT_PASSWORD", "").strip()
    guild = os.environ.get("SCOREBOARD_GUILD_ID", "").strip()
    port = int(os.environ.get("MQTT_PORT", "8883"))
    base = os.environ.get("SCOREBOARD_TOPIC", "pinbot/scoreboard").strip().rstrip("/")

    missing = [
        name
        for name, value in (
            ("MQTT_BROKER", broker),
            ("MQTT_USERNAME", username),
            ("MQTT_PASSWORD", password),
            ("SCOREBOARD_GUILD_ID", guild),
        )
        if not value
    ]
    if missing:
        where = "the environment"
        if args.env_file != Path(os.devnull):
            where += f" or {args.env_file}"
        log(f"missing {', '.join(missing)} — set them in {where}")
        sys.exit(1)
    if not guild.isdigit():
        log(f"SCOREBOARD_GUILD_ID must be the server's numeric ID, got {guild!r}")
        sys.exit(1)

    board = Board()
    start_mqtt(board, broker, port, username, password, f"{base}/{guild}")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(board))
    server.daemon_threads = True
    shown = "localhost" if args.host in ("0.0.0.0", "") else args.host
    log(f"serving the scoreboard on http://{shown}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
