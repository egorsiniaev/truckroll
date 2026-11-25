#!/usr/bin/env bash
# Copy the repo to the server and restart only what changed. Never copies .env or local data.
# Builds only our own services, without attestations (they change the image ID on every build and force a restart):
# rebuilding the simulator would restart it and cut every charging session.
set -euo pipefail
HOST="${TRUCKROLL_SERVER:-root@142.132.244.71}"
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .git --exclude '.env*' --exclude .venv --exclude node_modules --exclude .pio \
  --exclude __pycache__ --exclude .pytest_cache --exclude observability/data ./ "$HOST:/opt/truckroll/"
ssh "$HOST" 'cd /opt/truckroll && C="docker compose -f compose.yaml -f compose.server.yaml" &&
  BUILDX_NO_DEFAULT_ATTESTATIONS=1 $C build control csms && $C up -d --remove-orphans &&
  $C exec -T caddy caddy reload --config /etc/caddy/Caddyfile'
