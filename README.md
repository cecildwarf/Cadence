# Cadence

Turn messy multi-file MP3 audiobooks into one Plex-ready `.m4b` — sorted first, then merged.
Self-hosted, Docker, LAN-only (no login — don't expose it to the internet).

## Install

Needs Docker Engine + Docker Compose v2 on a machine that can see your Plex library folders.

```bash
git clone https://github.com/cecildwarf/cadence.git
cd cadence
docker compose up -d --build
```

Then open `http://<server-ip>:8285` from any phone or PC on your home network.
The first-run wizard asks for your inbox and Plex library folders — pick them and you're done.
Or run `bash install.sh` instead of the `docker compose` line: same steps, with checks and a clear error if Docker is missing.

No `.env` file needed. Every setting has a working default (port `8285`, timezone UTC, user `1000:1000`).

## Update

```bash
cd cadence
bash update.sh
```

Or by hand: `git pull --ff-only && docker compose up -d --build`

This pulls the latest code from GitHub and rebuilds the container. Your settings, jobs, and logs in `./data` are kept. (`docker compose pull` does not update Cadence — the image is built locally, so `git pull` is what fetches the new version.) There is no Update button in the web UI — the container has no git repo in it.

## How to use

1. **Browse** — pick book folders under the inbox. Your phone is a remote control; no file upload from it is needed.
2. **Review the order** (multi-file books) — auto-sort guesses the order and shows its confidence per file. Fix it with ↑ ↓ buttons or drag, then confirm. Single-file books skip this and queue straight away.
3. **Build** — one `.m4b` with a chapter per file, tags and cover art. Watch the live progress bar in the Queue tab.
4. **Rescan Plex** — point Plex at the output folder as a **Music** library ("Prefer local metadata" on).

Nothing moves until the book is successfully in Plex — only then are the originals moved to `Processed/<Author>/<Book>/`, so you can re-process later. A failed, cancelled, held, or rejected book leaves its originals in the source folder.

**Health check:** after every build, the finished file is checked against the book's published length and chapter list. A file that looks wrong never reaches Plex — it waits in the Review folder until you approve it or reject it.

**Cleanup:** the Cleanup tab lists processed originals whose finished `.m4b` is verified safe to delete, biggest first. Keep any you want, confirm, and it deletes the rest — only ever inside Processed.

## When the server is busy

Cadence works when it's quiet and backs off when it's not: if the server load tops 70% of its CPU cores, or Plex starts transcoding, queued books wait and a running encode pauses (its % freezes) until things have been quiet for 2 minutes — then it carries on where it stopped. Add your Plex URL + token in ⚙️ Settings (there's a Test button) so it can see transcodes; without them, load alone decides. The "Only run between" window in Settings is the backup — outside it, builds wait. It doesn't watch Tdarr or the GPU directly; Tdarr's load shows up in the server load anyway.

## Folders

One media mount: `MEDIA_ROOT` on the host is mounted at `/disks` in the container (default `MEDIA_ROOT=/disks`). Inbox, output, Processed, and Review are all folders under it.

Change any of them in ⚙️ Settings — no restart, as long as the new folder is under `/disks`. A folder outside it is rejected: a running container can't gain a new host folder, so changing `MEDIA_ROOT` itself needs `docker compose up -d` to recreate it. Settings shows a **Mounted folders** readout of what the container can actually see.

`.env` is optional overrides only — copy `.env.example` to `.env` only if you want to change a default (`MEDIA_ROOT`, `PORT`, `TZ`, `PUID`/`PGID`). The first-run wizard is the default path for folders.

Port `8285` by default. If it's taken, set `PORT=8286` in `.env` and restart.

## If it fails

A failed job says exactly why in the UI (file, step, ffmpeg error) — never just "something went wrong". For help, Logs tab → pick the job → **Copy log** → paste it in chat. Or on the server: `docker logs cadence --tail 50`.

Container restart-looping with `Permission denied: '/app/data/logs'`? The host `./data` folder was created owned by root — fix it with `docker compose down && sudo chown -R 1000:1000 ./data && docker compose up -d --build` (or just run `bash install.sh` / `bash update.sh` — they fix the ownership first).

## Develop / test

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
```

Stack: Python 3.12 + Flask + mutagen + ffmpeg. No database — jobs are JSON files under `data/jobs/`.
