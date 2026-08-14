"""Process-level configuration.

Only three things live here: the bot token, an optional dev guild for fast
command sync, and the database path. Everything a tournament organizer might
want to change — tables, channel, admin roles, tournament state — lives in the
database per guild and is set through slash commands, so the bot can be added
to a server and run without anyone editing files on disk.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> None:
    """Populate os.environ from a .env file.

    Real environment variables always win, so a systemd unit's Environment=
    lines override the file rather than the other way around.
    """
    path = path or ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


class Config:
    """Snapshot of the environment, read once at startup."""

    def __init__(self) -> None:
        self.token = os.environ.get("DISCORD_TOKEN", "").strip()
        self.dev_guild_id = _optional_int("DEV_GUILD_ID")
        raw_db = os.environ.get("DB_PATH", "").strip()
        self.db_path = Path(raw_db) if raw_db else ROOT / "data" / "pinball.db"
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None

    def require_token(self) -> str:
        if not self.token:
            raise SystemExit(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
            )
        return self.token
