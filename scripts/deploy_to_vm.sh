#!/usr/bin/env bash
# Runs on YOUR laptop. Copies the project to the VM, uploads .env (with an
# explicit confirmation first, since it contains real secrets), runs
# setup_vm.sh remotely, then starts and verifies the service.
#
# Usage:
#   bash scripts/deploy_to_vm.sh <vm_ip_or_hostname> <path_to_ssh_key> <path_to_env_file> [--with-db]
#
# <vm_ip_or_hostname> can be a bare host ("1.2.3.4") -- assumed to be the
# "ubuntu" user, Oracle's default Ubuntu cloud-image login -- or a full
# "user@host" if your VM uses a different username.
#
# --with-db additionally uploads your local nexoryn_agent.db (asks for
# separate confirmation, since it's real data, not just app code).
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: bash scripts/deploy_to_vm.sh <vm_ip_or_hostname> <path_to_ssh_key> <path_to_env_file> [--with-db]" >&2
  exit 1
fi

VM_HOST_ARG="$1"
SSH_KEY="$2"
ENV_FILE="$3"
WITH_DB="${4:-}"

if [[ "$VM_HOST_ARG" == *"@"* ]]; then
  VM_TARGET="$VM_HOST_ARG"
else
  VM_TARGET="ubuntu@$VM_HOST_ARG"
fi

REMOTE_DIR="/opt/nexoryn-agent"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "SSH key not found: $SSH_KEY" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo ".env file not found: $ENV_FILE" >&2
  exit 1
fi

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new $VM_TARGET"

echo "==> Deploying to $VM_TARGET"
echo "    (checking SSH connectivity first)"
$SSH "echo connected" >/dev/null

echo "==> Making sure $REMOTE_DIR exists"
$SSH "sudo mkdir -p $REMOTE_DIR && sudo chown \$(whoami) $REMOTE_DIR"

echo "==> Copying project files (excluding .venv, .git, __pycache__, local DB)"
if command -v rsync >/dev/null 2>&1; then
  rsync -avz --delete \
    --exclude='.venv/' --exclude='.git/' --exclude='__pycache__/' \
    --exclude='*.pyc' --exclude='nexoryn_agent.db' --exclude='.env' \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$PROJECT_ROOT/" "$VM_TARGET:$REMOTE_DIR/"
else
  echo "    rsync not found -- falling back to a tarball over scp"
  TARBALL="$(mktemp -u).tar.gz"
  tar -czf "$TARBALL" \
    --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='nexoryn_agent.db' --exclude='.env' \
    -C "$PROJECT_ROOT" .
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$TARBALL" "$VM_TARGET:/tmp/nexoryn-agent-deploy.tar.gz"
  $SSH "tar -xzf /tmp/nexoryn-agent-deploy.tar.gz -C $REMOTE_DIR && rm /tmp/nexoryn-agent-deploy.tar.gz"
  rm -f "$TARBALL"
fi

echo
echo "==> About to upload your .env file ($ENV_FILE) to the VM."
echo "    This file contains real secrets (Meta/Slack/Anthropic credentials)."
echo "    It will be transferred over SSH (encrypted in transit) and saved at"
echo "    $REMOTE_DIR/.env on the VM."
read -r -p "    Continue? [y/N] " confirm_env
if [[ "$confirm_env" =~ ^[Yy]$ ]]; then
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$ENV_FILE" "$VM_TARGET:$REMOTE_DIR/.env"
  $SSH "chmod 600 $REMOTE_DIR/.env"
  echo "    .env uploaded."
else
  echo "    Skipped -- make sure a valid .env already exists at $REMOTE_DIR/.env on the VM."
fi

if [[ "$WITH_DB" == "--with-db" ]]; then
  LOCAL_DB="$PROJECT_ROOT/nexoryn_agent.db"
  if [[ -f "$LOCAL_DB" ]]; then
    echo
    echo "==> About to upload your local database ($LOCAL_DB) to the VM,"
    echo "    overwriting anything already at $REMOTE_DIR/nexoryn_agent.db."
    read -r -p "    Continue? [y/N] " confirm_db
    if [[ "$confirm_db" =~ ^[Yy]$ ]]; then
      scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$LOCAL_DB" "$VM_TARGET:$REMOTE_DIR/nexoryn_agent.db"
      echo "    Database uploaded."
    else
      echo "    Skipped -- the VM will start with a fresh (empty) database."
    fi
  else
    echo "    --with-db was passed but $LOCAL_DB doesn't exist locally -- skipping."
  fi
fi

echo
echo "==> Running setup_vm.sh on the VM (installs deps, systemd, Caddy -- needs sudo)"
$SSH -t "sudo bash $REMOTE_DIR/scripts/setup_vm.sh"

echo
echo "==> Starting the service"
$SSH "sudo systemctl start nexoryn-agent"

sleep 2

echo
echo "==> Status:"
$SSH "sudo systemctl status nexoryn-agent --no-pager" || true

echo
echo "Done. Useful next steps:"
echo "  Tail logs:      ssh -i $SSH_KEY $VM_TARGET \"sudo journalctl -u nexoryn-agent -f\""
echo "  Health check:   curl https://agent.nexoryn.tech/health"
