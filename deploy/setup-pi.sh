#!/usr/bin/env bash
# One-time setup for a Raspberry Pi. Run it as your normal user, not root:
#
#   bash deploy/setup-pi.sh
#
# Safe to re-run: every step checks before it acts.

set -euo pipefail

RUN_USER="${SUDO_USER:-$USER}"
DEPLOY_DIR="${PINBOT_DIR:-/home/$RUN_USER/pinbot}"
REPO_URL="${PINBOT_REPO:-https://github.com/stellarhopper/pinbot.git}"
LOG_DIR=/var/log/pinbot
UNIT_DIR=/etc/systemd/system

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  ✓ %s\n' "$*"; }

if [ "$EUID" -eq 0 ]; then
    echo "Run this as your normal user — it will ask for sudo when it needs it."
    exit 1
fi

say "Discord Pinball Bot — Raspberry Pi setup"
echo "user:      $RUN_USER"
echo "directory: $DEPLOY_DIR"

# --- 1. system packages ----------------------------------------------------

say "[1/7] System packages"
sudo apt-get update -qq
# python3-venv for the bot's virtualenv, python3-paho-mqtt for the deployment
# listener (which deliberately runs outside that venv), git to fetch code.
sudo apt-get install -y -qq python3 python3-venv python3-pip python3-paho-mqtt git
ok "installed"

# --- 2. log directory ------------------------------------------------------

say "[2/7] Log directory"
sudo mkdir -p "$LOG_DIR"
sudo chown "$RUN_USER:$RUN_USER" "$LOG_DIR"
ok "$LOG_DIR"

# --- 3. the code -----------------------------------------------------------

say "[3/7] Repository"
if [ ! -d "$DEPLOY_DIR/.git" ]; then
    git clone "$REPO_URL" "$DEPLOY_DIR"
    ok "cloned"
else
    git -C "$DEPLOY_DIR" pull --ff-only
    ok "up to date"
fi

# --- 4. virtualenv ---------------------------------------------------------

say "[4/7] Python environment"
if [ ! -x "$DEPLOY_DIR/.venv/bin/python" ]; then
    python3 -m venv "$DEPLOY_DIR/.venv"
fi
"$DEPLOY_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DEPLOY_DIR/.venv/bin/pip" install --quiet -e "$DEPLOY_DIR[dev]"
ok "installed into $DEPLOY_DIR/.venv"

# --- 5. secrets ------------------------------------------------------------

say "[5/7] Credentials"

if [ ! -f "$DEPLOY_DIR/.env" ]; then
    read -rsp "  Discord bot token (paste, it won't echo): " token
    echo
    printf 'DISCORD_TOKEN=%s\n' "$token" > "$DEPLOY_DIR/.env"
    chmod 600 "$DEPLOY_DIR/.env"
    unset token
    ok "wrote .env"
else
    ok ".env already exists, leaving it alone"
fi

if [ ! -f "$DEPLOY_DIR/deploy/.env.mqtt" ]; then
    echo "  MQTT broker details (from your HiveMQ / Mosquitto instance):"
    read -rp  "    broker host: " mqtt_broker
    read -rp  "    username:    " mqtt_user
    read -rsp "    password:    " mqtt_pass
    echo
    cat > "$DEPLOY_DIR/deploy/.env.mqtt" <<EOF
MQTT_BROKER=$mqtt_broker
MQTT_PORT=8883
MQTT_USERNAME=$mqtt_user
MQTT_PASSWORD=$mqtt_pass
MQTT_TOPIC=pinbot/deploy
EOF
    chmod 600 "$DEPLOY_DIR/deploy/.env.mqtt"
    unset mqtt_pass
    ok "wrote deploy/.env.mqtt"
else
    ok "deploy/.env.mqtt already exists, leaving it alone"
fi

chmod +x "$DEPLOY_DIR/deploy/deploy.sh" "$DEPLOY_DIR/deploy/mqtt_subscriber.py"

# --- 6. systemd ------------------------------------------------------------

say "[6/7] systemd units"

# The units are written for user `stellarhopper` in /home/stellarhopper/pinbot.
# Rewrite both if this Pi uses anything else, so the same files work anywhere.
install_unit() {
    local name="$1"
    sed -e "s|/home/stellarhopper/pinbot|$DEPLOY_DIR|g" \
        -e "s|^User=stellarhopper$|User=$RUN_USER|" \
        "$DEPLOY_DIR/deploy/$name" | sudo tee "$UNIT_DIR/$name" >/dev/null
    ok "$name"
}

install_unit pinbot.service
install_unit pinbot-deployer.service

sed "s|^stellarhopper |$RUN_USER |" "$DEPLOY_DIR/deploy/pinbot-sudoers" \
    | sudo tee /etc/sudoers.d/pinbot >/dev/null
sudo chmod 440 /etc/sudoers.d/pinbot
# A malformed sudoers file locks you out of sudo entirely, so check it and
# remove it if it doesn't parse rather than leaving it in place.
if ! sudo visudo -cf /etc/sudoers.d/pinbot >/dev/null; then
    sudo rm -f /etc/sudoers.d/pinbot
    echo "  ✗ sudoers file was invalid and has been removed — deployment will"
    echo "    not be able to restart the bot until this is fixed."
    exit 1
fi
ok "sudoers drop-in"

sudo systemctl daemon-reload
sudo systemctl enable pinbot pinbot-deployer >/dev/null
ok "enabled at boot"

# --- 7. go -----------------------------------------------------------------

say "[7/7] Starting"
sudo systemctl restart pinbot pinbot-deployer
sleep 3
sudo systemctl is-active --quiet pinbot \
    && ok "pinbot running" \
    || echo "  ✗ pinbot did not start — journalctl -u pinbot -n 50"
sudo systemctl is-active --quiet pinbot-deployer \
    && ok "pinbot-deployer running" \
    || echo "  ✗ pinbot-deployer did not start — journalctl -u pinbot-deployer -n 50"

say "Done"
cat <<EOF
Verify the Discord side:   $DEPLOY_DIR/.venv/bin/python -m bot.checkup
Follow the bot:            journalctl -u pinbot -f
Follow deployments:        journalctl -u pinbot-deployer -f

Add these to the GitHub repo (Settings → Secrets and variables → Actions):
  MQTT_BROKER, MQTT_USERNAME, MQTT_PASSWORD

After that, every push to main deploys itself here.
EOF
