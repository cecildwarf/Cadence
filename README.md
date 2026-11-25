# Cadence

A self-hosted Bragi Books replacement for any home server running Plex.
MP3s in, one Plex-ready `.m4b` out — **sorted first, then merged**.

What it does that Bragi doesn't (the reason it exists):

1. **Auto-sort that shows its work.** Multi-file MP3 audiobooks arrive
   out of order; Cadence guesses the order from ID3/MP4 track numbers,
   then filename numbers (`01 - …`, `Part 03`, `Chapter 12`, `CD1-03`,
   `01 of 24`), then natural filename order — and labels every file
   high / medium / low confidence with the reason why.
2. **You correct the order before anything is merged.** Drag rows
   (mouse), or on a phone use the ↑ ↓ buttons or "move to #" on each
   row. Remove a file, rename a chapter, then confirm.
3. **Metadata from Audible via audnexus** (public API, no key) by ASIN
   or title search — title, author, narrator, series + #, year,
   description, cover art — every field editable before building.
4. **One `.m4b` out**, with chapter markers (one per input file),
   tags + embedded cover, placed in Plex's recommended structure:
   `Author/Book Title/Book Title.m4b`, or with a series
   `Author/Series/Year - Book Title [Series #]/Book Title (Year) [Series #].m4b`.
5. **A real queue.** Queue lots of books at once; each shows a live
   progress bar + % + stage; one build runs at a time. Single-file
   books skip the review step and queue straight away.
6. **Loud logging.** Bragi errors out with no indication of what's
   going on. Cadence logs every stage, every file, every ffmpeg
   command + exit code + stderr — and a failed job says exactly why
   in the UI. See *When something fails* below.
7. **Health check + Review.** After every build, the finished file
   is checked against the book's published length and chapter list.
   A file that looks wrong never reaches Plex — it waits in a
   Review folder until you approve it or reject it. See below.

## Install — on your server (Docker)

**Prerequisites:** Docker Engine + Docker Compose (v2, the `docker compose`
command) on any machine that can see your Plex library folders — a
home server, a NAS that runs Docker, or a spare PC. Nothing else is
needed on the host: Python, ffmpeg, and all app dependencies live
inside the container. The app listens on port **8285** by default.

```bash
# 1. On your server, in the folder you keep apps in:
git clone https://github.com/cecildwarf/cadence.git
cd cadence

# 2. Copy .env.example to .env and set your media folders:
cp .env.example .env
nano .env
```

In `.env` there are two kinds of paths — don't mix them up:

| Setting | What it is | Example |
|---|---|---|
| `INBOX_HOST_DIR` / `OUTPUT_HOST_DIR` | Folders **on your host** (the server) | `/path/to/audiobooks/in` |
| `INBOX_DIR` / `OUTPUT_DIR` | Matching paths **inside the container** | `/audiobooks/in`, `/audiobooks` |

The defaults mount `/audiobooks/in` and `/audiobooks` in the container.
Set the `*_HOST_DIR` values to your real folders, and leave
`INBOX_DIR` / `OUTPUT_DIR` at the container paths (or leave both
unset — the first-run wizard in the web UI will ask you instead, with
a folder picker). `TZ` is your timezone for log timestamps
(default `UTC`); `PUID` / `PGID` are the user/group the container
runs as (default `1000:1000` — match the user your media server
runs as, so written files are owned correctly).

```bash
# 3. Build + start:
docker compose up -d --build

# 4. Open from ANY computer/phone on your home network:
#    http://<server-ip>:8285
#    (replace <server-ip> with your server's LAN address)

# 5. If the page doesn't load, check the container log:
docker logs cadence --tail 50

# Useful later:
docker compose pull && docker compose up -d --build   # update
docker compose down                                   # stop
docker compose ps                                     # status
```

What lives where: `./data` (next to `docker-compose.yml`) is mounted
to `/app/data` in the container and holds jobs, logs, and settings —
it survives restarts and rebuilds. Your media folders are mounted
read/write so the app can read the inbox and write finished `.m4b`
files into the output folder.

**LAN-only.** Do not publish port 8285 on your router and do not
expose this to the internet — there is no login on the UI. It is meant
for the home network only.

Point Plex at the output folder as a separate **Music** library (the
community audiobook setup), with "Prefer local metadata" on, then
rescan the library after a build finishes.

## Will it clash? (running next to Plex / Tdarr / audiobookshelf)

Cadence is designed to sit quietly alongside the usual home-server
stack. It was audited against a server running Plex (Docker),
Tdarr v3 (8265/8266), audiobookshelf, tautulli, bragibooks,
what-to-watch, Home Assistant, Music Assistant, ESPHome, Curatarr,
and Watchtower — no shared ports, names, networks, or folders
except the inbox/output folders you point it at.

| Thing | Cadence | Clash? |
|---|---|---|
| Port | **8285** by default (`$PORT`, overridable in `.env`) | No — distinct from Plex 32400, Tdarr 8265/8266, audiobookshelf 13378, tautulli 8181, Home Assistant 8123. Tdarr's 8265/8266 are the closest numbers and still different. |
| Container / image | Container `cadence`, image built from this repo (`cadence-cadence`) | No — no other service in the stack uses that name. |
| Network | Default bridge network, no `--network host` | No — nothing else binds its ports. |
| GPU | None — CPU-only ffmpeg, no `/dev/dri`, no QSV/VAAPI, no `privileged` | No — it can't fight Tdarr for Quick Sync. |
| Volumes | `./data:/app/data` (own jobs/logs/settings, next to `docker-compose.yml` — not `~/tdarr`) + the inbox/output folders you configure | Only the folders you configure. It writes finished `.m4b` + `cover.jpg` into the output tree and moves originals to Processed — it never touches Plex/Tdarr config, databases, or media outside those folders, and Cleanup only ever deletes inside Processed. |
| CPU / jobs | One build at a time (single queue runner, no parallel jobs), one CPU ffmpeg encode | Light — a single AAC encode. If Tdarr is mid-transcode, queueing books just waits its turn; pause the queue from the UI if you want it fully idle. |

If the port is already bound when you start it, Docker refuses to
start the container (`port is already allocated`) and the app itself
fails fast with "address already in use" — it never silently takes
another port.

**Pre-flight check — run this on your server before the first
`docker compose up` (and again any time you suspect a clash):**

```bash
docker ps --format '{{.Names}} {{.Ports}}'
docker compose config | grep -A2 -i "ports\|container_name"
```

Look for `8285` in the first command's output. If another container
already uses it, set a different port in `.env` and restart:

```bash
# in .env:
PORT=8286
docker compose up -d --build
# then open http://<server-ip>:8286
```

> Note: this audit was done against the documented stack above, not
> by logging into your server — the live `docker ps` check on your
> server is the final step before `docker compose up`.

## First-run setup

On first open — or if a saved path later breaks (disk unmounted,
folder deleted) — Cadence shows a setup wizard **before** the main UI:

1. **Inbox** — where messy audiobook folders land.
2. **Output** — your Plex audiobook library folder.
3. **Processed** (optional) — where finished books' originals are kept.
   Leave empty for the default: a `Processed` folder inside the inbox.
4. **Review** (optional) — where health-held files wait for your decision.
   Leave empty for the default: a `Review` folder inside the inbox.
   It must stay **outside** your Plex output folder, or Plex would scan
   held, unchecked files — the wizard refuses that setup.
5. **Timezone** — for log timestamps (default UTC).

For each path you can tap a one-tap suggestion, browse the server's
folders with the built-in picker (tap to descend, "Use this folder"
to pick — no Linux path typing on a phone keyboard), or type a path
manually. Paths are validated on save: the inbox must exist and
be readable, the output must exist (or tick "Create it if missing")
and be writable. A bad path is rejected with a clear error — Cadence
never silently falls back to a wrong default.

The same paths stay editable in ⚙️ **Settings** afterwards (same
picker + validation). Changing the output folder only affects future
builds — books already placed stay where they are. If a saved path
later becomes invalid, a banner appears on open saying which path is
broken, with a button straight to Settings to fix it.

## Using it (phone or PC — same UI, same steps)

1. **Browse** — pick book folders or files under the inbox. Your phone
   is a remote control: the app browses folders *on the server*, so no
   file upload from your phone is needed.
2. **Queue** — see below.
3. **Review order & metadata** (multi-file books) — check the auto-sort
   (confidence badge + reason on every row), fix anything wrong, look
   up the ASIN or edit the fields by hand.
4. **Build .m4b** — watch the live progress bar. When it says DONE,
   the file is in the Plex tree. **Nothing moves until the book is
   successfully in Plex:** only then are the originals **moved**
   (not copied, not deleted) to the **Processed** folder:
   `Processed/<Author>/<Book>/` with their original filenames, so the
   book can be re-processed later if needed. Before anything is placed,
   the **health check** runs (see below) — a file that looks wrong is
   held in the Review folder instead, and the job says so. While it is
   held, and if it fails, is cancelled, or is rejected, the originals
   stay exactly where they were, in the source folder.
5. Rescan the Plex audiobook library.

### The queue

**How a folder is routed when you pick it:**

| Audio files in the folder | What happens |
|---|---|
| Exactly 1 | Auto-queued straight away — no sorting to do. Metadata is guessed from the filename (ASIN if present); the real lookup runs at build time, and you can edit the fields from the queue row before it starts. |
| More than 1 | Goes to the review flow first (auto-sort + reorder + metadata). Confirming it joins the build queue. |
| 0 | No job is created. You get a clear "no audio files in this folder" message. |

A single file picked on its own is auto-queued the same way.

**Queueing lots at once:** tick the checkboxes on the Browse rows
(or **Select all**), then **Queue selected** — or **Queue all in this
folder**. Each book becomes its own queue entry with its own progress,
and the runner builds them **one at a time, in order** (never two
ffmpeg encodes at once on a small server). Queue position (#1, #2, …)
is shown on every row.

**Progress is real, not a spinner:** Scanning 0–10%, Metadata 10–20%,
Sorting 20–30%, Converting 30–90% (parsed live from ffmpeg's
`out_time_ms` against the total duration), Health check 90–95%,
Placing in Plex 95–100%.
The Queue tab polls every 2 seconds while anything runs, and progress
is saved on the job record, so reloading the page keeps it. Progress
milestones are logged too.

**Clear / Cancel:** every queue row has a ✕ button (min. 44px touch
target). A queued or in-review item is simply removed. A running item
is Cancel: the encode stops, partial output is deleted, and the job is
marked **Cancelled** — not Failed, not Done. **Clear finished** and
**Clear failed** remove those jobs in bulk. Clearing never deletes
source files or finished books in Plex — only the queue entry itself.
(Once a finished file has been placed in Plex, a late cancel can't
un-place it — the job completes instead.)

## Health check — did anything go missing?

Bragi never tells you a file was missing from a book. Cadence checks
every finished file **before** it is allowed into Plex, against the
metadata pulled from the internet (audnexus runtime + chapter list):

1. **Total length vs the published runtime** — e.g. the metadata says
   6 hours and the file is only 5h 12m. The tolerance is >2% **or**
   >3 minutes, whichever is larger.
2. **Total length vs the summed inputs** (within 2% — the build's own
   gate, reported here as part of the health report).
3. **Per chapter** — every input file is one chapter: its actual
   length, its start timestamp in the finished file, and — when the
   metadata has chapter times — its expected length. Short, zero-length,
   duplicated or missing chapters are flagged by number and name.
4. **File count vs expected chapters**, and gaps/duplicates in the
   part numbering.
5. **Integrity** — every input and the finished file must probe cleanly;
   no zero-byte files, no corrupt files.

**The report talks in plain language, not just pass/fail.** It names
the suspect chapter and where it should be, for example:

> Expected 6h 00m, got 5h 12m — 48 minutes missing.
> Chapter 3 is 1h 12m long against an expected 2h 00m — 48 minutes
> is missing from the total, and this chapter accounts for it.
> Verify chapter 3 is complete; it should start at 04:00:00 in
> the output.

The full report — verdict, every diagnosis, and a per-chapter table
(#, title, expected length, actual length, start timestamp, status:
OK / SHORT / MISSING / EXTRA / UNCHECKED) — is saved per job at
`data/jobs/<job-id>-health.json` and viewable from the job and the
Review tab. Every check is logged too.

**If the metadata has no runtime or chapter list**, the check runs in
**Limited** mode (inputs vs output + integrity + counts only) and
says so clearly — the file still goes to Plex, it is never held
just because the metadata was thin.

### The Review folder — held files wait for you

A file that **fails** or **warns** (wrong total length, missing/short
chapter, count mismatch) never goes to Plex. It goes to the **Review**
folder instead — by default a `Review` folder inside your inbox, next
to Processed, and always outside the Plex output library so Plex can
never scan a bad file. Change it in first-run setup or ⚙️ Settings
(`REVIEW_DIR` in `.env` also works). The job becomes
**Needs health review**, a banner appears on the home page, and the
🔍 **Review** tab lists it with the full health report.

From the Review tab, on your phone or PC:

- **✓ Looks OK — place in Plex** — moves the held `.m4b` (and
  `cover.jpg`) to its correct Plex location, marks the job Done, and
  records **what you overrode** (the health verdict and the diagnoses)
  plus an optional note — the approval is on the record, not silent.
- **🗑 Reject — delete held file** — deletes the held `.m4b` after a
  confirmation showing the filename. The originals stay in their
  original source folder, so you can re-process the book from there.
  The job becomes Rejected.
- **↻ Re-run health check** — re-probes the held file and re-compares
  it against the recorded inputs and metadata. It updates the report
  but never places the file by itself — you still approve or reject.

A held job cannot be cleared or cancelled from the Queue tab — that
would orphan the held file with no decision. Approve or Reject it
from Review instead.

### Reordering on a phone

Reordering never depends on drag. On every file row you get:

- **↑ / ↓ buttons** — move one step (large, 44px touch targets);
- **"Move to #" + Go** — jump straight to a position;
- **✕ Remove** — drop a file from this book (the file stays on the server);
- **Chapter title field** — rename the chapter marker for that file.

On a PC the same buttons work, plus mouse drag-and-drop on the rows.
Touch drag also works (drag a row up/down), but it is an extra —
the buttons are the guaranteed path.

## Mobile-first — checked at phone width

The UI was built mobile-first and checked at **390px width (iPhone)**:
single-column layout under ~760px, min-44px touch targets, no
hover-only controls, no tiny text, works in iOS Safari and Android
Chrome. Every workflow — browse, bulk queue, reorder, metadata, ASIN
lookup, build + progress, Cleanup, Logs + Copy, Settings, first-run setup —
works end-to-end on a phone. **Desktop-only steps: none.** The desktop
layout is the same pages widened (file rows become a two-column grid,
log pane taller).

## The Processed folder — and the move-timing rule

**Nothing moves until the book is successfully in Plex.** That is
the rule, in all three states:

1. **Built + health pass → Plex → originals move.** The finished
   `.m4b` lands in Plex, then the originals are moved to Processed.
2. **Built + health hold → Review, originals STAY in source.** The
   finished file waits in Review; the source files are untouched in
   their original folder.
3. **Held + approved → Plex → originals move.** Approving moves the
   `.m4b` into Plex, and as part of that same step the originals
   move to Processed. **Held + rejected → `.m4b` deleted, originals
   STAY in source** — re-process from the source folder.

A failed build, a cancelled job, or a cleared queue entry likewise
never moves anything. If a move itself fails part-way through (one
file moves, the next cannot), the job is still Done — the Plex file
is already placed — but with a warning naming the files that did
not move and where they still are; the already-moved files are
listed on the job record.

Every book that finishes successfully is filed away, not thrown away. After
a successful build, the original source files are **moved** (not copied,
not deleted) to:

```
<Processed>/<Author>/<Book>/<original filenames>
```

By default Processed is a `Processed` folder inside your inbox
(`<inbox>/Processed`); change it in first-run setup or ⚙️ Settings
(`PROCESSED_DIR` in `.env` also works). To **re-process** a book that
failed, was held, or was rejected, start it again from its original
source folder. To re-process a book that already succeeded, browse to
its folder under Processed and start it again — the originals are
still there, untouched, with their original filenames.

Originals are never deleted automatically at build time. If you want
the disk space back later, use 🧹 **Cleanup** below — it only ever
deletes processed originals whose finished .m4b is verified safe, and
only after you confirm.

## Cleanup — free disk space, safely

The 🧹 **Cleanup** tab scans the Processed folder and matches each
processed book against its finished file in Plex. A book is listed as
**safe to delete** only if ALL of these hold:

1. A matching completed `.m4b` exists in the output tree.
2. That `.m4b` is a valid audio file (ffprobe succeeds, duration > 0).
3. Its duration is within 2% of the processed sources' total duration
   (or of the recorded job duration, when a job record exists).
4. The job record for it is **Done** (not Failed / Cancelled).

If any check fails — or the match is ambiguous — the book goes in
**Needs review** with the reason why, and is never on the delete list.

The safe list shows book title/author, size per book, processed path,
finished output path, the duration-check result, and the job date —
biggest savings first, with the total reclaimable space at the top.
The list itself is the preview: **nothing is deleted until you confirm.**

- **Keep:** every safe row has a Keep button that takes that book off
  the delete list (it stays in Processed). Kept books stay excluded
  on refresh; tick "Show kept items" to see them and un-keep them.
- **Delete:** the Delete button opens a confirmation listing exactly
  how many books and how much space will be freed. Only after Confirm
  are the processed folders deleted, one by one, each deletion logged
  (path + size freed). If a deletion fails mid-run it stops, reports
  which book failed and why, and leaves the rest untouched.
- **Safety:** cleanup deletes only files under the configured
  Processed folder — never anything under Output, never anything
  outside Processed. Every delete path is validated first and refused
  otherwise (and logged).

## When something fails

Cadence never silently fails and never shows a generic
"something went wrong". A failed job shows the exact reason in the
UI (exact file + exact step + ffmpeg exit code + stderr tail).

Logs live on the server (volume-mounted, survive restarts):

| Log | Location |
|---|---|
| Global log (rotating, 30 days kept) | `data/logs/app.log` |
| One log per job (text) | `data/logs/<job-id>.log` |
| One log per job (machine-readable) | `data/logs/<job-id>.jsonl` |
| Job record (order, metadata, output, durations, progress) | `data/jobs/<job-id>.json` |

Startup problems (missing inbox/output folder, missing ffmpeg,
unwritable output dir) are logged at boot **and** shown in a banner
at the top of the web UI — not just in `docker logs`. A restart
mid-build re-queues the interrupted job instead of leaving it stuck
on "building".

### Sending a log from your phone

1. Open Cadence on your phone → **Logs**.
2. Pick the failed job from the dropdown → **Load log**.
3. Tap **📋 Copy log** (primary action — one tap, full log on the
   clipboard; a "✓ Copied" confirmation shows). Or tap
   **📋 Copy last error only** if you only want the failure: it
   copies just the ERROR entries + the 20 lines before the first
   error, so you don't paste a huge log.
4. Paste it in chat.

No copy button? Use **📄 Plain-text view** — a clean page with no
site chrome (`/logs/<job-id>/text`, or `/logs/app/text` for the
global log). Long-press → **Select All → Copy**, then paste.

The pasted log is self-contained: a summary block at the top (job
id, book title/ASIN, timestamps, per-file outcomes) followed by the
full log, so it can be diagnosed from the paste alone. The
**Download .log** button is still there if you prefer a file.

## Settings

In the UI (⚙️ Settings) or `.env`: inbox dir, Plex output dir,
Processed dir (default `<inbox>/Processed`), Review dir (default
`<inbox>/Review`, must stay outside the output folder), temp dir, AAC bitrate
(default 96k), audnexus mirror URL, timezone (default UTC), and what
happens to originals after a successful build — `processed` (default,
move to `Processed/<Author>/<Book>/`) or `keep` (leave in place).
Originals are never deleted at build time. Saved to
`data/settings.json`. UI-saved settings win over `.env` after first run.

## How the build works (short version)

- MP3 inputs are re-encoded to AAC at the source sample rate
  (default 96k stereo); m4a/m4b inputs already in AAC are
  stream-copied. One `ffmpeg` run merges in the confirmed order,
  with `-progress` reporting real encode progress.
- Chapters: one per input file, from the chapter title you confirmed.
- Duration check: output must be within 2% of the summed inputs,
  otherwise the job FAILS and nothing is placed in Plex.
- **Health check:** the finished file is compared against the
  metadata runtime (tolerance: >2% or >3 minutes) and chapter list.
  A warn/fail verdict holds the file in the Review folder — it never
  reaches Plex until you approve it. Reports live at
  `data/jobs/<job-id>-health.json`.
- Tags written: title, album, album artist (author), artist
  (narrator, else author), genre, year, description, cover art.

## Development / tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m py_compile app.py sort.py build.py metadata.py plex_output.py cadence_logging.py cadence_queue.py health.py cleanup.py
```

Stack: Python 3.12 + Flask + mutagen + ffmpeg CLI. No database —
jobs are JSON files under `data/jobs/`. Kept deliberately small
for a home server that is also running Plex.

## Notes / decisions

- **Port 8285** — picked as the default; change it with `PORT` in
  `.env` (the compose mapping and the app both follow it) + the URL
  if it clashes with anything. See "Will it clash?" below.
- **Inbox / output paths** — no defaults are assumed. They come from
  the first-run wizard or `.env` (see Install above).
- **Repo URL** — `github.com/cecildwarf/cadence` is the
  project's own home.
- **Originals default** — moved to `Processed/<Author>/<Book>/`, never deleted.
- **Metadata source** — audnexus by ASIN first; title search is a
  best-effort fallback and manual fields always win.
- **AAC bitrate** — 96k stereo default (matches Bragi/m4b-merge
  practice); changeable in Settings.
- **Name** — "Cadence", confirmed 2026-11-24 — a nod to Crypt of the
  NecroDancer's protagonist, and to the cadence of a narrator's voice.
