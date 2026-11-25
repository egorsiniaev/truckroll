#!/usr/bin/env bash
# Copy the repo to the server and rebuild. Never copies .env or local data.
set -euo pipefail
HOST="${TRUCKROLL_SERVER:-root@142.132.244.71}"
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .git --exclude '.env*' --exclude .venv --exclude node_modules --exclude .pio \
  --exclude __pycache__ --exclude .pytest_cache --exclude observability/data ./ "$HOST:/opt/truckroll/"
ssh "$HOST" 'cd /opt/truckroll && docker compose -f compose.yaml -f compose.server.yaml up -d --build --remove-orphans'
