#!/usr/bin/env python3
"""Listens for a deploy trigger on MQTT and runs deploy.sh.

Why MQTT rather than a webhook or SSH: the Pi opens one outbound TLS
connection to the broker and keeps it. Nothing has to be reachable from the
internet, no port is forwarded, and the Pi can sit behind CGNAT or move
networks without anything being reconfigured.

Runs on the system Python (paho-mqtt from apt), not the bot's venv, so a
broken deployment can never take the deployer down with it — the thing that
fixes the bot must not share the bot's dependencies.
"""

from __future__ import annotations

import os
import ssl
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

BROKER = os.environ.get("MQTT_BROKER", "")
PORT = int(os.environ.get("MQTT_PORT", "8883"))
USERNAME = os.environ.get("MQTT_USERNAME", "")
PASSWORD = os.environ.get("MQTT_PASSWORD", "")
TOPIC = os.environ.get("MQTT_TOPIC", "pinbot/deploy")

DEPLOY_SCRIPT = Path(__file__).resolve().parent / "deploy.sh"
DEPLOY_TIMEOUT = 600  # generous: a cold venv build on a Pi is not quick


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def on_connect(client, userdata, flags, rc, *args) -> None:
    # *args absorbs the `properties` argument paho 2.x passes; the same
    # callback then works under both major versions.
    if rc == 0:
        log(f"connected to {BROKER}:{PORT}")
        client.subscribe(TOPIC)
        log(f"subscribed to {TOPIC}")
    else:
        log(f"connection refused (code {rc})")


def on_message(client, userdata, msg) -> None:
    payload = msg.payload.decode(errors="replace").strip()
    log(f"trigger received: {payload or '(empty)'}")
    run_deploy()


def run_deploy() -> None:
    try:
        result = subprocess.run(
            ["bash", str(DEPLOY_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=DEPLOY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log(f"deployment timed out after {DEPLOY_TIMEOUT}s — bot left as it was")
        return
    except Exception as exc:  # noqa: BLE001 - the listener must never die
        log(f"could not run the deploy script: {exc}")
        return

    # Always print the script's own log: when a deployment fails, this output
    # is the only record of why, and it is what `journalctl -u pinbot-deployer`
    # will be read for.
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.returncode == 0:
        log("deployment succeeded")
    else:
        log(f"deployment FAILED (exit {result.returncode})")
        if result.stderr:
            print(result.stderr, end="", flush=True)


def on_disconnect(client, userdata, rc, *args) -> None:
    if rc != 0:
        log(f"unexpected disconnect (code {rc}) — paho will reconnect")


def build_client() -> mqtt.Client:
    """A client that works on both paho-mqtt 1.x and 2.x.

    Debian stable ships 1.6, trixie and pip ship 2.x, and 2.x refuses to
    construct a client without being told which callback API you want.
    """
    try:
        return mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1, client_id="pinbot-pi-deployer"
        )
    except AttributeError:
        return mqtt.Client(client_id="pinbot-pi-deployer")


def main() -> None:
    missing = [
        name
        for name, value in (
            ("MQTT_BROKER", BROKER),
            ("MQTT_USERNAME", USERNAME),
            ("MQTT_PASSWORD", PASSWORD),
        )
        if not value
    ]
    if missing:
        log(f"missing {', '.join(missing)} — check deploy/.env.mqtt")
        sys.exit(1)
    if not DEPLOY_SCRIPT.is_file():
        log(f"no deploy script at {DEPLOY_SCRIPT}")
        sys.exit(1)

    client = build_client()
    client.username_pw_set(USERNAME, PASSWORD)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    log(f"connecting to {BROKER}:{PORT}...")
    try:
        client.connect(BROKER, PORT, keepalive=60)
        # loop_forever reconnects on its own, which is the whole point of
        # running this as a long-lived service.
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        log("shutting down")
        client.disconnect()
    except Exception as exc:  # noqa: BLE001
        log(f"fatal: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
