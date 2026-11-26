#!/usr/bin/env bash
# Cadence — update to the latest version from GitHub.
#
#   ./update.sh
#
# What it does, in order (and it stops with a clear error if any
# step fails — it never half-updates):
#   1. git pull --ff-only   — fetch the latest code from GitHub
#   2. make ./data owned by the container user (PUID:PGID), then
#      docker compose up -d --build
#                            — rebuild the image from the new code
#                              and recreate the container
#   3. docker compose ps + a health check that the app answers
#
# Your settings, jobs, and logs are kept: they live in ./data,
# which is a mounted volume (./data:/app/data). Rebuilding the
# image and recreating the container never touches that volume.
# Idempotent: safe to run again — if nothing changed on GitHub,
# git pull reports "Already up to date" and the container is
# simply restarted with the same code.
#
# Updates happen on the host with this script (or the manual
# commands below) — there is no Update button in the web UI, and
# there can't be: the running container has no git repo in it.
# Manual equivalent:
#   git pull --ff-only && docker compose up -d --build
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

echo "==> Step 1/3: pulling the latest code from GitHub..."
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: this folder is not a git checkout (no .git here)."
    echo "Update from a cloned copy of the repo instead:"
    echo "  git clone https://github.com/cecildwarf/cadence.git"
    exit 1
fi
if ! git diff --quiet; then
    echo "ERROR: you have uncommitted changes to tracked files in this repo."
    echo "Update would overwrite them. Commit them, stash them"
    echo "('git stash'), or discard them ('git checkout -- .'), then run ./update.sh again."
    git status --porcelain
    exit 1
fi
if ! git pull --ff-only; then
    echo "ERROR: 'git pull' failed — see the git output above."
    echo "Common cause: the local checkout has diverged from GitHub."
    echo "Fix it (e.g. 'git status' to see why), then run ./update.sh again."
    echo "Nothing was rebuilt and the running container was not touched."
    exit 1
fi

echo ""
echo "==> Step 2/3: rebuilding the container from the new code..."
echo "    (./data — settings, jobs, logs — is a mounted volume and is kept.)"
ensure_data_dir
docker compose up -d --build

echo ""
echo "==> Step 3/3: checking the updated container is up..."
docker compose ps
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        echo ""
        echo "✓ Updated and running: http://<server-ip>:${PORT}"
        echo "  Settings, jobs, and logs in ./data were kept."
        exit 0
    fi
    sleep 2
done

echo ""
echo "ERROR: the rebuilt container did not answer within ~60 seconds."
echo "The update may have broken the app — the old container is gone,"
echo "but your data is safe in ./data. Recent log:"
docker compose logs --tail 50
exit 1
