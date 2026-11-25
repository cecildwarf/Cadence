"""
Cadence — structured logging.

Non-negotiable rule: NEVER silently fail, NEVER swallow an error,
NEVER show a generic "something went wrong".

Every stage logs, with:
  * timestamp in local time (configurable timezone, default UTC)
    AND UTC (ISO 8601)
  * level (INFO / WARN / ERROR)
  * stage (startup, scan, metadata, sort, build, ffmpeg, plex-output, ...)
  * job id (or "startup" / "global")
  * filename when applicable
  * a clear success / failure outcome per file / step

Sinks (all under <data_dir>/logs/, which is volume-mounted so logs
survive container restarts):
  * data/logs/app.log      — global text log, rotating, 30 days kept
  * data/logs/<job>.log    — one text log per job
  * data/logs/<job>.jsonl  — one machine-readable JSON-lines log per job

The stdlib ``logging`` RotatingFileHandler is used for the global
log (10 MB per file, 30 backups ≈ 30 days at expected volume).
Per-job logs are written by the same log_event() call, so a job
can never "succeed" in the UI while its log says otherwise — the
job record's status is derived from the same events.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

_TZ_NAME = "UTC"
_TZ = ZoneInfo("UTC")


def set_timezone(name: str) -> bool:
    """Set the local timezone used in log timestamps.

    Returns True on success; on an unknown name keeps the current
    timezone and returns False (caller logs the rejection).
    """
    global _TZ_NAME, _TZ
    try:
        tz = ZoneInfo(name or "UTC")
    except Exception:
        return False
    _TZ_NAME = name or "UTC"
    _TZ = tz
    return True


def get_timezone_name() -> str:
    return _TZ_NAME
LOG_DIR_NAME = "logs"
GLOBAL_LOG = "app.log"

_lock = threading.Lock()
_global_logger: logging.Logger | None = None
_data_dir: str | None = None


def _now() -> tuple[str, str]:
    """Return (local_iso, utc_iso) timestamps for one event."""
    utc = datetime.now(timezone.utc)
    local = utc.astimezone(_TZ)
    return local.isoformat(timespec="seconds"), utc.isoformat(timespec="seconds")


def log_dir(data_dir: str) -> str:
    path = os.path.join(data_dir, LOG_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def init_logging(data_dir: str) -> logging.Logger:
    """Initialise (or re-initialise) the global rotating log."""
    global _global_logger, _data_dir
    _data_dir = data_dir
    d = log_dir(data_dir)
    logger = logging.getLogger("cadence")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    handler = RotatingFileHandler(
        os.path.join(d, GLOBAL_LOG),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=30,             # keep at least 30 days of rotations
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)

    # Also log to the container stdout so `docker logs` shows it.
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(console)
    _global_logger = logger
    return logger


def _record(job_id: str | None, stage: str, level: str, message: str,
            filename: str | None = None, outcome: str | None = None,
            extra: dict | None = None) -> dict:
    local_ts, utc_ts = _now()
    record = {
        "ts_local": local_ts,
        "ts_utc": utc_ts,
        "tz": _TZ_NAME,
        "level": level,
        "stage": stage,
        "job_id": job_id or "global",
        "filename": filename,
        "message": message,
        "outcome": outcome,
        "extra": extra or {},
    }
    return record


def _text_line(record: dict) -> str:
    parts = [f"[{record['ts_local']} {record.get('tz', 'UTC')} / {record['ts_utc']} UTC]",
             record["level"], f"[{record['stage']}]",
             f"[job={record['job_id']}]"]
    if record.get("filename"):
        parts.append(f"[file={record['filename']}]")
    parts.append(record["message"])
    if record.get("outcome"):
        parts.append(f"=> {record['outcome']}")
    if record.get("extra"):
        parts.append(json.dumps(record["extra"], default=str))
    return " ".join(parts)


def log_event(job_id: str | None, stage: str, level: str, message: str,
              filename: str | None = None, outcome: str | None = None,
              extra: dict | None = None) -> dict:
    """Write one structured event to every sink. Never raises.

    Returns the record dict, so callers can also store it in the
    job JSON. If logging itself fails (disk full, read-only dir),
    the error is printed to stderr and the record is still
    returned — a logging failure must never mask the real error.
    """
    record = _record(job_id, stage, level, message, filename, outcome, extra)
    line = _text_line(record)
    try:
        with _lock:
            if _global_logger is not None:
                lvl = {"INFO": logging.INFO, "WARN": logging.WARNING,
                       "ERROR": logging.ERROR, "DEBUG": logging.DEBUG}.get(level, logging.INFO)
                _global_logger.log(lvl, line)
            if _data_dir:
                d = log_dir(_data_dir)
                if job_id:
                    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")
                    with open(os.path.join(d, f"{safe}.log"), "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                    with open(os.path.join(d, f"{safe}.jsonl"), "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 — logging must never crash the app
        print(f"LOGGING FAILURE (original event: {line}) — {exc!r}", flush=True)
    return record


def log_info(job_id, stage, message, **kw):
    return log_event(job_id, stage, "INFO", message, **kw)


def log_warn(job_id, stage, message, **kw):
    return log_event(job_id, stage, "WARN", message, **kw)


def log_error(job_id, stage, message, **kw):
    return log_event(job_id, stage, "ERROR", message, **kw)


def job_log_paths(data_dir: str, job_id: str) -> dict[str, str]:
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")
    d = log_dir(data_dir)
    return {
        "text": os.path.join(d, f"{safe}.log"),
        "jsonl": os.path.join(d, f"{safe}.jsonl"),
    }


def read_job_log(data_dir: str, job_id: str, max_lines: int = 500) -> list[dict]:
    """Return the job's JSONL events (newest last), parsed, best-effort."""
    paths = job_log_paths(data_dir, job_id)
    events: list[dict] = []
    try:
        with open(paths["jsonl"], encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"level": "ERROR", "stage": "logging",
                                   "message": f"Unparseable log line: {line[:500]}",
                                   "ts_local": "", "ts_utc": "", "job_id": job_id})
    except FileNotFoundError:
        pass
    return events[-max_lines:]


def last_errors(data_dir: str, limit: int = 10) -> list[dict]:
    """Most recent ERROR events from the global log, for the home page.

    Parses the tail of app.log's JSONL sibling if present; falls back
    to scanning the text log for ERROR lines.
    """
    d = log_dir(data_dir)
    errors: list[dict] = []
    # Prefer per-job jsonl files (structured), newest first.
    try:
        files = sorted(
            (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")),
            key=os.path.getmtime, reverse=True,
        )
    except OSError:
        files = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("level") == "ERROR":
                        errors.append(rec)
        except OSError:
            continue
        if len(errors) >= limit:
            break
    errors.sort(key=lambda r: r.get("ts_utc", ""), reverse=True)
    return errors[:limit]


# ---------------------------------------------------------------------------
# Phone copy-paste helpers (failures are often evaluated from a phone)
# ---------------------------------------------------------------------------

def get_job_text(data_dir: str, job_id: str) -> str:
    """Full per-job .log text, or '' if the job has no log yet."""
    paths = job_log_paths(data_dir, job_id)
    try:
        with open(paths["text"], encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def get_global_text(data_dir: str) -> str:
    """Full global app.log text, or ''."""
    try:
        with open(os.path.join(log_dir(data_dir), GLOBAL_LOG),
                   encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def last_error_block(text: str, context_lines: int = 20) -> str:
    """ERROR entries plus the ``context_lines`` lines before the first error.

    So a pasted failure report stays small: the error itself and
    what led up to it, not the whole (possibly huge) log.
    """
    lines = text.splitlines()
    first_error = next(
        (i for i, ln in enumerate(lines)
         if " ERROR " in ln or " | ERROR |" in ln or ln.startswith("ERROR")),
        None,
    )
    if first_error is None:
        error_lines = [ln for ln in lines if "ERROR" in ln]
        if not error_lines:
            return ("No ERROR entries in this log.\n"
                    "The job did not record a failure — "
                    "copy the full log instead if something looks wrong.")
        return "\n".join(error_lines)
    start = max(0, first_error - context_lines)
    block = lines[start:]
    # Always include every ERROR line even if it is past the first one.
    tail_errors = [ln for ln in lines if "ERROR" in ln and ln not in block]
    if tail_errors:
        block = block + ["..."] + tail_errors
    return "\n".join(block)


def build_pasteable_log(data_dir: str, job_id: str,
                        job: dict | None = None) -> str:
    """Self-contained log text for pasting into chat.

    Summary block first (job id, book title/ASIN, timestamps,
    per-file outcomes), then the full log. A pasted copy alone
    must be enough to diagnose — no other context needed.
    """
    job = job or {}
    meta = job.get("metadata") or {}
    events = read_job_log(data_dir, job_id, max_lines=100000)
    full = get_job_text(data_dir, job_id)

    title = meta.get("title") or job.get("title") or "(unknown title)"
    author = meta.get("author") or ""
    asin = meta.get("asin") or job.get("asin") or "(no ASIN)"
    status = job.get("status") or "(unknown status)"
    error = job.get("error") or ""
    created = job.get("created") or ""
    output = job.get("output_path") or "(no output yet)"

    lines = [
        "=== Cadence — job log (paste this whole block) ===",
        f"Job id:      {job_id}",
        f"Book:        {title}" + (f" by {author}" if author else ""),
        f"ASIN:        {asin}",
        f"Status:      {status}",
        f"Created:     {created}",
        f"Output:      {output}",
    ]
    if error:
        lines.append(f"Error:       {error}")
    lines.append("")
    lines.append("--- Per-file outcomes ---")
    files = job.get("files") or []
    if files:
        for f in files:
            lines.append(
                f"  #{f.get('guessed_position', '?')} "
                f"{f.get('name', f.get('path', '?'))} — "
                f"{f.get('outcome', f.get('status', 'unknown'))}"
                + (f" ({f.get('duration', '')}s)" if f.get("duration") else "")
            )
    else:
        lines.append("  (no files recorded for this job)")
    if events:
        lines.append(f"  Log events: {len(events)} "
                     f"({sum(1 for e in events if e.get('level') == 'ERROR')} errors, "
                     f"{sum(1 for e in events if e.get('level') == 'WARN')} warnings)")
    lines.append("")
    lines.append("--- Full log ---")
    lines.append(full if full else "(no log lines recorded yet)")
    return "\n".join(lines) + "\n"


def build_global_pasteable_log(data_dir: str) -> str:
    """Self-contained global log for pasting into chat."""
    full = get_global_text(data_dir)
    errors = last_errors(data_dir, limit=5)
    lines = [
        "=== Cadence — global log (paste this whole block) ===",
        f"Log:         data/logs/{GLOBAL_LOG}",
    ]
    if errors:
        lines.append("Recent errors:")
        for e in errors:
            lines.append(
                f"  [{e.get('ts_local', '')}] [{e.get('stage', '')}] "
                f"[job={e.get('job_id', '')}] {e.get('message', '')}"
            )
    lines.append("")
    lines.append("--- Full log ---")
    lines.append(full if full else "(global log is empty)")
    return "\n".join(lines) + "\n"
