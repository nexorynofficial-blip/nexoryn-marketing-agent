#!/usr/bin/env bash
# Runs ON the VM (as root/sudo) to install everything the agent needs and
# wire up systemd + Caddy + logrotate. Safe to re-run -- every step checks
# whether it's already done before doing it again.
#
# Assumes deploy_to_vm.sh (or you, manually) has already copied this
# project's files to /opt/nexoryn-agent, including the systemd/ and
# caddy/ subdirectories -- this script installs system dependencies and
# copies THOSE checked-in config files into their real system locations,
# rather than duplicating their content here.
#
# Usage (as root):
#   bash /opt/nexoryn-agent/scripts/setup_vm.sh
set -euo pipefail

PROJECT_DIR="/opt/nexoryn-agent"
LOG_DIR="/var/log/nexoryn-agent"
SERVICE_USER="nexoryn"

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (e.g. with sudo)." >&2
  exit 1
fi

if [[ ! -d "$PROJECT_DIR/app" ]]; then
  echo "Expected the project at $PROJECT_DIR (with an app/ directory) but didn't find it." >&2
  echo "Copy the project there first (see scripts/deploy_to_vm.sh), then re-run this script." >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
# Ubuntu 24.04 LTS ships Python 3.12 by default, which already satisfies
# this project's 3.11+ requirement -- no need for a separate PPA.
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  git curl rsync \
  debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Installing Caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y caddy
else
  echo "==> Caddy already installed, skipping"
fi

echo "==> Creating service user '$SERVICE_USER'"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PROJECT_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
else
  echo "    user already exists, skipping"
fi

echo "==> Setting up directories"
mkdir -p "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR" "$LOG_DIR"

echo "==> Creating Python virtualenv and installing dependencies"
if [[ ! -d "$PROJECT_DIR/.venv" ]]; then
  sudo -u "$SERVICE_USER" python3 -m venv "$PROJECT_DIR/.venv"
fi
sudo -u "$SERVICE_USER" "$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "==> No .env found -- copying .env.example as a starting point"
  echo "    You MUST edit $PROJECT_DIR/.env with real values before the service will start."
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR/.env"
  chmod 600 "$PROJECT_DIR/.env"
else
  echo "==> .env already present, leaving it alone"
  chmod 600 "$PROJECT_DIR/.env"
fi

echo "==> Installing logrotate config"
cat > /etc/logrotate.d/nexoryn-agent <<'EOF'
/var/log/nexoryn-agent/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    # The agent (and Caddy) keep an open file handle on these logs for
    # their whole run; copytruncate avoids needing either process to
    # reopen the file after rotation.
    copytruncate
}
EOF

echo "==> Installing systemd service"
cp "$PROJECT_DIR/systemd/nexoryn-agent.service" /etc/systemd/system/nexoryn-agent.service
systemctl daemon-reload
systemctl enable nexoryn-agent

echo "==> Installing Caddy config"
cp "$PROJECT_DIR/caddy/Caddyfile" /etc/caddy/Caddyfile
systemctl enable caddy
systemctl reload caddy || systemctl restart caddy

echo
echo "==> Setup complete."
echo "    Next: edit $PROJECT_DIR/.env with real credentials (if not already done),"
echo "    then: systemctl start nexoryn-agent"
echo "    then: systemctl status nexoryn-agent"
