"""
Cadence — server-busy backoff (pure logic, no Flask).

Why this exists: the server also runs Plex, and several people
starting Plex transcodes at once should make Cadence back off and
wait for things to quiet down — rather than running on a schedule.

Signals (checked every ``check_interval`` seconds by the monitor
thread in app.py; every decision is logged with its reason):
  * Host load — /proc/loadavg (1-min) vs the CPU core count, and
    memory pressure from /proc/meminfo. NOTE: /proc/loadavg and
    /proc/meminfo are NOT container-namespaced — inside Docker they
    still report the HOST, which is exactly what we want here. No
    Docker socket mount, no privileged mode, nothing to install.
  * Plex transcodes (optional) — GET <plex-url>/status/sessions
    with X-Plex-Token. Busy only when a session has an active
    TranscodeSession; direct play / direct stream is cheap and
    does NOT count. Plex not configured or unreachable is a
    warning, never a failure — the load signal still governs.

What NOT to expect: this does not watch Tdarr or the GPU directly.
Plex transcode sessions + host load cover the case that motivated
this (several people hopping on Plex at once); Tdarr's own load shows up in the
host load number anyway.

Pause mechanism (see app.py): the running ffmpeg process is frozen
with SIGSTOP and thawed with SIGCONT. ffmpeg resumes cleanly — the
partial .m4b stays on disk, the encode just continues where it
stopped, and the job is never marked failed or cancelled.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

# Memory pressure counts as busy when less than this much RAM is
# still available — a server swapping is busy whatever the load says.
MEM_AVAILABLE_BUSY_PCT = 10.0

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> tuple[int, int] | None:
    """'HH:MM' -> (hour, minute), or None when it isn't one."""
    m = _HHMM_RE.match((value or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def in_run_window(now: datetime, start: str, end: str) -> bool:
    """True when ``now`` falls inside the HH:MM–HH:MM window.

    Empty start/end = no window (run any time). Equal start/end =
    all day. A window that crosses midnight (e.g. 22:00–06:00)
    works: it is inside when now >= start OR now < end.
    """
    s = parse_hhmm(start)
    e = parse_hhmm(end)
    if s is None or e is None:
        return True
    now_min = now.hour * 60 + now.minute
    start_min, end_min = s[0] * 60 + s[1], e[0] * 60 + e[1]
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def read_host_load() -> dict:
    """Host load right now: 1-min loadavg, cores, memory available.

    Reads /proc directly — host-wide even inside Docker (see the
    module docstring). Raises OSError on a system without /proc;
    the caller treats that as 'unknown', never as busy.
    """
    with open("/proc/loadavg", encoding="utf-8") as fh:
        load1 = float(fh.read().split()[0])
    cores = os.cpu_count() or 1
    mem_total = mem_avail = None
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1])
    mem_avail = mem_available if mem_available is not None else None
    mem_total_kb = mem_total
    mem_avail_pct = None
    if mem_total_kb and mem_avail is not None:
        mem_avail_pct = mem_avail / mem_total_kb * 100
    return {
        "load1": load1,
        "cores": cores,
        "load_pct": load1 / cores * 100,
        "mem_available_pct": mem_avail_pct,
    }


def load_is_busy(load: dict, threshold_pct: float) -> tuple[bool, str]:
    """Busy when load exceeds ``threshold_pct`` % of the cores, or
    memory pressure is high. Returns (busy, plain-language reason)."""
    load_pct = float(load.get("load_pct") or 0)
    load1 = float(load.get("load1") or 0)
    cores = int(load.get("cores") or 1)
    if load_pct > float(threshold_pct):
        return True, (
            f"server load {load1:.1f} on {cores} core(s) "
            f"({load_pct:.0f}% of capacity, limit {float(threshold_pct):.0f}%)"
        )
    mem_pct = load.get("mem_available_pct")
    if mem_pct is not None and mem_pct < MEM_AVAILABLE_BUSY_PCT:
        return True, (
            f"memory nearly full — only {mem_pct:.0f}% available"
        )
    return False, ""


def describe_load(load: dict) -> str:
    """One-line human summary of a load reading (for the UI)."""
    load_pct = float(load.get("load_pct") or 0)
    mem_pct = load.get("mem_available_pct")
    text = (f"load {float(load.get('load1') or 0):.1f} on "
            f"{int(load.get('cores') or 1)} core(s) ({load_pct:.0f}%)")
    if mem_pct is not None:
        text += f", {mem_pct:.0f}% memory free"
    return text


def fetch_plex_sessions(url: str, token: str, timeout: int = 5) -> dict:
    """GET <plex-url>/status/sessions. Returns
    {"ok", "sessions", "transcodes", "error"} — transcodes counts
    sessions with an active TranscodeSession child; direct play and
    direct stream do NOT count. Never raises; a bad URL, a timeout,
    or a wrong token comes back as ok=False with the error string.
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "sessions": 0, "transcodes": 0,
                "error": "No Plex server URL configured."}
    try:
        resp = requests.get(f"{base}/status/sessions",
                            headers={"X-Plex-Token": token or "",
                                     "Accept": "application/xml"},
                            timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — any failure is a warning
        return {"ok": False, "sessions": 0, "transcodes": 0,
                "error": f"Plex request failed: {exc}"}
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        return {"ok": False, "sessions": 0, "transcodes": 0,
                "error": f"Plex returned unparseable XML: {exc}"}
    sessions = 0
    transcodes = 0
    for video in root.iter("Video"):
        sessions += 1
        if video.find("TranscodeSession") is not None:
            transcodes += 1
    return {"ok": True, "sessions": sessions, "transcodes": transcodes,
            "error": ""}


def plex_is_busy(plex: dict) -> tuple[bool, str]:
    """Busy when any Plex session is actively transcoding."""
    n = int(plex.get("transcodes") or 0)
    if n > 0:
        return True, (
            f"Plex is transcoding ({n} transcode session(s)"
            + (f", {int(plex.get('sessions') or 0)} playing" if plex.get("sessions") else "")
            + ")"
        )
    return False, ""


class BackoffState:
    """Debounce + quiet-grace for busy/quiet transitions.

    * A single busy reading does nothing — the load has to stay
      busy for ``consecutive_needed`` checks in a row (default 2,
      i.e. ~60s at the default 30s check interval) before anything
      pauses. One spike from a Plex scan does not stop a build.
    * Resuming needs the server quiet for a continuous
      ``quiet_grace_s`` (default 120s) — no stop/start flapping.

    ``update(busy, reason, now)`` — ``now`` is a monotonic clock
    (time.monotonic) — returns one of:
      "pause"  — busy sustained, and not paused yet: freeze the encode
      "hold"   — busy (already paused, or still counting) / quiet but
                 grace not yet reached: do nothing, keep waiting
      "resume" — quiet for the full grace: thaw the encode
      "start"  — quiet and not paused: new jobs may start
      "none"   — first busy reading: still counting, do nothing
    The state owns ``paused``: True after "pause", False after "resume".
    """

    def __init__(self, consecutive_needed: int = 2,
                 quiet_grace_s: float = 120.0):
        self.consecutive_needed = max(1, int(consecutive_needed))
        self.quiet_grace_s = max(0.0, float(quiet_grace_s))
        self.consecutive_busy = 0
        self.quiet_since: float | None = None
        self.paused = False

    def update(self, busy: bool, reason: str, now: float) -> str:
        if busy:
            self.consecutive_busy += 1
            self.quiet_since = None
            if self.consecutive_busy < self.consecutive_needed:
                return "none"
            if not self.paused:
                self.paused = True
                return "pause"
            return "hold"
        self.consecutive_busy = 0
        if not self.paused:
            return "start"
        if self.quiet_since is None:
            self.quiet_since = now
            return "hold"
        if now - self.quiet_since >= self.quiet_grace_s:
            self.paused = False
            self.quiet_since = None
            return "resume"
        return "hold"
