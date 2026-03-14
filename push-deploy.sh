#!/bin/bash
# ── Config ────────────────────────────────────────────────────────────────────
SSH_USER="administrator"
SSH_HOST="38.247.136.208"
VPS_PATH="/var/www/verificador"
APP_PORT="5001"
# ─────────────────────────────────────────────────────────────────────────────

set -e

echo "==> Pushing to git remote..."
git push

echo "==> Deploying to ${SSH_USER}@${SSH_HOST}:${VPS_PATH}..."
ssh "${SSH_USER}@${SSH_HOST}" bash <<EOF
  set -e
  cd "${VPS_PATH}"
  echo "  -> git pull"
  git pull
  echo "  -> installing dependencies"
  pip3 install -r requirements.txt --quiet
  echo "  -> restarting app"
  mkdir -p logs
  pkill -f "python3 run_web.py" || true
  sleep 1
  nohup python3 run_web.py --port ${APP_PORT} > logs/web.log 2>&1 &
  echo "  -> done"
EOF

echo ""
echo "Deploy complete — http://${SSH_HOST}:${APP_PORT}"
