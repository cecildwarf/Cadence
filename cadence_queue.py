"""
Cadence — queue routing + progress maths (pure logic, no Flask).

Queue rules (also stated in the UI + README, keep in sync):
  * 1 audio file  -> auto-queue straight away (no sorting to do).
  * >1 files      -> organisation/review flow first (auto-sort + reorder).
  * 0 files       -> no job at all, clear message.

Bulk queueing creates ONE queue entry per book/folder, and the
queue runner builds them ONE AT A TIME (small home servers
should never run ffmpeg jobs in parallel).

Progress is REAL, derived from actual stages:
  scanning   0-10%   metadata 10-20%   sorting 20-30%
  converting 30-90%   (ffmpeg out_time_ms / total duration)
  placing    90-100%  done 100%
"""

from __future__ import annotations

import os
import re

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".aac", ".ogg", ".wav"}

# Stage label -> (low %, high %) of the overall 0-100 bar.
STAGE_RANGES = {
    "queued": (0, 0),
    "scanning": (0, 10),
    "metadata": (10, 20),
    "sorting": (20, 30),
    "converting": (30, 90),
    "health": (90, 95),
    "placing": (95, 100),
    "done": (100, 100),
    "failed": (100, 100),
    "cancelled": (100, 100),
}

STAGE_LABELS = {
    "queued": "Queued",
    "scanning": "Scanning",
    "metadata": "Metadata",
    "sorting": "Sorting",
    "converting": "Converting",
    "health": "Health check",
    "placing": "Placing in Plex",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "needs_health_review": "Needs health review",
    "rejected": "Rejected",
}


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def progress_for_stage(stage: str, fraction: float = 0.0) -> int:
    """Overall % for a stage + how far through that stage (0.0-1.0)."""
    lo, hi = STAGE_RANGES.get(stage, (0, 0))
    frac = max(0.0, min(1.0, float(fraction or 0.0)))
    return int(round(lo + (hi - lo) * frac))


def count_audio_files(folder: str, exts=frozenset(AUDIO_EXTS)) -> list[str]:
    """All audio files under folder (folder + subfolders, sorted).

    Same walk as scan_folder() in app.py, so the routing count can
    never disagree with what the job actually finds.
    """
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in exts:
                found.append(os.path.join(root, name))
    return sorted(found)


def route_for_count(n: int) -> str:
    """'auto_queue' | 'review' | 'empty' — the folder-select rule."""
    if n == 0:
        return "empty"
    if n == 1:
        return "auto_queue"
    return "review"


# --- Metadata guessing for a single file (no lookup yet) -------------------
# Filenames like "Wild Magic_B002V0TEQ8_LC_64_22050_Stereo.m4b" or
# "01 - The Way of Kings.mp3" or "Brandon Sanderson - The Way of Kings.mp3".
# The guess is only a starting point: the metadata lookup (ASIN first)
# runs at build time, and every field stays editable from the queue
# row before the job starts.

_ASIN_RE = re.compile(r"(?<![A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])", re.IGNORECASE)
_TRAILING_JUNK_RE = re.compile(
    r"(_LC_\d+_\d+_.*|_LC_\d+.*|_\d+k?_?\d*kbps.*)$", re.IGNORECASE)


def guess_metadata_from_name(name: str) -> dict:
    """Best-effort {title, author, asin} from one audio filename."""
    base = os.path.splitext(os.path.basename(name))[0]
    asin_m = _ASIN_RE.search(base)
    asin = asin_m.group(1).upper() if asin_m else ""
    title = base
    if asin_m:
        title = base[:asin_m.start()] + base[asin_m.end():]
    title = _TRAILING_JUNK_RE.sub("", title)
    title = title.replace("_", " ").strip(" -_")
    # Leading track numbers ("01 - Title") are not the title.
    title = re.sub(r"^\d{1,3}\s*[-–.]\s*", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    # "Author - Title" is a common single-file pattern; keep it
    # conservative: only split when both sides look substantial.
    author = ""
    if " - " in title:
        left, right = title.split(" - ", 1)
        if len(left.strip()) >= 3 and len(right.strip()) >= 3:
            author, title = left.strip(), right.strip()
    return {"title": title or os.path.splitext(os.path.basename(name))[0],
            "author": author, "asin": asin}
