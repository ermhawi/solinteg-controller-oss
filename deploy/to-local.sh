#!/usr/bin/env bash
# Deploys the current working tree to a LOCAL /opt/solinteg install and restarts the services.
# Local sibling of to-nuc.sh — same stage-build-swap-restart discipline, no SSH: use this when
# the app runs as systemd services on THIS machine (e.g. the k3s node exposing it at
# ermhawi.com/solinteg). Needs sudo (passwordless assumed on this box).
#
# Usage:  ./deploy/to-local.sh
#
# What it does: rsyncs the working tree into a persistent staging dir (<app>-staging, so
# node_modules/.next caches carry over), builds there as the `solinteg` user with solinteg.env
# sourced (pricing/site/basePath constants are baked at build time), and only on a successful
# build swaps the staged tree into /opt/solinteg/app and restarts solinteg-web + solinteg-dispatch.
# A broken build never touches the running deployment.
set -euo pipefail

APP_DIR="${SOLINTEG_APP_DIR:-/opt/solinteg/app}"
ENV_FILE="${SOLINTEG_ENV_FILE:-/opt/solinteg/solinteg.env}"
STAGING_DIR="${APP_DIR}-staging"
# Node is installed system-wide from NodeSource so deploys and systemd use the same runtime.
# npm's launcher resolves `node` via `env node`, so keep /usr/bin explicit for the solinteg user.
NODE_BIN="${SOLINTEG_NODE_BIN:-/usr/bin}"
PORT="${SOLINTEG_WEB_PORT:-3007}"
BASE_PATH="${NEXT_BASE_PATH:-/solinteg}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -x "${NODE_BIN}/node" ]]; then
  echo "ERROR: Node.js executable not found at ${NODE_BIN}/node" >&2
  exit 1
fi
if [[ ! -x "${NODE_BIN}/npm" ]]; then
  echo "ERROR: npm executable not found at ${NODE_BIN}/npm" >&2
  exit 1
fi

echo "==> Staging working tree into ${STAGING_DIR} (caches persist for fast rebuilds)..."
sudo mkdir -p "$STAGING_DIR"
# --delete keeps staging a faithful mirror of the working tree, but node_modules/.next/.venv are
# excluded from the comparison so their caches survive between deploys.
sudo rsync -a --delete \
  --exclude 'node_modules' --exclude '.next' --exclude '.venv' --exclude '.git' \
  --exclude '.env.local' --exclude '.env' --exclude '*.db' --exclude '*.db-wal' \
  --exclude '*.db-shm' --exclude 'live.json' --exclude 'coverage' \
  --exclude '*.tsbuildinfo' --exclude 'next-env.d.ts' \
  "$REPO_ROOT/" "$STAGING_DIR/"
sudo chown -R solinteg:solinteg "$STAGING_DIR"

echo "==> Building in staging as solinteg (solinteg.env sourced — bakes basePath/site constants)..."
sudo -u solinteg env HOME=/opt/solinteg PATH="${NODE_BIN}:/usr/bin:/bin" bash -lc \
  "cd '${STAGING_DIR}' && set -a && source '${ENV_FILE}' && set +a && npm ci && npm run build"

echo "==> Build OK — swapping staged tree into ${APP_DIR} (.venv is live-only, preserved)..."
sudo rsync -a --delete --exclude '.venv' "$STAGING_DIR/" "$APP_DIR/"
sudo chown -R solinteg:solinteg "$APP_DIR"

echo "==> Restarting services..."
sudo systemctl restart solinteg-web
sleep 2
sudo systemctl is-active solinteg-web
# dispatch_loop.py is a long-running process, not hot-reloaded by the rsync — restart on changes.
sudo systemctl restart solinteg-dispatch
sleep 2
sudo systemctl is-active solinteg-dispatch

echo "==> Verifying..."
curl --fail --silent --show-error \
  -o /dev/null \
  -w "local curl ${BASE_PATH}: %{http_code}\n" \
  "http://localhost:${PORT}${BASE_PATH}"
echo "==> Done."
