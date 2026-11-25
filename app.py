"""
Cadence — self-hosted audiobook tool (Bragi Books replacement).

Mobile-first, LAN-only web UI. The phone is a remote control, NOT a
file source: the app browses server-side folders under INBOX_DIR,
so there is no file upload from the phone in the main flow.

Workflows (all work end-to-end on phone AND desktop — no step is
desktop-only):
  Browse inbox folder -> auto-sort review/reorder -> metadata
  (ASIN / audnexus, editable) -> queue -> Build single .m4b ->
  HEALTH CHECK (against the metadata runtime/chapters) -> Plex tree,
  or the Review folder when the check holds the file for a human.

Health check: after every build, the finished .m4b is compared
against the metadata runtime and chapter list. A mismatch (wrong
total length, missing/short chapter, bad file count) does NOT go
to Plex — it goes to the Review folder and the job becomes
"needs_health_review" until you approve it or reject it.

Move timing — the rule: NOTHING moves until a book is
successfully in Plex. Originals move to Processed only after the
finished .m4b is placed in Plex (health pass, or approve from
Review). A failed, cancelled, held, or rejected book leaves its
source files exactly where they were, in the source folder.

Queue rules (also in cadence_queue.py, the UI and the README):
  * 1 audio file  -> auto-queued straight away (no sorting to do).
  * >1 files      -> organisation/review flow first.
  * 0 files       -> no job at all, clear message.
  * Bulk queueing creates one entry per book; the runner builds
    them ONE AT A TIME (never ffmpeg jobs in parallel).

First run: no paths are assumed. On first open (or if a saved path
later breaks) the setup wizard asks for the inbox (input) and
output (Plex audiobook library) folders, with a server-side folder
picker — no Linux path typing on a phone keyboard required.

Logging rule (non-negotiable): never silently fail. Every stage
logs with timestamp (local time + UTC), level, stage, job id and
per-file outcome. Any failure marks the job FAILED in the UI with
the exact reason shown — never "something went wrong", never a
partial job marked done. Logs live in data/logs/ (volume-mounted)
and are copyable from the Logs view on a phone.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template, request, Response, send_file

import build as build_mod
import cleanup as cleanup_mod
import cadence_logging as flog
import cadence_queue as queue_mod
import health as health_mod
import metadata as meta_mod
import plex_output
from sort import auto_sort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CADENCE_DATA_DIR") or os.path.join(BASE_DIR, "data")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

# No assumed paths: inbox/output come ONLY from the first-run
# prompt (saved to data/settings.json) or from the environment
# (.env) if the installer sets them. Empty until the user picks.
DEFAULTS = {
    "inbox_dir": os.environ.get("INBOX_DIR", ""),
    "output_dir": os.environ.get("OUTPUT_DIR", ""),
    # Processed folder: empty = <inbox>/Processed (set in first-run
    # or Settings; .env PROCESSED_DIR also works).
    "processed_dir": os.environ.get("PROCESSED_DIR", ""),
    # Review folder: empty = <inbox>/Review (set in first-run or
    # Settings; .env REVIEW_DIR also works). Health-held files go
    # here — it MUST be outside the Plex output library, so Plex
    # never scans a bad file.
    "review_dir": os.environ.get("REVIEW_DIR", ""),
    "temp_dir": os.environ.get("TEMP_DIR", os.path.join(DATA_DIR, "temp")),
    "bitrate": os.environ.get("AAC_BITRATE", "96k"),
    "audnexus_url": os.environ.get("AUDNEXUS_URL", "https://api.audnexus.com"),
    # processed (default: move originals to Processed/<Author>/<Book>/)
    # or keep (leave originals in place). Originals are NEVER
    # deleted automatically at build time.
    "keep_originals": os.environ.get("KEEP_ORIGINALS", "processed"),
    "timezone": os.environ.get("TZ", "UTC"),
}

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".aac", ".ogg", ".wav"}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

flog.init_logging(DATA_DIR)
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Settings + first-run setup
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        settings.update({k: v for k, v in saved.items() if v not in (None, "")})
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        flog.log_error(None, "startup",
                       f"Settings file {SETTINGS_PATH} unreadable — using defaults. "
                       f"Exact error: {exc!r}")
    # Migrate the old originals behaviour: 'move_done' is the old
    # name for 'processed'; 'delete' is no longer offered — originals
    # are never deleted automatically at build time, so an old
    # 'delete' setting becomes 'keep' (safest) and is logged.
    old_keep = settings.get("keep_originals")
    if old_keep == "move_done":
        settings["keep_originals"] = "processed"
    elif old_keep == "delete":
        settings["keep_originals"] = "keep"
        flog.log_warn(None, "startup",
                      "Old setting keep_originals='delete' is no longer "
                      "supported — originals are never deleted at build "
                      "time. Changed to 'keep'.", outcome="warning")
    elif old_keep not in ("processed", "keep"):
        settings["keep_originals"] = "processed"
    # Timezone: UI-saved settings win; an unknown name falls back
    # to UTC and is logged, never silently accepted.
    if not flog.set_timezone(settings.get("timezone") or "UTC"):
        flog.log_warn(None, "startup",
                      f"Unknown timezone {settings.get('timezone')!r} — using UTC.",
                      outcome="warning")
        settings["timezone"] = "UTC"
    return settings


def save_settings(settings: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)


SETTINGS = load_settings()
STARTUP_ERRORS: list[dict] = []


def _processed_dir(settings: dict | None = None) -> str:
    """The Processed folder: the configured path, or
    <inbox>/Processed when nothing is configured."""
    s = settings or SETTINGS
    custom = (s.get("processed_dir") or "").strip()
    if custom:
        return custom
    inbox = (s.get("inbox_dir") or "").strip()
    return os.path.join(inbox, "Processed") if inbox else ""


def _review_dir(settings: dict | None = None) -> str:
    """The Review folder: the configured path, or <inbox>/Review
    when nothing is configured. Health-held files live here, next
    to Processed and outside the Plex output library."""
    s = settings or SETTINGS
    custom = (s.get("review_dir") or "").strip()
    if custom:
        return custom
    inbox = (s.get("inbox_dir") or "").strip()
    return os.path.join(inbox, "Review") if inbox else ""


def _is_inside(base: str, path: str) -> bool:
    """True when path resolves inside base (path-traversal guard)."""
    if not base or not path:
        return False
    base_real = os.path.realpath(base)
    target = os.path.realpath(path)
    return os.path.commonpath([base_real, target]) == base_real


def validate_paths(inbox: str, output: str, create_output: bool = False) -> list[dict]:
    """Validate the two paths. Returns a list of {field, message}
    errors (empty = valid). Never raises. If create_output is True,
    a missing output folder is created when its parent allows it."""
    errors: list[dict] = []
    inbox = (inbox or "").strip()
    output = (output or "").strip()
    if not inbox:
        errors.append({"field": "inbox_dir",
                       "message": "Choose an inbox (input) folder — "
                                  "where messy audiobook folders land."})
    elif not os.path.isdir(inbox):
        errors.append({"field": "inbox_dir",
                       "message": f"Inbox folder does not exist: {inbox}. "
                                  f"Pick an existing folder on the server."})
    elif not os.access(inbox, os.R_OK):
        errors.append({"field": "inbox_dir",
                       "message": f"Inbox folder is not readable: {inbox}. "
                                  f"Check ownership/permissions."})
    if not output:
        errors.append({"field": "output_dir",
                       "message": "Choose an output folder — your Plex "
                                  "audiobook library folder."})
    elif not os.path.isdir(output):
        parent = os.path.dirname(output.rstrip("/")) or "/"
        if create_output and os.path.isdir(parent) and os.access(parent, os.W_OK):
            try:
                os.makedirs(output, exist_ok=True)
            except OSError as exc:
                errors.append({"field": "output_dir",
                               "message": f"Could not create output folder "
                                          f"{output}: {exc}"})
        else:
            errors.append({"field": "output_dir",
                           "message": f"Output folder does not exist: {output}. "
                                      f"Pick an existing folder, or tick "
                                      f"'Create it if missing'."})
    elif not os.access(output, os.W_OK):
        errors.append({"field": "output_dir",
                       "message": f"Output folder is not writable: {output}. "
                                  f"Check ownership/permissions."})
    return errors


def settings_path_errors(settings: dict | None = None) -> list[dict]:
    """Path errors for the current settings (no creation attempt).

    The Processed folder only errors when explicitly configured and
    unusable; when unset it defaults to <inbox>/Processed, which is
    created on demand at build time."""
    s = settings or SETTINGS
    errors = validate_paths(s.get("inbox_dir", ""), s.get("output_dir", ""))
    processed = (s.get("processed_dir") or "").strip()
    if processed and not os.path.isdir(processed):
        parent = os.path.dirname(processed.rstrip("/")) or "/"
        if not (os.path.isdir(parent) and os.access(parent, os.W_OK)):
            errors.append({"field": "processed_dir",
                           "message": f"Processed folder cannot be created: "
                                      f"{processed} (parent folder is missing "
                                      f"or not writable)."})
    review = (s.get("review_dir") or "").strip()
    if review and not os.path.isdir(review):
        parent = os.path.dirname(review.rstrip("/")) or "/"
        if not (os.path.isdir(parent) and os.access(parent, os.W_OK)):
            errors.append({"field": "review_dir",
                           "message": f"Review folder cannot be created: "
                                      f"{review} (parent folder is missing "
                                      f"or not writable)."})
    # A Review folder inside the Plex output tree would let Plex
    # scan health-held files — refuse that configuration outright.
    output = (s.get("output_dir") or "").strip()
    if review and output and _is_inside(output, review):
        errors.append({"field": "review_dir",
                       "message": "Review folder must be OUTSIDE the Plex "
                                  f"output folder ({output}) — otherwise Plex "
                                  f"would scan held, unchecked files."})
    return errors


def setup_status(settings: dict | None = None) -> dict:
    """First-run / broken-path status. setup_required is True when
    no paths are saved yet, or a saved path is missing/unusable —
    the UI then shows the setup wizard / banner, never silently
    fails a job against a wrong default."""
    errors = settings_path_errors(settings)
    tz = (settings or SETTINGS).get("timezone") or "UTC"
    try:
        ZoneInfo(tz)
    except Exception:
        errors.append({"field": "timezone",
                       "message": f"Unknown timezone: {tz}. Pick a valid "
                                  f"timezone name (e.g. UTC)."})
    return {"setup_required": bool(errors), "setup_errors": errors}


def run_startup_checks() -> list[dict]:
    """Check everything the app needs at boot; log + surface in UI.

    Bad paths, missing ffmpeg/ffprobe and unwritable output dirs
    are shown in the UI banner, not just in the container log.
    """
    errors: list[dict] = []
    s = SETTINGS

    for err in settings_path_errors():
        errors.append({"code": "bad_path", "path": err["field"],
                       "message": err["message"]})
        flog.log_error(None, "startup", err["message"], outcome="FAIL")
    if not settings_path_errors():
        flog.log_info(None, "startup",
                      f"Paths OK: inbox={s['inbox_dir']} output={s['output_dir']}",
                      outcome="pass")

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            errors.append({"code": "missing_tool", "path": tool,
                           "message": f"{tool} not found in PATH. The Docker image installs it; "
                                      f"if you installed without Docker, install ffmpeg first."})
            flog.log_error(None, "startup", f"Missing tool: {tool}", outcome="FAIL")
        else:
            flog.log_info(None, "startup", f"{tool} found: {shutil.which(tool)}", outcome="pass")

    try:
        os.makedirs(s["temp_dir"], exist_ok=True)
        probe_file = os.path.join(s["temp_dir"], ".write-test")
        with open(probe_file, "w") as fh:
            fh.write("ok")
        os.remove(probe_file)
    except OSError as exc:
        errors.append({"code": "temp_unwritable", "path": s["temp_dir"],
                       "message": f"TEMP_DIR {s['temp_dir']} is not writable: {exc}"})
        flog.log_error(None, "startup", f"TEMP_DIR unwritable: {s['temp_dir']}: {exc}", outcome="FAIL")

    flog.log_info(None, "startup",
                  f"Startup checks finished: {len(errors)} error(s)",
                  outcome="FAIL" if errors else "pass",
                  extra={"errors": errors})
    return errors


STARTUP_ERRORS = run_startup_checks()


# ---------------------------------------------------------------------------
# Job persistence
# ---------------------------------------------------------------------------

def job_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def save_job(job: dict) -> None:
    os.makedirs(JOBS_DIR, exist_ok=True)
    with open(job_path(job["id"]), "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2)


def _update_job(job_id: str, **fields) -> dict | None:
    """Persist fields onto the FRESH job record and return it.

    Always load-then-save: a long-running build holds a stale local
    copy, and a blind save_job() of that copy would clobber fields
    another thread/request changed meanwhile (notably
    cancel_requested, set by the Cancel button)."""
    job = load_job(job_id)
    if job is None:
        return None
    job.update(fields)
    save_job(job)
    return job


def load_job(job_id: str) -> dict | None:
    try:
        with open(job_path(job_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def list_jobs() -> list[dict]:
    jobs = []
    try:
        names = os.listdir(JOBS_DIR)
    except FileNotFoundError:
        return []
    for name in sorted(names, reverse=True):
        if name.endswith(".json"):
            job = load_job(name[:-5])
            if job:
                jobs.append(job)
    jobs.sort(key=lambda j: j.get("created", ""), reverse=True)
    return jobs


def fail_job(job: dict, reason: str, stage: str = "build") -> dict:
    """Mark a job FAILED with the exact reason — never generic."""
    job["status"] = "failed"
    job["stage"] = "failed"
    job["error"] = reason
    job["failed_at"] = datetime.now(timezone.utc).isoformat()
    save_job(job)
    flog.log_error(job["id"], stage, f"Job FAILED: {reason}", outcome="FAIL")
    return job


def _with_live_position(job: dict) -> dict:
    """Attach the live queue position (#1, #2, …) to a queued /
    building job; None for anything not in the build queue."""
    if job.get("status") in ("queued", "building"):
        ordered = _queue_order()
        for i, j in enumerate(ordered):
            if j["id"] == job["id"]:
                job["queue_position"] = i + 1
                return job
    job["queue_position"] = job.get("queue_position")
    return job


# ---------------------------------------------------------------------------
# Queue — one build at a time, real progress, cancel / clear
# ---------------------------------------------------------------------------

QUEUE_POLL_SECONDS = 1.0   # tests may raise this to freeze the runner
QUEUE_RUNNER_ENABLED = True
_runner_thread: threading.Thread | None = None
_procs: dict[str, object] = {}
_procs_lock = threading.Lock()
_progress_cache: dict[str, tuple[int, str]] = {}


def _queue_order() -> list[dict]:
    """Queued + building jobs, oldest first — the build order."""
    active = [j for j in list_jobs() if j.get("status") in ("queued", "building")]
    active.sort(key=lambda j: j.get("created", ""))
    return active


def _refresh_queue_positions() -> None:
    """Persist #1, #2, … onto the queued/building job records."""
    for i, job in enumerate(_queue_order()):
        if job.get("queue_position") != i + 1:
            job["queue_position"] = i + 1
            save_job(job)


def _set_progress(job_id: str, pct: int, stage: str, force: bool = False) -> None:
    """Persist real progress on the job record (page reloads keep
    it). Writes are throttled to actual changes; stage changes and
    25% milestones are logged so the log shows the job working."""
    pct = int(max(0, min(100, pct)))
    cache = _progress_cache.get(job_id)
    if cache == (pct, stage) and not force:
        return
    job = load_job(job_id)
    if job is None:
        return
    old_pct = int(job.get("progress_pct") or 0)
    old_stage = job.get("stage") or ""
    # Progress never goes backwards within one job (except a reset).
    if pct < old_pct and not force:
        pct = old_pct
    job["progress_pct"] = pct
    job["stage"] = stage
    save_job(job)
    _progress_cache[job_id] = (pct, stage)
    if stage != old_stage:
        flog.log_info(job_id, "progress",
                      f"Stage: {queue_mod.stage_label(stage)} ({pct}%)",
                      outcome="success")
    elif pct // 25 != old_pct // 25 and pct not in (0, 100):
        flog.log_info(job_id, "progress",
                      f"Progress milestone: {pct}% — {queue_mod.stage_label(stage)}")


def _set_job_status(job_id: str, status: str, stage: str, pct: int) -> None:
    job = load_job(job_id)
    if job is None:
        return
    job["status"] = status
    job["stage"] = stage
    job["progress_pct"] = int(max(0, min(100, pct)))
    save_job(job)
    _progress_cache[job_id] = (job["progress_pct"], stage)


def _is_cancel_requested(job_id: str) -> bool:
    job = load_job(job_id)
    return bool(job and job.get("cancel_requested"))


def _register_proc(job_id: str, proc) -> None:
    with _procs_lock:
        if proc is None:
            _procs.pop(job_id, None)
        else:
            _procs[job_id] = proc


def _mark_cancelled(job_id: str, reason: str) -> bool:
    """Mark a job Cancelled (NOT failed, NOT done). Returns False
    if the job was already finished."""
    job = load_job(job_id)
    if job is None or job.get("status") in ("done", "failed", "cancelled"):
        return False
    job["status"] = "cancelled"
    job["stage"] = "cancelled"
    job["cancelled_reason"] = reason
    job["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_job(job)
    _progress_cache.pop(job_id, None)
    flog.log_info(job_id, "queue", f"Job CANCELLED: {reason}", outcome="success")
    _refresh_queue_positions()
    return True


def _cancel_job(job_id: str) -> tuple[bool, str]:
    """Cancel / clear a queued or running job.

    Queued (or still in review): marked Cancelled immediately.
    Building: a cancel is requested, the live ffmpeg process is
    stopped, and the runner marks it Cancelled and deletes any
    partial output. Finished jobs: nothing to cancel.
    Returns (ok, message)."""
    job = load_job(job_id)
    if job is None:
        return False, f"Job {job_id} not found."
    status = job.get("status")
    if status in ("done", "failed", "cancelled", "rejected"):
        return False, f"Job is already {status} — nothing to cancel."
    if status == "needs_health_review":
        return False, ("Job is waiting in Review — use Approve or Reject "
                       "there instead; cancelling would leave the held "
                       "file with no decision.")
    if status == "building":
        job["cancel_requested"] = True
        save_job(job)
        with _procs_lock:
            proc = _procs.get(job_id)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        flog.log_info(job_id, "queue",
                      "Cancel requested for running job — stopping the "
                      "encode and deleting any partial output.",
                      outcome="success")
        return True, "Cancelling — the running encode is being stopped."
    _mark_cancelled(job_id, "Cancelled by user before it started.")
    return True, "Cancelled."


def _remove_job(job_id: str) -> tuple[bool, str]:
    """Clear a job from the queue / job list. A running job must be
    cancelled first (409). Never deletes source files or finished
    Plex output — only the job record itself. The per-job log file
    is kept on disk as an audit trail."""
    job = load_job(job_id)
    if job is None:
        return False, f"Job {job_id} not found."
    if job.get("status") == "building":
        return False, "Job is building right now — Cancel it first, then clear it."
    if job.get("status") == "needs_health_review":
        return False, ("Job is waiting in Review — Approve it (places the "
                       "file in Plex) or Reject it (deletes the held file) "
                       "there instead. Clearing the record would orphan "
                       "the held file.")
    title = (job.get("metadata") or {}).get("title") or job.get("folder", job_id)
    try:
        os.remove(job_path(job_id))
    except FileNotFoundError:
        pass
    except OSError as exc:
        return False, f"Could not remove job {job_id}: {exc}"
    _progress_cache.pop(job_id, None)
    flog.log_info(None, "queue",
                  f"Job removed from queue: {job_id} "
                  f"('{title}', was {job.get('status')}). "
                  f"Source files and finished Plex output were NOT touched.",
                  outcome="success")
    _refresh_queue_positions()
    return True, f"Removed '{title}' from the queue."


def _enqueue_job(job: dict) -> dict:
    """Put a job in the build queue (status queued, position set),
    log it, and make sure the runner is going."""
    job["status"] = "queued"
    job["stage"] = "queued"
    job["progress_pct"] = 0
    job["error"] = ""
    job["cancel_requested"] = False
    save_job(job)
    _refresh_queue_positions()
    job = load_job(job["id"]) or job
    title = (job.get("metadata") or {}).get("title") or job.get("folder", job["id"])
    flog.log_info(job["id"], "queue",
                  f"Queued #{job.get('queue_position')} (one at a time): "
                  f"'{title}' — {len(job.get('files') or [])} file(s).",
                  outcome="success")
    _ensure_runner()
    return job


def _ensure_runner() -> None:
    global _runner_thread
    if _runner_thread is not None and _runner_thread.is_alive():
        return
    _runner_thread = threading.Thread(target=_queue_loop, daemon=True,
                                      name="cadence-queue-runner")
    _runner_thread.start()


def _queue_loop() -> None:
    """Runner: take the oldest queued job, build it, then the next.
    One build at a time — never parallel ffmpeg. Never dies on an
    unexpected error (logs it and retries)."""
    while True:
        try:
            if QUEUE_RUNNER_ENABLED:
                ordered = _queue_order()
                nxt = next((j for j in ordered if j.get("status") == "queued"), None)
                if nxt is not None:
                    if _is_cancel_requested(nxt["id"]):
                        _mark_cancelled(nxt["id"],
                                        "Cancel requested while queued.")
                    else:
                        _set_job_status(nxt["id"], "building", "scanning", 0)
                        _run_build(nxt["id"])
                    _refresh_queue_positions()
                    continue
            time.sleep(QUEUE_POLL_SECONDS)
        except Exception as exc:  # noqa: BLE001 — the runner must not die
            flog.log_error(None, "queue",
                           f"Queue runner error (will retry): {exc!r}",
                           outcome="FAIL")
            time.sleep(QUEUE_POLL_SECONDS)


def _requeue_interrupted_builds() -> None:
    """A restart mid-build must not wedge the queue: jobs left in
    'building' with no runner go back to 'queued'."""
    for job in list_jobs():
        if job.get("status") == "building":
            job["status"] = "queued"
            job["stage"] = "queued"
            job["progress_pct"] = 0
            job["cancel_requested"] = False
            save_job(job)
            flog.log_warn(job["id"], "queue",
                          "Build was interrupted by a restart — "
                          "re-queued instead of stuck on 'building'.",
                          outcome="warning")


_requeue_interrupted_builds()
_ensure_runner()


# ---------------------------------------------------------------------------
# Scanning / tags
# ---------------------------------------------------------------------------

def read_tags(path: str) -> dict:
    """Embedded track/disc numbers via mutagen. Never raises."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return {}
        def _num(key):
            vals = audio.get(key) or []
            if not vals:
                return None
            try:
                return int(str(vals[0]).split("/")[0])
            except (ValueError, IndexError):
                return None
        return {"track": _num("tracknumber"), "disc": _num("discnumber")}
    except Exception as exc:  # noqa: BLE001 — a tag read must never kill a scan
        flog.log_warn(None, "scan", f"Could not read tags from {path}: {exc!r}")
        return {}


def scan_folder(folder: str, job_id: str) -> list[dict]:
    """Walk one book folder, probe each audio file, log per-file outcome."""
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                found.append(os.path.join(root, name))
    flog.log_info(job_id, "scan",
                  f"Scan found {len(found)} audio file(s) in {folder}",
                  outcome="success" if found else "FAIL — no audio files")
    files = []
    for path in found:
        tags = read_tags(path)
        try:
            info = build_mod.probe(path)
            files.append({"path": path, "name": os.path.basename(path),
                          "mtime": os.path.getmtime(path),
                          "duration": info["duration"], "size": info["size"],
                          "codec": info["codec"], "sample_rate": info["sample_rate"],
                          "track": tags.get("track"), "disc": tags.get("disc"),
                          "outcome": "probed OK"})
            flog.log_info(job_id, "scan",
                          f"Probed OK: {os.path.basename(path)} "
                          f"({info['duration']:.1f}s, {info['codec']})",
                          filename=os.path.basename(path), outcome="success")
        except build_mod.BuildError as exc:
            # Corrupt / empty file: record it, flag it, do NOT skip silently.
            files.append({"path": path, "name": os.path.basename(path),
                          "mtime": os.path.getmtime(path),
                          "duration": 0, "size": os.path.getsize(path) if os.path.exists(path) else 0,
                          "codec": "unknown", "sample_rate": 44100,
                          "track": tags.get("track"), "disc": tags.get("disc"),
                          "outcome": f"PROBE FAILED: {exc}"})
            flog.log_error(job_id, "scan",
                           f"Probe FAILED for {os.path.basename(path)}: {exc}",
                           filename=os.path.basename(path), outcome="FAIL")
    return files


def _safe_join(base: str, rel: str) -> str | None:
    """Resolve rel under base; return None on path-traversal attempt."""
    if not base:
        return None
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, rel or ""))
    if os.path.commonpath([base_real, target]) != base_real:
        return None
    return target


def _create_job_record(folder: str) -> tuple[dict, str]:
    """Scan + auto-sort one folder into a job record (status
    'created', not yet queued). Returns (job, route) where route is
    'auto_queue' | 'review'. Raises ValueError on an empty folder —
    no empty job is ever created."""
    job_id = uuid.uuid4().hex[:12]
    flog.log_info(job_id, "scan", f"Job started for folder {folder}")
    files = scan_folder(folder, job_id)
    if not files:
        raise ValueError(
            f"No audio files found in {folder}. "
            f"Check the folder contains mp3/m4a/m4b/flac files.")
    result = auto_sort(files)
    for entry in result["ordered"]:
        flog.log_info(job_id, "sort",
                      f"Auto-sort #{entry['guessed_position']}: {entry['name']} — "
                      f"{entry['confidence']} confidence ({entry['reason']})",
                      filename=entry["name"], outcome="success")
    for w in result["warnings"]:
        flog.log_warn(job_id, "sort", f"Auto-sort warning: {w}", outcome="warning")
    route = queue_mod.route_for_count(len(files))
    metadata: dict = {"title": os.path.basename(folder.rstrip("/"))}
    if route == "auto_queue":
        # One file needs no sorting — guess the metadata from its
        # name now (ASIN lookup runs at build time, in the Metadata
        # stage); every field stays editable from the queue row
        # until the job starts.
        guess = queue_mod.guess_metadata_from_name(result["ordered"][0]["name"])
        metadata = {**metadata, **{k: v for k, v in guess.items() if v}}
        metadata["source"] = "filename-guess"
        result["ordered"][0]["reason"] = "Single file — no sorting needed"
        result["ordered"][0]["confidence"] = "high"
    job = {"id": job_id, "status": "created", "folder": folder,
           "created": datetime.now(timezone.utc).isoformat(),
           "files": result["ordered"], "warnings": result["warnings"],
           "sort_summary": result["summary"],
           "metadata": metadata,
           "metadata_source": metadata.get("source", "folder-name"),
           "auto_queued": route == "auto_queue",
           "needs_review": route == "review",
           "progress_pct": 0, "stage": "queued" if route == "auto_queue" else "created",
           "queue_position": None, "cancel_requested": False,
           "error": "", "cancelled_reason": ""}
    save_job(job)
    return job, route


def _create_single_file_job(path: str) -> dict:
    """A lone audio file picked on its own: one-file job, queued."""
    folder = os.path.dirname(path)
    job_id = uuid.uuid4().hex[:12]
    flog.log_info(job_id, "scan", f"Single-file job for {path}")
    tags = read_tags(path)
    try:
        info = build_mod.probe(path)
        entry = {"path": path, "name": os.path.basename(path),
                 "mtime": os.path.getmtime(path),
                 "duration": info["duration"], "size": info["size"],
                 "codec": info["codec"], "sample_rate": info["sample_rate"],
                 "track": tags.get("track"), "disc": tags.get("disc"),
                 "guessed_position": 1, "confidence": "high",
                 "reason": "Single file — no sorting needed",
                 "outcome": "probed OK"}
    except build_mod.BuildError as exc:
        entry = {"path": path, "name": os.path.basename(path),
                 "mtime": os.path.getmtime(path),
                 "duration": 0, "size": os.path.getsize(path) if os.path.exists(path) else 0,
                 "codec": "unknown", "sample_rate": 44100,
                 "track": tags.get("track"), "disc": tags.get("disc"),
                 "guessed_position": 1, "confidence": "high",
                 "reason": "Single file — no sorting needed",
                 "outcome": f"PROBE FAILED: {exc}"}
        flog.log_error(job_id, "scan",
                       f"Probe FAILED for {os.path.basename(path)}: {exc}",
                       filename=os.path.basename(path), outcome="FAIL")
    guess = queue_mod.guess_metadata_from_name(os.path.basename(path))
    job = {"id": job_id, "status": "created", "folder": folder,
           "created": datetime.now(timezone.utc).isoformat(),
           "files": [entry], "warnings": [], "sort_summary": {},
           "metadata": {**{"title": os.path.splitext(os.path.basename(path))[0]},
                        **{k: v for k, v in guess.items() if v},
                        "source": "filename-guess"},
           "metadata_source": "filename-guess",
           "auto_queued": True, "needs_review": False,
           "progress_pct": 0, "stage": "queued",
           "queue_position": None, "cancel_requested": False,
           "error": "", "cancelled_reason": ""}
    save_job(job)
    return job


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/logs/<job_id>/text")
def job_text_view(job_id):
    """Plain-text view, no site chrome: long-press > Select All > Copy
    on a phone. Lines wrap (pre-wrap) so iOS select-all works cleanly."""
    job = load_job(job_id)
    if job is None and job_id != "app":
        return Response("Job not found.\n", status=404, mimetype="text/plain")
    text = flog.build_pasteable_log(DATA_DIR, job_id, job) if job else ""
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Log — {job_id}</title></head>"
            "<body style='margin:0;padding:12px;font-family:monospace;font-size:14px;"
            "line-height:1.45;background:#fff;color:#111'>"
            "<pre style='white-space:pre-wrap;word-wrap:break-word;margin:0'>"
            + _html_escape(text) + "</pre></body></html>")
    return Response(html, mimetype="text/html")


@app.route("/logs/app/text")
def global_text_view():
    text = flog.build_global_pasteable_log(DATA_DIR)
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Global log — Cadence</title></head>"
            "<body style='margin:0;padding:12px;font-family:monospace;font-size:14px;"
            "line-height:1.45;background:#fff;color:#111'>"
            "<pre style='white-space:pre-wrap;word-wrap:break-word;margin:0'>"
            + _html_escape(text) + "</pre></body></html>")
    return Response(html, mimetype="text/html")


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# API — health / setup / folders / settings
# ---------------------------------------------------------------------------

@app.route("/api/health")
def health():
    status = setup_status()
    active = _queue_order()
    review_awaiting = [j for j in list_jobs()
                       if j.get("status") == "needs_health_review"]
    return jsonify({"status": "ok" if not STARTUP_ERRORS else "degraded",
                    "startup_errors": STARTUP_ERRORS,
                    "setup_required": status["setup_required"],
                    "setup_errors": status["setup_errors"],
                    "settings": SETTINGS,
                    "queue": {"queued": sum(1 for j in active if j["status"] == "queued"),
                              "building": sum(1 for j in active if j["status"] == "building")},
                    "review_awaiting": len(review_awaiting),
                    "ffmpeg": shutil.which("ffmpeg"),
                    "ffprobe": shutil.which("ffprobe")})


@app.route("/api/folders")
def folders_api():
    """Server-side folder picker: list subfolders of an absolute
    server path (phone-friendly — tap to descend, no typing)."""
    path = os.path.realpath(request.args.get("path", "/") or "/")
    if not os.path.isabs(path) or not os.path.isdir(path):
        return jsonify({"ok": False,
                        "error": f"Folder not found on this server: {path}"}), 404
    entries = []
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Cannot list {path}: {exc}"}), 403
    for name in names:
        full = os.path.join(path, name)
        if os.path.isdir(full) and not name.startswith("."):
            try:
                audio = sum(1 for f in os.listdir(full)
                            if os.path.isfile(os.path.join(full, f))
                            and os.path.splitext(f)[1].lower() in AUDIO_EXTS)
            except OSError:
                audio = 0
            entries.append({"name": name, "path": full, "audio_files": audio})
    parent = os.path.dirname(path.rstrip("/")) or "/"
    return jsonify({"ok": True, "path": path, "parent": parent,
                    "entries": entries})


@app.route("/api/folders/suggestions")
def folder_suggestions():
    """One-tap path suggestions for the setup wizard: generic
    candidates that actually exist on this server, plus anything
    already configured. Never assumes a particular disk layout."""
    candidates = ["/audiobooks", "/audiobooks/in", "/audiobooks/out",
                  "/audiobooks/processed", "/audiobooks/review",
                  "/data", "/data/audiobooks", "/data/inbox", "/data/processed",
                  "/data/review",
                  "/media", "/mnt", "/srv", "/home"]
    for env_key in ("INBOX_DIR", "OUTPUT_DIR"):
        val = os.environ.get(env_key)
        if val:
            candidates.append(val)
    for key in ("inbox_dir", "output_dir"):
        val = SETTINGS.get(key)
        if val:
            candidates.append(val)
    seen: set[str] = set()
    suggestions = []
    for cand in candidates:
        cand = os.path.realpath(cand)
        if cand in seen or not os.path.isdir(cand):
            continue
        seen.add(cand)
        base = os.path.basename(cand.rstrip("/")).lower()
        if base in ("in", "inbox", "input"):
            label = "Inbox (input) folder"
        elif base in ("out", "output", "audiobooks"):
            label = "Audiobooks library folder"
        elif base == "review":
            label = "Review folder (health-held files)"
        elif base == "processed":
            label = "Processed folder (originals)"
        else:
            label = "Folder on this server"
        suggestions.append({"path": cand, "label": label})
    suggestions.append({"path": "/", "label": "Server root — browse from here"})
    return jsonify({"ok": True, "suggestions": suggestions})


@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    global SETTINGS, STARTUP_ERRORS
    if request.method == "GET":
        status = setup_status()
        return jsonify({**SETTINGS,
                        "setup_required": status["setup_required"],
                        "setup_errors": status["setup_errors"]})
    data = request.get_json(force=True) or {}
    was_required = setup_status()["setup_required"]
    merged = dict(SETTINGS)
    for key in ("inbox_dir", "output_dir", "processed_dir", "review_dir",
                "temp_dir", "bitrate",
                "audnexus_url", "keep_originals", "timezone"):
        if key in data and str(data[key]).strip():
            merged[key] = str(data[key]).strip()
    # Explicitly clearing the Processed / Review path means "use the default".
    if "processed_dir" in data and not str(data["processed_dir"]).strip():
        merged["processed_dir"] = ""
    if "review_dir" in data and not str(data["review_dir"]).strip():
        merged["review_dir"] = ""
    # Migrate legacy values the same way load_settings does.
    if merged.get("keep_originals") == "move_done":
        merged["keep_originals"] = "processed"
    elif merged.get("keep_originals") == "delete":
        merged["keep_originals"] = "keep"
    create_output = bool(data.get("create_output"))
    errors = validate_paths(merged.get("inbox_dir", ""),
                            merged.get("output_dir", ""),
                            create_output=create_output)
    # Processed folder: create it on save when its parent allows it.
    processed = (merged.get("processed_dir") or "").strip()
    if processed and not os.path.isdir(processed):
        parent = os.path.dirname(processed.rstrip("/")) or "/"
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            try:
                os.makedirs(processed, exist_ok=True)
            except OSError as exc:
                errors.append({"field": "processed_dir",
                               "message": f"Could not create Processed folder "
                                          f"{processed}: {exc}"})
        else:
            errors.append({"field": "processed_dir",
                           "message": f"Processed folder does not exist and "
                                      f"cannot be created: {processed}."})
    # Review folder: same treatment — and it must stay OUTSIDE the
    # Plex output tree, or Plex would scan held, unchecked files.
    review = (merged.get("review_dir") or "").strip()
    output = (merged.get("output_dir") or "").strip()
    if review and output and _is_inside(output, review):
        errors.append({"field": "review_dir",
                       "message": "Review folder must be OUTSIDE the Plex "
                                  f"output folder — otherwise Plex would scan "
                                  f"held, unchecked files."})
    elif review and not os.path.isdir(review):
        parent = os.path.dirname(review.rstrip("/")) or "/"
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            try:
                os.makedirs(review, exist_ok=True)
            except OSError as exc:
                errors.append({"field": "review_dir",
                               "message": f"Could not create Review folder "
                                          f"{review}: {exc}"})
        else:
            errors.append({"field": "review_dir",
                           "message": f"Review folder does not exist and "
                                      f"cannot be created: {review}."})
    tz = merged.get("timezone") or "UTC"
    try:
        ZoneInfo(tz)
    except Exception:
        errors.append({"field": "timezone",
                       "message": f"Unknown timezone: {tz}. Use a valid "
                                  f"timezone name (e.g. UTC, America/New_York)."})
    if errors:
        # Rejected, clearly — bad paths are never silently saved.
        flog.log_warn(None, "settings",
                      "Settings save rejected: "
                      + "; ".join(e["message"] for e in errors),
                      outcome="FAIL")
        return jsonify({"ok": False, "errors": errors,
                        "error": errors[0]["message"]}), 400
    SETTINGS = merged
    save_settings(SETTINGS)
    if not flog.set_timezone(SETTINGS.get("timezone") or "UTC"):
        flog.log_warn(None, "settings",
                      f"Timezone {SETTINGS.get('timezone')!r} not applied — using UTC.",
                      outcome="warning")
    STARTUP_ERRORS = run_startup_checks()
    flog.log_info(None, "settings", f"Settings updated: {SETTINGS}", outcome="success")
    if was_required and not setup_status()["setup_required"]:
        flog.log_info(None, "setup",
                      f"First-run setup completed: inbox={SETTINGS['inbox_dir']} "
                      f"output={SETTINGS['output_dir']} "
                      f"processed={_processed_dir()} "
                      f"review={_review_dir()} "
                      f"timezone={SETTINGS.get('timezone', 'UTC')}",
                      outcome="success")
    status = setup_status()
    return jsonify({"ok": True, "settings": SETTINGS,
                    "startup_errors": STARTUP_ERRORS,
                    "setup_required": status["setup_required"],
                    "setup_errors": status["setup_errors"]})


# ---------------------------------------------------------------------------
# API — browse (setup guard: no paths, no browsing)
# ---------------------------------------------------------------------------

@app.route("/api/browse")
def browse():
    if setup_status()["setup_required"]:
        return jsonify({"ok": False, "setup_required": True,
                        "error": "Finish first-run setup first — pick an "
                                 "inbox and an output folder in the setup wizard."}), 400
    rel = request.args.get("path", "")
    target = _safe_join(SETTINGS["inbox_dir"], rel)
    if target is None or not os.path.isdir(target):
        flog.log_warn(None, "browse", f"Browse rejected/invalid path: rel={rel!r}")
        return jsonify({"ok": False, "error": f"Folder not found under inbox: {rel}"}), 404
    entries = []
    for name in sorted(os.listdir(target)):
        full = os.path.join(target, name)
        if os.path.isdir(full):
            count = sum(1 for _r, _d, fs in os.walk(full)
                        for f in fs if os.path.splitext(f)[1].lower() in AUDIO_EXTS)
            entries.append({"name": name, "type": "folder", "rel": os.path.join(rel, name),
                            "audio_files": count})
        elif os.path.splitext(name)[1].lower() in AUDIO_EXTS:
            entries.append({"name": name, "type": "file", "rel": os.path.join(rel, name),
                            "size": os.path.getsize(full)})
    return jsonify({"ok": True, "path": rel or "/", "inbox": SETTINGS["inbox_dir"],
                    "entries": entries})


# ---------------------------------------------------------------------------
# API — jobs (routing: 1 file -> auto-queue, >1 -> review, 0 -> error)
# ---------------------------------------------------------------------------

@app.route("/api/jobs", methods=["GET", "POST"])
def jobs_api():
    if request.method == "GET":
        jobs = [_with_live_position(j) for j in list_jobs()]
        return jsonify({"ok": True, "jobs": [
            {k: v for k, v in j.items() if k != "log"} for j in jobs]})
    if setup_status()["setup_required"]:
        return jsonify({"ok": False, "setup_required": True,
                        "error": "Finish first-run setup first — pick an "
                                 "inbox and an output folder in the setup wizard."}), 400
    data = request.get_json(force=True) or {}
    # A lone file picked on its own: straight to the queue.
    if data.get("file"):
        rel = (data.get("file") or "").strip()
        path = _safe_join(SETTINGS["inbox_dir"], rel)
        if path is None or not os.path.isfile(path):
            return jsonify({"ok": False, "error": f"File not found under inbox: {rel}"}), 404
        job = _create_single_file_job(path)
        job = _enqueue_job(job)
        flog.log_info(job["id"], "queue",
                      f"Single file auto-queued: {os.path.basename(path)} "
                      f"(1 file — no sorting needed).",
                      filename=os.path.basename(path), outcome="success")
        return jsonify({"ok": True, "job": _with_live_position(job),
                        "route": "auto_queue"})
    rel = (data.get("folder") or "").strip()
    folder = _safe_join(SETTINGS["inbox_dir"], rel)
    if folder is None or not os.path.isdir(folder):
        return jsonify({"ok": False, "error": f"Folder not found under inbox: {rel}"}), 404
    try:
        job, route = _create_job_record(folder)
    except ValueError as exc:
        # Empty folder: clear error, NO empty job is created.
        flog.log_warn(None, "scan", str(exc), outcome="FAIL")
        return jsonify({"ok": False, "error": str(exc)}), 400
    if route == "auto_queue":
        job = _enqueue_job(job)
        flog.log_info(job["id"], "queue",
                      f"Folder auto-queued: {folder} "
                      f"(exactly 1 audio file — no sorting needed).",
                      outcome="success")
        return jsonify({"ok": True, "job": _with_live_position(job),
                        "route": "auto_queue"})
    return jsonify({"ok": True, "job": _with_live_position(job),
                    "route": "review"})


@app.route("/api/jobs/<job_id>")
def job_api(job_id):
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    return jsonify({"ok": True, "job": _with_live_position(job)})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def job_delete(job_id):
    ok, msg = _remove_job(job_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/jobs/<job_id>/metadata", methods=["POST"])
def job_metadata(job_id):
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    data = request.get_json(force=True) or {}
    asin = meta_mod.extract_asin(data.get("asin") or "") or (data.get("asin") or "").strip()
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    api_url = SETTINGS["audnexus_url"]
    flog.log_info(job_id, "metadata",
                  f"Metadata lookup requested: asin={asin or '(none)'} "
                  f"title={title or '(none)'} author={author or '(none)'} "
                  f"api={api_url}")
    try:
        if asin:
            result = meta_mod.fetch_by_asin(asin, api_url)
        elif title:
            result = meta_mod.search(title, author, api_url)
        else:
            raise ValueError("Give an ASIN or a title to look up — nothing supplied.")
        flog.log_info(job_id, "metadata",
                      f"audnexus lookup OK: '{result.get('title')}' by "
                      f"{result.get('author')} (ASIN {result.get('asin')}, "
                      f"series: {result.get('series')} {result.get('series_position')}, "
                      f"year {result.get('year')})",
                      outcome="success",
                      extra={"asin": result.get("asin"), "api_url": api_url})
        job["metadata"] = {**job.get("metadata", {}), **result}
        job["metadata_source"] = "audnexus"
        save_job(job)
        return jsonify({"ok": True, "metadata": result, "job": job})
    except Exception as exc:  # noqa: BLE001 — exact failure must reach the UI
        reason = (f"Metadata lookup failed (asin={asin or 'none'}, "
                  f"title={title or 'none'}, api={api_url}): {exc}")
        flog.log_error(job_id, "metadata", reason, outcome="FAIL")
        # Lookup failure never blocks the job: manual fields stay usable.
        job["metadata_error"] = reason
        save_job(job)
        return jsonify({"ok": False, "error": reason,
                        "fallback": "manual — edit the fields below and build anyway."}), 502


@app.route("/api/jobs/<job_id>/fields", methods=["POST"])
def job_fields(job_id):
    """Edit a queued / in-review job's metadata or file order
    before it starts building. (The queue row's Edit button.)"""
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    if job.get("status") not in ("created", "queued"):
        return jsonify({"ok": False,
                        "error": f"Job is {job.get('status')} — it can no "
                                 f"longer be edited."}), 409
    data = request.get_json(force=True) or {}
    if isinstance(data.get("metadata"), dict):
        job["metadata"] = {**job.get("metadata", {}), **data["metadata"]}
        job["metadata_source"] = "manual"
    if isinstance(data.get("files"), list) and data["files"]:
        job["files"] = data["files"]
    save_job(job)
    flog.log_info(job_id, "queue",
                  "Job details edited before building "
                  f"(title='{job.get('metadata', {}).get('title')}', "
                  f"{len(job.get('files') or [])} file(s)).",
                  outcome="success")
    return jsonify({"ok": True, "job": _with_live_position(job)})


@app.route("/api/jobs/<job_id>/confirm", methods=["POST"])
def job_confirm(job_id):
    """Confirm the reviewed order/metadata: the job joins the build
    queue (it does NOT start immediately — one build at a time)."""
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    if job.get("status") in ("queued", "building"):
        return jsonify({"ok": False, "error": "Job is already queued/building."}), 409
    if job.get("status") == "done":
        return jsonify({"ok": False, "error": "Job is already done."}), 409
    if job.get("status") == "cancelled":
        return jsonify({"ok": False, "error": "Job was cancelled — start it again from Browse."}), 409
    data = request.get_json(force=True) or {}
    ordered = data.get("files") or job.get("files") or []
    metadata = data.get("metadata") or job.get("metadata") or {}
    if not ordered:
        return jsonify({"ok": False, "error": "No files in the confirmed order."}), 400
    if not (metadata.get("title") or "").strip():
        return jsonify({"ok": False, "error": "A book title is required before building."}), 400
    # The confirmed order is the truth from here on.
    job["files"] = ordered
    job["metadata"] = metadata
    job["needs_review"] = False
    job["error"] = ""
    job["cancel_requested"] = False
    save_job(job)
    flog.log_info(job_id, "build",
                  f"Order confirmed by user: {len(ordered)} file(s), "
                  f"title='{metadata.get('title')}' author='{metadata.get('author')}' "
                  f"— joining the build queue.",
                  outcome="success")
    job = _enqueue_job(job)
    return jsonify({"ok": True, "job": _with_live_position(job)})


# ---------------------------------------------------------------------------
# API — the queue itself
# ---------------------------------------------------------------------------

@app.route("/api/queue")
def queue_api():
    _ensure_runner()
    active = _queue_order()
    queue = []
    for i, job in enumerate(active):
        job = dict(job)
        job["queue_position"] = i + 1
        queue.append(job)
    review = [j for j in list_jobs() if j.get("status") == "created"]
    review.sort(key=lambda j: j.get("created", ""))
    return jsonify({"ok": True, "queue": queue, "needs_review": review,
                    "counts": {"queued": sum(1 for j in queue if j["status"] == "queued"),
                               "building": sum(1 for j in queue if j["status"] == "building"),
                               "needs_review": len(review)}})


@app.route("/api/queue", methods=["POST"])
def queue_bulk_api():
    """Queue lots of items at once: one queue entry per book/folder.
    Single-file items auto-queue; multi-file items are created in
    review state (they still need their order checked) and join the
    build queue once confirmed. Empty folders are reported, not queued."""
    if setup_status()["setup_required"]:
        return jsonify({"ok": False, "setup_required": True,
                        "error": "Finish first-run setup first."}), 400
    data = request.get_json(force=True) or {}
    items = data.get("items")
    if items is None:
        items = ([{"folder": data["folder"]}] if data.get("folder")
                 else ([{"file": data["file"]}] if data.get("file") else []))
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "Nothing to queue — pick at least one folder or file."}), 400
    results = []
    queued_n = 0
    review_n = 0
    for item in items:
        if not isinstance(item, dict):
            results.append({"item": item, "route": "error",
                            "error": "Bad queue item (expected {folder} or {file})."})
            continue
        if item.get("file"):
            rel = str(item["file"]).strip()
            path = _safe_join(SETTINGS["inbox_dir"], rel)
            if path is None or not os.path.isfile(path):
                results.append({"item": rel, "route": "error",
                                "error": f"File not found under inbox: {rel}"})
                continue
            job = _create_single_file_job(path)
            job = _enqueue_job(job)
            queued_n += 1
            results.append({"item": rel, "route": "auto_queue", "job_id": job["id"],
                            "title": (job.get("metadata") or {}).get("title")})
            continue
        rel = str(item.get("folder") or "").strip()
        folder = _safe_join(SETTINGS["inbox_dir"], rel)
        if folder is None or not os.path.isdir(folder):
            results.append({"item": rel, "route": "error",
                            "error": f"Folder not found under inbox: {rel}"})
            continue
        try:
            job, route = _create_job_record(folder)
        except ValueError as exc:
            results.append({"item": rel, "route": "empty", "error": str(exc)})
            flog.log_warn(None, "queue",
                          f"Bulk queue skipped empty folder {rel}: {exc}",
                          outcome="warning")
            continue
        if route == "auto_queue":
            job = _enqueue_job(job)
            queued_n += 1
            results.append({"item": rel, "route": "auto_queue", "job_id": job["id"],
                            "title": (job.get("metadata") or {}).get("title")})
        else:
            review_n += 1
            results.append({"item": rel, "route": "review", "job_id": job["id"],
                            "title": (job.get("metadata") or {}).get("title"),
                            "note": "Needs order review — open it from the queue to check, then confirm."})
    flog.log_info(None, "queue",
                  f"Bulk queue: {len(items)} item(s) picked — {queued_n} auto-queued, "
                  f"{review_n} need order review, "
                  f"{len(results) - queued_n - review_n} skipped/failed.",
                  outcome="success")
    return jsonify({"ok": True, "results": results,
                    "queued": queued_n, "needs_review": review_n})


@app.route("/api/queue/<job_id>/cancel", methods=["POST"])
def queue_cancel_api(job_id):
    ok, msg = _cancel_job(job_id)
    job = load_job(job_id)
    return jsonify({"ok": ok, "message": msg,
                    "job": _with_live_position(job) if job else None}), \
        (200 if ok else 400)


@app.route("/api/queue/<job_id>", methods=["DELETE"])
def queue_delete(job_id):
    ok, msg = _remove_job(job_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/queue/clear", methods=["POST"])
def queue_clear_api():
    """Bulk clear: which=finished (done+failed+cancelled), done, failed,
    or cancelled. Only job records are removed — never source files,
    never finished Plex output."""
    data = request.get_json(force=True) or {}
    which = (data.get("which") or "finished").strip().lower()
    wanted = {"done", "failed", "cancelled"} if which == "finished" \
        else {which}
    if not wanted <= {"done", "failed", "cancelled"}:
        return jsonify({"ok": False,
                        "error": f"Unknown clear target: {which} "
                                 f"(use finished, done, failed, or cancelled)."}), 400
    removed = 0
    for job in list(list_jobs()):
        if job.get("status") in wanted:
            ok, _msg = _remove_job(job["id"])
            if ok:
                removed += 1
    flog.log_info(None, "queue",
                  f"Cleared {removed} {which} job(s) from the queue. "
                  f"Source files and Plex output were NOT touched.",
                  outcome="success")
    return jsonify({"ok": True, "removed": removed, "which": which})


# ---------------------------------------------------------------------------
# API — cleanup (safe-to-delete processed originals)
# ---------------------------------------------------------------------------

def _kept_path() -> str:
    return os.path.join(DATA_DIR, "cleanup-kept.json")


def _load_kept() -> set[str]:
    try:
        with open(_kept_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(p) for p in data} if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def _save_kept(kept: set[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_kept_path(), "w", encoding="utf-8") as fh:
        json.dump(sorted(kept), fh, indent=2)


def _cleanup_scan(show_kept: bool = False) -> dict:
    """Run the matcher and split out kept items (kept items never
    re-appear on the safe list unless show_kept is on)."""
    scan = cleanup_mod.scan_cleanup(_processed_dir(),
                                    SETTINGS.get("output_dir", ""),
                                    list_jobs())
    kept = _load_kept()
    kept_items = [i for i in scan["safe"] if i["processed_path"] in kept]
    scan["safe"] = [i for i in scan["safe"] if i["processed_path"] not in kept]
    scan["total_reclaimable_bytes"] = sum(
        i.get("processed_size", 0) for i in scan["safe"])
    scan["total_reclaimable_count"] = len(scan["safe"])
    scan["kept"] = kept_items if show_kept else []
    scan["kept_count"] = len(kept_items)
    scan["processed_dir"] = _processed_dir()
    return scan


@app.route("/api/cleanup")
def cleanup_api():
    if setup_status()["setup_required"]:
        return jsonify({"ok": False, "setup_required": True,
                        "error": "Finish first-run setup first."}), 400
    show_kept = request.args.get("show_kept") in ("1", "true", "yes")
    scan = _cleanup_scan(show_kept=show_kept)
    return jsonify({"ok": True, **scan})


@app.route("/api/cleanup/keep", methods=["POST"])
def cleanup_keep_api():
    """Take one safe item OFF the delete list (Keep) — or put it
    back (Un-keep). Kept items stay in Processed and are excluded
    from this and later cleanup runs until un-kept."""
    data = request.get_json(force=True) or {}
    path = (data.get("processed_path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "No processed path given."}), 400
    kept = _load_kept()
    if data.get("keep", True):
        kept.add(path)
        action = "kept (removed from the delete list)"
    else:
        kept.discard(path)
        action = "un-kept (back on the delete list if still safe)"
    _save_kept(kept)
    flog.log_info(None, "cleanup", f"Processed item {action}: {path}",
                  outcome="success")
    return jsonify({"ok": True, "kept_count": len(kept)})


@app.route("/api/cleanup/delete", methods=["POST"])
def cleanup_delete_api():
    """Delete the listed safe items — but ONLY after re-verifying
    each one against a fresh scan (the list on the phone may be
    stale). Stops at the first failure and reports it, leaving the
    rest untouched. Only paths inside Processed are ever deleted."""
    data = request.get_json(force=True) or {}
    paths = data.get("processed_paths") or []
    if not isinstance(paths, list) or not paths:
        return jsonify({"ok": False,
                        "error": "Nothing to delete — the safe list is empty."}), 400
    processed_dir = _processed_dir()
    scan = _cleanup_scan(show_kept=False)
    safe_by_path = {i["processed_path"]: i for i in scan["safe"]}
    # Re-verify every requested path against the fresh safe list,
    # in the order given; stop at the first problem.
    to_delete = []
    for path in paths:
        if path not in safe_by_path:
            flog.log_error(None, "cleanup",
                           f"Delete refused: {path} is not on the current "
                           f"safe list (re-check it first) — stopping, "
                           f"nothing deleted for this or later items.",
                           outcome="FAIL")
            return jsonify({"ok": False,
                            "error": f"Delete refused: {path} is not on the "
                                     f"current safe list. Re-check the list "
                                     f"and try again — nothing was deleted.",
                            "failed": {"path": path,
                                       "reason": "Not on the current safe list."},
                            "deleted": [], "freed_bytes": 0}), 400
        to_delete.append(safe_by_path[path])
    result = cleanup_mod.delete_processed_items(processed_dir, to_delete)
    for item in result["deleted"]:
        flog.log_info(None, "cleanup",
                      f"Deleted processed originals: {item['path']} "
                      f"({item['size'] / 1e6:.1f} MB freed) — "
                      f"'{item.get('title', '')}' by {item.get('author', '')}. "
                      f"Completed .m4b in the output tree was NOT touched.",
                      outcome="success")
    if result["failed"] is not None:
        flog.log_error(None, "cleanup",
                       f"Cleanup stopped: {result['failed']['reason']} "
                       f"({len(result['deleted'])} item(s) were already deleted).",
                       outcome="FAIL")
        return jsonify({"ok": False, "error": result["failed"]["reason"],
                        "failed": result["failed"],
                        "deleted": result["deleted"],
                        "freed_bytes": result["freed_bytes"]}), 500
    flog.log_info(None, "cleanup",
                  f"Cleanup deleted {len(result['deleted'])} item(s), "
                  f"freed {result['freed_bytes'] / 1e6:.1f} MB.",
                  outcome="success")
    return jsonify({"ok": True, "deleted": result["deleted"],
                    "freed_bytes": result["freed_bytes"],
                    "failed": None})


# ---------------------------------------------------------------------------
# API — health review (quarantined files awaiting a human decision)
# ---------------------------------------------------------------------------

def _review_jobs() -> list[dict]:
    held = [j for j in list_jobs()
            if j.get("status") == "needs_health_review"]
    held.sort(key=lambda j: j.get("held_at") or j.get("created", ""))
    return held


@app.route("/api/review")
def review_api():
    """Held items: finished files the health check would not let
    into Plex, with their verdict + diagnoses attached."""
    jobs = []
    for job in _review_jobs():
        report = health_mod.load_health_report(JOBS_DIR, job["id"])
        jobs.append({**job, "health_report": report})
    return jsonify({"ok": True, "review": jobs,
                    "count": len(jobs),
                    "review_dir": _review_dir()})


@app.route("/api/jobs/<job_id>/health")
def job_health_api(job_id):
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    report = health_mod.load_health_report(JOBS_DIR, job_id)
    if report is None and job.get("health"):
        report = {"verdict": job["health"].get("verdict"),
                  "diagnoses": job["health"].get("diagnoses", []),
                  "limited": job["health"].get("limited", False),
                  "chapters": [], "checks": []}
    if report is None:
        return jsonify({"ok": False,
                        "error": "No health report for this job yet — "
                                 "it has not finished building."}), 404
    return jsonify({"ok": True, "job_id": job_id, "job": job,
                    "health": report})


@app.route("/api/jobs/<job_id>/health/rerun", methods=["POST"])
def job_health_rerun_api(job_id):
    """Re-run the health check on the held file (cheap: it probes
    the Review file and re-compares against the recorded inputs
    and metadata). It updates the report but never auto-places —
    the file still waits for a human approve or reject."""
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    if job.get("status") != "needs_health_review":
        return jsonify({"ok": False,
                        "error": f"Job is {job.get('status')} — only a held "
                                 f"job can have its health check re-run."}), 409
    review_path = job.get("review_path") or ""
    if not review_path or not os.path.isfile(review_path):
        return jsonify({"ok": False,
                        "error": f"Held file is gone: {review_path or '(no path recorded)'}. "
                                 f"Re-process the book from its original source folder."}), 404
    report = health_mod.run_health_check(
        review_path, job.get("files") or [], job.get("metadata") or {})
    health_mod.save_health_report(JOBS_DIR, job_id, report)
    job["health"] = {
        "verdict": report["verdict"], "limited": report["limited"],
        "expected_total": report["expected_total"],
        "actual_total": report["actual_total"],
        "missing": report["missing"],
        "diagnoses": report["diagnoses"]}
    save_job(job)
    flog.log_info(job_id, "health",
                  f"Health check re-run on the held file: "
                  f"{report['verdict'].upper()} — expected "
                  f"{report['expected_total']}, got "
                  f"{report['actual_total']}. "
                  + "; ".join(report["diagnoses"]),
                  outcome="success" if report["verdict"] in ("pass", "limited")
                  else "warning")
    return jsonify({"ok": True, "job": job, "health": report})


@app.route("/api/jobs/<job_id>/approve", methods=["POST"])
def job_approve_api(job_id):
    """Approve / "Looks OK, place it": move the held .m4b from
    Review to its correct Plex location (plus cover.jpg), mark the
    job Done, and record WHICH warning was overridden — the
    approval is on the record, not silent. Success path (2): the
    .m4b is in Plex now, so ONLY at this point do the originals
    move to Processed (same move as a passing build). If a move
    fails midway the job is still Done — with a warning naming the
    unmoved files — never Failed, since Plex already has the book."""
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    if job.get("status") != "needs_health_review":
        return jsonify({"ok": False,
                        "error": f"Job is {job.get('status')} — only a held "
                                 f"job can be approved."}), 409
    review_path = job.get("review_path") or ""
    dest = job.get("output_path") or ""
    if not review_path or not os.path.isfile(review_path):
        return jsonify({"ok": False,
                        "error": f"Held file is gone: {review_path or '(no path recorded)'}. "
                                 f"Re-process the book from its original source folder."}), 404
    if not dest or not _is_inside(SETTINGS.get("output_dir", ""), dest):
        return jsonify({"ok": False,
                        "error": f"Refusing to place the file outside the "
                                 f"Plex output folder: {dest or '(no destination recorded)'}"}), 400
    if not _is_inside(_review_dir(), review_path):
        return jsonify({"ok": False,
                        "error": f"Refusing: held file is not inside the "
                                 f"Review folder: {review_path}"}), 400
    data = request.get_json(force=True, silent=True) or {}
    note = (data.get("note") or "").strip()
    report = health_mod.load_health_report(JOBS_DIR, job_id) or {}
    overridden = report.get("diagnoses") or (job.get("health") or {}).get("diagnoses") or []
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            flog.log_warn(job_id, "health",
                          f"Approve overwrote an existing file in Plex: {dest}",
                          outcome="warning")
        os.replace(review_path, dest)
    except OSError as exc:
        flog.log_error(job_id, "health",
                       f"Approve FAILED moving {review_path} to {dest}: {exc}",
                       outcome="FAIL")
        return jsonify({"ok": False,
                        "error": f"Could not move the file into Plex: {exc}"}), 500
    review_cover = job.get("review_cover_path") or ""
    if review_cover and os.path.isfile(review_cover):
        try:
            shutil.copyfile(review_cover, plex_output.cover_path_for(dest))
        except OSError as exc:
            flog.log_warn(job_id, "health",
                          f"Approved, but cover.jpg could not be copied: {exc}",
                          outcome="warning")
        try:
            os.remove(review_cover)
        except OSError:
            pass
    job["status"] = "done"
    job["stage"] = "done"
    job["progress_pct"] = 100
    job["finished_at"] = datetime.now(timezone.utc).isoformat()
    job["approved_override"] = {
        "at": job["finished_at"],
        "verdict_overridden": report.get("verdict"),
        "diagnoses_overridden": overridden,
        "note": note,
    }
    job["review_path"] = ""
    # Success path (2): the held file is in Plex now — ONLY now do
    # the originals move to Processed. Until this moment they stayed
    # in the original source folder while the file sat in Review.
    _approve_log = lambda stage, level, msg, **kw: flog.log_event(
        job_id, stage, level, msg, **kw)
    move_warning, _moved, _failed = _move_originals_to_processed(
        job_id, job.get("files") or [], job.get("metadata") or {},
        job.get("folder", ""), _approve_log)
    job["originals_warning"] = move_warning or ""
    job["warning"] = move_warning or ""
    save_job(job)
    if move_warning:
        flog.log_warn(job_id, "health",
                      f"Job DONE with a warning: {move_warning}",
                      outcome="warning")
    flog.log_info(job_id, "health",
                  f"APPROVED by user despite health verdict "
                  f"{report.get('verdict', '?').upper()}: {review_path} -> "
                  f"{dest}. Overrode: {'; '.join(overridden) or '(no flags)'}"
                  + (f" Note: {note}" if note else ""),
                  outcome="success")
    return jsonify({"ok": True, "job": job})


@app.route("/api/jobs/<job_id>/reject", methods=["POST"])
def job_reject_api(job_id):
    """Reject: delete the held temp .m4b (confirmed in the UI
    first). The originals REMAIN in their original source location
    — nothing ever moved to Processed for a held book — so the book
    can be re-processed from the source folder. The job becomes
    Rejected — never Done, never silently gone."""
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    if job.get("status") != "needs_health_review":
        return jsonify({"ok": False,
                        "error": f"Job is {job.get('status')} — only a held "
                                 f"job can be rejected."}), 409
    review_path = job.get("review_path") or ""
    if not review_path or not _is_inside(_review_dir(), review_path):
        return jsonify({"ok": False,
                        "error": f"Refusing: held file is not inside the "
                                 f"Review folder: {review_path or '(no path recorded)'}"}), 400
    size = os.path.getsize(review_path) if os.path.isfile(review_path) else 0
    try:
        if os.path.isfile(review_path):
            os.remove(review_path)
    except OSError as exc:
        flog.log_error(job_id, "health",
                       f"Reject FAILED deleting {review_path}: {exc}",
                       outcome="FAIL")
        return jsonify({"ok": False,
                        "error": f"Could not delete the held file: {exc}"}), 500
    review_cover = job.get("review_cover_path") or ""
    if review_cover and os.path.isfile(review_cover):
        try:
            os.remove(review_cover)
        except OSError:
            pass
    # Remove the now-empty Review subfolders for this book.
    parent = os.path.dirname(review_path)
    for _ in range(3):
        try:
            if parent and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
            else:
                break
        except OSError:
            break
    job["status"] = "rejected"
    job["stage"] = "rejected"
    job["progress_pct"] = 100
    job["finished_at"] = datetime.now(timezone.utc).isoformat()
    job["rejected_size"] = size
    job["review_path"] = ""
    save_job(job)
    flog.log_info(job_id, "health",
                  f"REJECTED by user: held file deleted: {review_path} "
                  f"({size / 1e6:.1f} MB freed). Originals remain in "
                  f"their original source location for a re-process.",
                  outcome="success")
    return jsonify({"ok": True, "job": job})


# ---------------------------------------------------------------------------
# Move timing — the rule (non-negotiable): NOTHING moves until
# a book has successfully processed and its finished .m4b is in
# Plex. A failed build, a cancelled job, a health-held file in
# Review, or a rejected file must leave the source files exactly
# where they were, in the original source location. Only the two
# success paths move originals to Processed: (1) health pass ->
# Plex -> move originals; (2) health hold -> Review -> approve ->
# Plex -> move originals as part of that same approve step.
# ---------------------------------------------------------------------------

def _move_originals_to_processed(job_id: str, files: list[dict],
                                 meta: dict, folder: str,
                                 log_fn) -> tuple[str | None, list[str], list[str]]:
    """Move source files to Processed/<Author>/<Book>/ — ONLY to be
    called after the finished .m4b is successfully in Plex.

    Moves files one by one, logging each, and never deletes a source
    file before its move is confirmed (shutil.move only removes the
    source once the copy is complete). If a move fails midway it
    does NOT raise: it logs the exact file, keeps going with the
    rest, and returns a warning — the Plex file is already placed,
    so the job is Done-with-warning, never Failed. The already-moved
    files are recorded on the file records (processed_path); the
    unmoved ones keep their original path and are named in the
    warning, so nothing is ever lost track of.

    Returns (warning_or_None, moved_names, failed_names). With
    keep_originals='keep' nothing moves and the warning is None.
    """
    behaviour = SETTINGS.get("keep_originals", "processed")
    if behaviour == "move_done":
        behaviour = "processed"
    if behaviour != "processed":
        for f in files:
            f["outcome"] = "original kept in place"
            log_fn("plex-output", "INFO",
                   f"Original kept in place: {f['path']}",
                   filename=f["name"], outcome="success")
        return None, [], []
    processed_base = _processed_dir()
    if not processed_base:
        warning = ("Processed folder is not configured — originals were "
                   "left in the source folder: "
                   f"{folder or '(unknown source folder)'}.")
        log_fn("plex-output", "WARN", warning, outcome="warning")
        return warning, [], [f.get("name", "?") for f in files]
    author = plex_output.sanitise(
        meta.get("author") or meta.get("authors") or "Unknown Author")
    book = plex_output.sanitise(
        meta.get("title") or
        os.path.basename((folder or "").rstrip("/")) or "Unknown Book")
    dest_dir = os.path.join(processed_base, author, book)
    moved: list[str] = []
    failed: list[str] = []
    for f in files:
        src = f.get("path") or ""
        if not src or not os.path.isfile(src):
            failed.append(f.get("name") or src or "?")
            f["outcome"] = (f"original NOT moved — source file missing: {src}")
            log_fn("plex-output", "ERROR",
                   f"Could not move original {f.get('name')}: source file "
                   f"missing: {src or '(no path recorded)'}",
                   filename=f.get("name"), outcome="FAIL")
            continue
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, f["name"])
            if os.path.exists(dest_path):
                # Same filename already processed (e.g. a re-run):
                # keep both, don't overwrite.
                stem, ext = os.path.splitext(f["name"])
                n = 2
                while os.path.exists(
                        os.path.join(dest_dir, f"{stem} ({n}){ext}")):
                    n += 1
                dest_path = os.path.join(dest_dir, f"{stem} ({n}){ext}")
            shutil.move(src, dest_path)
            f["outcome"] = "original moved to Processed"
            f["processed_path"] = dest_path
            moved.append(f.get("name") or dest_path)
            log_fn("plex-output", "INFO",
                   f"Original moved to Processed: {src} -> {dest_path}",
                   filename=f.get("name"), outcome="success")
        except OSError as exc:
            failed.append(f.get("name") or src)
            f["outcome"] = f"original NOT moved — {exc}"
            log_fn("plex-output", "ERROR",
                   f"Could not move original {f.get('name')} to Processed: {exc}",
                   filename=f.get("name"), outcome="FAIL")
    if not failed:
        return None, moved, []
    warning = (
        f"Some originals could not be moved to Processed: "
        f"{', '.join(failed)}. The finished file IS in Plex. "
        f"Unmoved originals are still in the source folder: "
        f"{folder or '(unknown source folder)'}.")
    log_fn("plex-output", "WARN", warning, outcome="warning")
    return warning, moved, failed


# Build runner (called by the queue runner, one job at a time)
# ---------------------------------------------------------------------------

def _run_build(job_id: str):
    job = load_job(job_id)
    if job is None:
        return
    s = SETTINGS
    meta = job.get("metadata") or {}
    files = job.get("files") or []

    def _log(stage, level, msg, **kw):
        return flog.log_event(job_id, stage, level, msg, **kw)

    def _cancelled() -> bool:
        return _is_cancel_requested(job_id)

    try:
        if _cancelled():
            _mark_cancelled(job_id, "Cancel requested before the build started.")
            return
        # Scanning: re-probe every file right before building. A
        # corrupt/empty file found here fails the job with its exact
        # error — it is never silently dropped from the merge.
        _set_progress(job_id, 0, "scanning")
        probed = []
        for i, f in enumerate(files):
            if _cancelled():
                _mark_cancelled(job_id, "Cancelled during scanning.")
                return
            try:
                info = build_mod.probe(f["path"])
                probed.append({**f, "duration": info["duration"],
                               "codec": info["codec"],
                               "sample_rate": info["sample_rate"],
                               "chapter_title": f.get("chapter_title") or
                               os.path.splitext(f["name"])[0],
                               "outcome": "probed OK"})
                _log("build", "INFO",
                     f"Pre-build probe OK: {f['name']} ({info['duration']:.1f}s)",
                     filename=f["name"], outcome="success")
            except build_mod.BuildError as exc:
                _log("build", "ERROR",
                     f"Pre-build probe FAILED for {f['name']}: {exc}",
                     filename=f["name"], outcome="FAIL")
                raise
            _set_progress(job_id,
                          queue_mod.progress_for_stage("scanning", (i + 1) / max(1, len(files))),
                          "scanning")
        job = _update_job(job_id, files=probed)
        if job is None:
            return
        if _cancelled():
            _mark_cancelled(job_id, "Cancelled during scanning.")
            return

        # Metadata stage (10-20%): an auto-queued job only has a
        # filename guess so far — try the ASIN lookup now. A failed
        # lookup is a warning, never a blocker.
        _set_progress(job_id, 10, "metadata")
        meta = job.get("metadata") or {}
        if meta.get("asin") and job.get("metadata_source") != "audnexus":
            try:
                result = meta_mod.fetch_by_asin(meta["asin"], s["audnexus_url"])
                job = _update_job(job_id, metadata={**meta, **result},
                                  metadata_source="audnexus")
                meta = job["metadata"] if job else meta
                _log("metadata", "INFO",
                     f"Metadata lookup OK: '{result.get('title')}' by "
                     f"{result.get('author')} (ASIN {result.get('asin')})",
                     outcome="success")
            except Exception as exc:  # noqa: BLE001
                _log("metadata", "WARN",
                     f"Metadata lookup failed for ASIN {meta.get('asin')}: {exc} — "
                     f"continuing with the filename guess; edit the fields "
                     f"from the queue row if the title is wrong.",
                     outcome="warning")
        _set_progress(job_id, 20, "sorting")
        _log("sorting", "INFO",
             f"Sorting: {len(probed)} file(s) in confirmed order "
             f"({'single file — nothing to sort' if len(probed) == 1 else 'order confirmed by user/auto-sort'}).",
             outcome="success")

        # Cover art (downloaded, logged; failure is a warning, not fatal).
        cover_path = None
        cover_url = meta.get("cover_url")
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=30)
                resp.raise_for_status()
                cover_path = os.path.join(s["temp_dir"], f"{job_id}-cover.jpg")
                os.makedirs(s["temp_dir"], exist_ok=True)
                with open(cover_path, "wb") as fh:
                    fh.write(resp.content)
                _log("build", "INFO",
                     f"Cover art downloaded ({len(resp.content)} bytes) from {cover_url}",
                     outcome="success")
            except Exception as exc:  # noqa: BLE001
                _log("build", "WARN",
                     f"Cover download failed ({cover_url}): {exc} — "
                     f"continuing without embedded cover.",
                     outcome="warning")
        _set_progress(job_id, 25, "metadata")

        dest = plex_output.plex_output_path(s["output_dir"], meta)
        job = _update_job(job_id, output_path=dest)
        _log("plex-output", "INFO", f"Destination (Plex structure): {dest}")
        meta_for_build = dict(meta, cover_path=cover_path)
        # Converting (30-90%): REAL ffmpeg progress — out_time_ms
        # vs total duration, parsed from the encode itself.
        _set_progress(job_id, 30, "converting")
        # The merge lands in a STAGING file first, NOT straight into
        # Plex: the health check runs on it, and only a passing file
        # is placed. A held file goes to Review instead.
        stage_path = os.path.join(s["temp_dir"], f"{job_id}-output.m4b")
        result = build_mod.build_m4b(
            probed, meta_for_build, stage_path, s["temp_dir"],
            bitrate=s.get("bitrate", "96k"),
            log=lambda stage, level, msg, **kw: _log(stage, level, msg, **kw),
            progress_callback=lambda frac: _set_progress(
                job_id,
                queue_mod.progress_for_stage("converting", frac),
                "converting"),
            cancel_check=_cancelled,
            register_proc=lambda proc: _register_proc(job_id, proc))
        if _cancelled():
            # Cancel landed after the encode finished: the file is
            # complete, but the user said no — delete it, no output.
            try:
                if os.path.exists(stage_path):
                    os.remove(stage_path)
            except OSError:
                pass
            _mark_cancelled(job_id, "Cancelled after the encode finished — output deleted.")
            return
        job = _update_job(job_id, output_duration=result["duration"],
                          output_size=result["size"])

        # HEALTH CHECK (90-95%): finished file vs the metadata
        # runtime and chapter list. A warn/fail verdict NEVER goes
        # to Plex — it is quarantined in Review for a human.
        _set_progress(job_id, 90, "health")
        report = health_mod.run_health_check(stage_path, probed, meta)
        report_path = health_mod.save_health_report(JOBS_DIR, job_id, report)
        _log("health",
             "INFO" if report["verdict"] in ("pass", "limited") else "WARN",
             f"Health check: {report['verdict'].upper()} — expected "
             f"{report['expected_total']}, got {report['actual_total']} "
             f"({len(report['chapters'])} chapter(s)). "
             + "; ".join(report["diagnoses"]),
             outcome="success" if report["verdict"] in ("pass", "limited")
             else "warning",
             extra={"verdict": report["verdict"], "report": report_path})
        job = _update_job(job_id, health={
            "verdict": report["verdict"], "limited": report["limited"],
            "expected_total": report["expected_total"],
            "actual_total": report["actual_total"],
            "missing": report["missing"],
            "diagnoses": report["diagnoses"]})
        if job is None:
            return

        held = report["verdict"] in ("warn", "fail")
        review_path = None
        review_cover = None
        if held:
            # Quarantine: the finished file moves to the Review
            # folder, under the same relative path it would have
            # had in Plex — so Approve knows exactly where it goes.
            # Cover art travels with it.
            review_base = _review_dir()
            rel = os.path.relpath(dest, s["output_dir"])
            review_path = os.path.join(review_base, rel)
            os.makedirs(os.path.dirname(review_path), exist_ok=True)
            os.replace(stage_path, review_path)
            if cover_path and os.path.exists(cover_path):
                review_cover = os.path.join(
                    os.path.dirname(review_path), "cover.jpg")
                try:
                    shutil.copyfile(cover_path, review_cover)
                except OSError as exc:
                    _log("health", "WARN",
                         f"Could not copy cover art to the Review folder: {exc}",
                         outcome="warning")
                    review_cover = None
            _log("health", "WARN",
                 f"Health check {report['verdict'].upper()} — file HELD "
                 f"in Review, NOT placed in Plex: {review_path}. Open "
                 f"the Review tab to approve it or reject it.",
                 outcome="warning")
        else:
            # Passing (or limited — no metadata to compare against):
            # place in Plex (95-100%).
            _set_progress(job_id, 95, "placing")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(stage_path, dest)
            if cover_path and os.path.exists(cover_path):
                try:
                    shutil.copyfile(cover_path, plex_output.cover_path_for(dest))
                    _log("plex-output", "INFO",
                         f"Cover written to {plex_output.cover_path_for(dest)}",
                         outcome="success")
                except OSError as exc:
                    _log("plex-output", "WARN",
                         f"Could not write cover.jpg alongside output: {exc}",
                         outcome="warning")

        if held:
            # Held for review: originals STAY in their original source
            # location — nothing moves to Processed until (and unless)
            # the held file is approved into Plex. A rejected book is
            # re-processed from the source folder, not from Processed.
            for f in probed:
                f["outcome"] = "original kept in source — held in Review"
            _log("health", "INFO",
                 "Originals stay in the source folder while the file is "
                 f"held in Review: {job.get('folder', '')} — nothing "
                 f"moved to Processed.",
                 outcome="success")
            job = _update_job(job_id, files=probed,
                              status="needs_health_review",
                              stage="needs_health_review",
                              progress_pct=100,
                              review_path=review_path,
                              review_cover_path=review_cover,
                              held_at=datetime.now(timezone.utc).isoformat())
            _progress_cache.pop(job_id, None)
            _refresh_queue_positions()
            return
        # Success path (1): health passed, file is in Plex — ONLY now
        # do the originals move to Processed.
        move_warning, _moved, _failed = _move_originals_to_processed(
            job_id, probed, meta, job.get("folder", ""), _log)
        job = _update_job(job_id, files=probed, status="done", stage="done",
                          progress_pct=100,
                          originals_warning=move_warning or "",
                          warning=move_warning or "",
                          finished_at=datetime.now(timezone.utc).isoformat())
        _progress_cache.pop(job_id, None)
        if move_warning:
            _log("plex-output", "WARN",
                 f"Job DONE with a warning: {move_warning}",
                 outcome="warning")
        _log("plex-output", "INFO",
             f"Job DONE: {dest} ({result['size'] / 1e6:.1f} MB, "
             f"{result['duration']:.1f}s). Health check "
             f"{report['verdict'].upper()}. Rescan the Plex audiobook "
             f"library to see it.",
             outcome="success")
        _refresh_queue_positions()
    except build_mod.CancelledBuild as exc:
        _mark_cancelled(job_id, str(exc) or "Cancelled by user.")
    except Exception as exc:  # noqa: BLE001 — every failure lands in the log + UI
        if _cancelled():
            _mark_cancelled(job_id, "Cancelled by user.")
            return
        reason = str(exc) or repr(exc)
        job = load_job(job_id) or job
        job["status"] = "failed"
        job["stage"] = "failed"
        job["error"] = reason
        job["failed_at"] = datetime.now(timezone.utc).isoformat()
        if "files" in locals():
            job["files"] = probed if "probed" in locals() else files
        save_job(job)
        _progress_cache.pop(job_id, None)
        _log("build", "ERROR", f"Job FAILED: {reason}", outcome="FAIL")
        _refresh_queue_positions()


# ---------------------------------------------------------------------------
# API — logs (view / copy / download / plain text)
# ---------------------------------------------------------------------------

@app.route("/api/jobs/<job_id>/log")
def job_log_api(job_id):
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": f"Job {job_id} not found"}), 404
    fmt = request.args.get("format", "json")
    if fmt == "pasteable":
        return Response(flog.build_pasteable_log(DATA_DIR, job_id, job),
                        mimetype="text/plain")
    if fmt == "last-error":
        full = flog.get_job_text(DATA_DIR, job_id)
        return Response(flog.last_error_block(full), mimetype="text/plain")
    if fmt == "text":
        return Response(flog.get_job_text(DATA_DIR, job_id), mimetype="text/plain")
    if request.args.get("download") == "1":
        path = flog.job_log_paths(DATA_DIR, job_id)["text"]
        if os.path.exists(path):
            return send_file(path, as_attachment=True,
                             download_name=f"cadence-{job_id}.log")
        return jsonify({"ok": False, "error": "No log file yet for this job"}), 404
    events = flog.read_job_log(DATA_DIR, job_id, max_lines=1000)
    level = request.args.get("level")
    stage = request.args.get("stage")
    q = (request.args.get("q") or "").lower()
    if level:
        events = [e for e in events if e.get("level") == level.upper()]
    if stage:
        events = [e for e in events if e.get("stage") == stage]
    if q:
        events = [e for e in events if q in json.dumps(e, default=str).lower()]
    return jsonify({"ok": True, "job_id": job_id, "job": job, "events": events})


@app.route("/api/logs/app")
def global_log_api():
    fmt = request.args.get("format", "json")
    if fmt == "pasteable":
        return Response(flog.build_global_pasteable_log(DATA_DIR), mimetype="text/plain")
    if fmt == "last-error":
        return Response(flog.last_error_block(flog.get_global_text(DATA_DIR)),
                        mimetype="text/plain")
    if fmt == "text":
        return Response(flog.get_global_text(DATA_DIR), mimetype="text/plain")
    if request.args.get("download") == "1":
        path = os.path.join(flog.log_dir(DATA_DIR), flog.GLOBAL_LOG)
        if os.path.exists(path):
            return send_file(path, as_attachment=True, download_name="cadence-app.log")
        return jsonify({"ok": False, "error": "Global log is empty"}), 404
    return jsonify({"ok": True, "text": flog.get_global_text(DATA_DIR)[-50000:],
                    "last_errors": flog.last_errors(DATA_DIR, limit=10)})


@app.route("/api/logs/last-errors")
def last_errors_api():
    return jsonify({"ok": True, "errors": flog.last_errors(DATA_DIR, limit=10)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8285"))
    flog.log_info(None, "startup", f"Cadence starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
