#!/usr/bin/env bash
# Pull the latest main, install it, run the tests, and restart the bot.
#
# Invoked by the MQTT listener on every push to main, and safe to run by hand.
#
# The one rule: never restart the bot onto code that fails its tests. A
# tournament is live for a weekend and a bad push at the wrong moment costs
# real scores, so a failing test set rolls the checkout back to the commit
# that was running and leaves the process untouched.

set -euo pipefail

DEPLOY_DIR="${PINBOT_DIR:-/home/stellarhopper/pinbot}"
REPO_URL="${PINBOT_REPO:-https://github.com/stellarhopper/pinbot.git}"
BRANCH="${PINBOT_BRANCH:-main}"
SERVICE="${PINBOT_SERVICE:-pinbot}"
LOG_DIR="${PINBOT_LOG_DIR:-/var/log/pinbot}"
LOG_FILE="$LOG_DIR/deploy.log"
VENV="$DEPLOY_DIR/.venv"

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

fail() {
    log "DEPLOY FAILED: $*"
    exit 1
}

log "=== deployment starting ==="

# --- fetch -----------------------------------------------------------------

if [ ! -d "$DEPLOY_DIR/.git" ]; then
    log "cloning $REPO_URL into $DEPLOY_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$DEPLOY_DIR" || fail "clone failed"
    PREVIOUS=""
else
    cd "$DEPLOY_DIR"
    PREVIOUS="$(git rev-parse HEAD)"
    log "currently at ${PREVIOUS:0:12}"
    git fetch origin "$BRANCH" || fail "fetch failed (network? deploy key?)"
    # Hard reset rather than pull: the Pi is a mirror of main, not somewhere
    # anyone edits. The database and .env are gitignored, so they survive.
    git reset --hard "origin/$BRANCH" || fail "reset failed"
fi

cd "$DEPLOY_DIR"
CURRENT="$(git rev-parse HEAD)"
log "now at ${CURRENT:0:12} — $(git log -1 --pretty=%s)"

if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" = "$CURRENT" ]; then
    log "already up to date; reinstalling and restarting anyway"
fi

# --- roll back to the code that was running if anything below fails ---------

rollback() {
    if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$CURRENT" ]; then
        log "rolling the checkout back to ${PREVIOUS:0:12}"
        git -C "$DEPLOY_DIR" reset --hard "$PREVIOUS" >>"$LOG_FILE" 2>&1 || true
    fi
}

# --- install ---------------------------------------------------------------

if [ ! -x "$VENV/bin/python" ]; then
    log "creating venv at $VENV"
    python3 -m venv "$VENV" || fail "could not create the venv"
fi

log "installing dependencies"
if ! "$VENV/bin/pip" install --quiet --upgrade pip >>"$LOG_FILE" 2>&1; then
    log "warning: could not upgrade pip, continuing"
fi
if ! "$VENV/bin/pip" install --quiet -e ".[dev,vision]" >>"$LOG_FILE" 2>&1; then
    rollback
    fail "dependency install failed (see $LOG_FILE)"
fi

# --- verify ----------------------------------------------------------------

log "running the test suite"
if ! "$VENV/bin/python" -m pytest -q >>"$LOG_FILE" 2>&1; then
    rollback
    fail "tests failed — the bot was NOT restarted and is still running ${PREVIOUS:0:12}"
fi
log "tests passed"

# --- restart ---------------------------------------------------------------

log "restarting $SERVICE"
sudo systemctl restart "$SERVICE" || fail "systemctl restart failed"

# Give it a moment to either come up or crash on startup — a token error or a
# bad migration shows up within a couple of seconds, and reporting "deployed"
# for a process that died immediately is worse than reporting nothing.
sleep 5
if sudo systemctl is-active --quiet "$SERVICE"; then
    log "$SERVICE is running ${CURRENT:0:12}"
else
    log "$SERVICE is NOT running after restart — check: journalctl -u $SERVICE -n 50"
    exit 1
fi

log "=== deployment complete ==="
