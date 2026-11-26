#!/usr/bin/env bash
# Cadence — one-command install (run from inside the cloned repo).
#
#   git clone https://github.com/cecildwarf/cadence.git
#   cd cadence
#   ./install.sh
#
# That's it. This script only runs the same three commands the
# README lists, with checks and a clear error at each step instead
# of a silent failure:
#   1. checks Docker + Docker Compose are installed
#   2. makes ./data owned by the container user (PUID:PGID) — see
#      ensure_data_dir below; without this the app can't write its
#      logs and the container restart-loops on "Permission denied"
#   3. docker compose up -d --build
#   4. waits until the app answers on its port, then prints the URL
#
# No .env file is needed — every setting has a working default and
# the first-run wizard in the web UI asks for your folders. If you
# do use a .env file (to change the port, media folder, or
# timezone), its values are honoured here too.
# Idempotent: safe to run again — `docker compose up` just makes
# sure the container is running with the current code.
set -euo pipefail
cd "$(dirname "$0")"

# The container runs as PUID:PGID (default 1000:1000), but Docker
# creates a missing bind-mount folder (./data) as root on first
# `docker compose up` — then the app cannot write /app/data/logs
# and the container restart-loops with
# "Permission denied: '/app/data/logs'". Make ./data exist and
# owned by the container user *before* compose runs. Idempotent:
# does nothing when ownership is already right, and never touches
# the contents of ./data. If the container is already
# restart-looping on this error, fixing the ownership is enough —
# the restart policy retries it and the next attempt succeeds.
ensure_data_dir() {
    local puid pgid owner
    puid="${PUID:-}"
    pgid="${PGID:-}"
    if [ -f .env ]; then
        # .env values, like compose's: shell env wins over .env.
        if [ -z "$puid" ]; then
            puid="$(grep -E '^PUID=' .env | tail -n 1 | cut -d= -f2- | tr -d ' "')" || true
        fi
        if [ -z "$pgid" ]; then
            pgid="$(grep -E '^PGID=' .env | tail -n 1 | cut -d= -f2- | tr -d ' "')" || true
        fi
    fi
    puid="${puid:-1000}"
    pgid="${pgid:-1000}"
    mkdir -p ./data
    owner="$(stat -c '%u:%g' ./data 2>/dev/null || echo "?")"
    if [ "$owner" = "${puid}:${pgid}" ]; then
        return 0
    fi
    echo "    ./data is owned by ${owner} but the container runs as ${puid}:${pgid} — fixing ownership..."
    if [ "$(id -u)" = "0" ]; then
        chown -R "${puid}:${pgid}" ./data
    elif sudo chown -R "${puid}:${pgid}" ./data; then
        :
    else
        echo "ERROR: could not change ownership of ./data (owned by ${owner})."
        echo "Run this on the host, then run this script again:"
        echo "  sudo chown -R ${puid}:${pgid} ./data"
        exit 1
    fi
}

# Port: $PORT wins, then .env, then the 8285 default.
PORT="${PORT:-8285}"
if [ -f .env ]; then
    ENV_PORT="$(grep -E '^PORT=' .env | tail -n 1 | cut -d= -f2- | tr -d ' "')" || true
    if [ -n "${ENV_PORT:-}" ]; then PORT="$ENV_PORT"; fi
fi

echo "==> Checking for Docker..."
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: 'docker' is not installed or not on PATH."
    echo "Install Docker Engine first, then run ./install.sh again."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' (Compose v2) is not available."
    echo "Your Docker install may be too old — update Docker, then retry."
    exit 1
fi
echo "    Docker OK: $(docker --version | head -n 1)"

echo "==> Making sure ./data is writable by the container user..."
ensure_data_dir

echo "==> Building and starting Cadence (this may take a minute the first time)..."
docker compose up -d --build

echo "==> Waiting for Cadence to answer on http://127.0.0.1:${PORT}/api/health ..."
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        echo ""
        echo "✓ Cadence is up: open http://<server-ip>:${PORT}"
        echo "  (replace <server-ip> with this server's LAN address)"
        echo "  The first-run wizard will ask for your inbox + Plex library folders."
        exit 0
    fi
    sleep 2
done

echo ""
echo "ERROR: Cadence did not answer within ~60 seconds."
echo "The container may have failed to start. Recent log:"
docker compose logs --tail 50
exit 1
