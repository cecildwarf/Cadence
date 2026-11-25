"""
Cadence — health check + quarantine diagnosis.

Runs automatically on EVERY queue item after the .m4b is built,
BEFORE it is placed in Plex. It answers the question Bragi never
could: "am I missing a file?"

What it compares:
  * Output total duration vs the expected total from metadata
    (audnexus runtime — ``runtimeLengthMin`` / ``runtimeLengthSec`` /
    ``runtimeLengthMs`` — when available). A mismatch beyond the
    tolerance holds the file for review.
  * Output total duration vs the summed input durations (within 2%
    — the build already enforces this; here it is reported as part
    of the health report, not a separate silent gate).
  * Per chapter/part (one input file = one chapter): actual duration,
    start timestamp in the finished file, and — when metadata has
    chapter times/titles — the expected duration and title.
  * File count vs expected chapter/part count (when metadata has it).
  * Integrity: every input probes cleanly, no zero-byte files,
    no corrupt files; the output probes cleanly.

Verdicts:
  * "pass"    — everything matches; the file goes to Plex as normal.
  * "limited" — no metadata duration/chapters to compare against,
    so only the input-vs-output + integrity + count checks ran.
    The file still goes to Plex, but the report says so clearly.
  * "warn"    — something looks off (short chapter, numbering gap,
    extra files). Held for review.
  * "fail"    — duration mismatch vs metadata, missing chapter(s),
    corrupt file. Held for review.

A held file NEVER goes to Plex. It goes to the Review folder and
the job becomes "needs_health_review" until you approve it
("Looks OK, place it" — only then do the originals move to
Processed) or reject it (temp file deleted, originals stay in
their original source location for a re-process).

The report is plain language, not just pass/fail. It names the
chapter it suspects, with its expected start timestamp, e.g.:
  "Expected 6h 00m, got 5h 12m — 48 minutes missing"
  "Chapter 3 is 5:00 long; 5:00 is missing from the total —
   verify Chapter 3 is included, it should start at 02:14:00
   in the output"

Pure functions (no Flask): the checker takes paths + file records
+ metadata, so it is easy to unit-test (see tests/test_health.py).
"""

from __future__ import annotations

import json
import os

import build as build_mod

# Duration tolerance vs metadata: >2% OR >3 minutes, whichever is
# larger. A 6-hour book may be off by 3 minutes of intro/outro
# silence between releases; a 20-minute short must be much closer.
DURATION_TOLERANCE_PCT = 2.0
DURATION_TOLERANCE_MIN_SECONDS = 180.0
# Input-vs-output tolerance (same as the build's own gate).
INPUT_OUTPUT_TOLERANCE_PCT = 2.0
# A chapter shorter than this is suspiciously short on its own.
SHORT_CHAPTER_SECONDS = 10.0
# ... and a chapter under half its expected length is truncated.
SHORT_CHAPTER_FRACTION = 0.5


def fmt_hms(seconds: float | None) -> str:
    """6h 00m, 5h 12m, 48m 00s, 5:00 — human durations."""
    if seconds is None:
        return "unknown"
    total = int(round(float(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_timestamp(seconds: float) -> str:
    """Start timestamp inside the finished file: 02:14:00."""
    total = int(round(float(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_mmss(seconds: float) -> str:
    """Short m:ss form for chapter lengths: 5:00."""
    total = int(round(float(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def expected_runtime_seconds(metadata: dict | None) -> float | None:
    """Expected total runtime in seconds from metadata, or None.

    Accepts the normalised audnexus shape (``runtime_seconds`` /
    ``runtime_minutes``) and the raw field names, so a job whose
    metadata was stored before this existed still works.
    """
    meta = metadata or {}
    for key in ("runtime_seconds", "runtimeLengthSec", "duration_seconds"):
        try:
            val = float(meta[key])
        except (KeyError, TypeError, ValueError):
            continue
        if val > 0:
            return val
    for key in ("runtimeLengthMs",):
        try:
            val = float(meta[key])
        except (KeyError, TypeError, ValueError):
            continue
        if val > 0:
            return val / 1000.0
    for key in ("runtime_minutes", "runtimeLengthMin", "runtimeLengthMinutes",
                "lengthMin", "lengthMinutes"):
        try:
            val = float(meta[key])
        except (KeyError, TypeError, ValueError):
            continue
        if val > 0:
            return val * 60.0
    try:
        val = float(meta["length"])
    except (KeyError, TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return val * 60.0 if val <= 3000 else val


def expected_chapters(metadata: dict | None) -> list[dict]:
    """Expected chapters from metadata, normalised to seconds.

    Returns [{title, start_sec, length_sec}] or []. Handles the
    normalised shape (start_sec/length_sec) and the raw audnexus
    shape (startOffsetMs/startOffsetSec/lengthMs).
    """
    meta = metadata or {}
    out = []
    for ch in meta.get("chapters") or []:
        if not isinstance(ch, dict):
            continue

        def _field(*names):
            """One chapter time in seconds. Explicit *Ms keys are
            milliseconds; *Sec / *_sec keys are seconds; a bare
            value over 100000 can only be milliseconds."""
            for name in names:
                if name not in ch or ch[name] is None:
                    continue
                try:
                    num = float(ch[name])
                except (TypeError, ValueError):
                    continue
                if name.lower().endswith("ms"):
                    return num / 1000.0
                if num > 100000:
                    return num / 1000.0
                return num
            return 0.0

        start = _field("start_sec", "startOffsetSec", "startTimeSec",
                       "startOffsetMs", "startTimeMs", "start")
        length = _field("length_sec", "lengthSec",
                        "lengthMs", "length")
        out.append({"title": ch.get("title", "") or "",
                    "start_sec": float(start),
                    "length_sec": float(length)})
    return out


def _probe_duration(path: str) -> tuple[float | None, str | None]:
    """(duration_seconds, error_or_None) for one file. Never raises."""
    try:
        return float(build_mod.probe(path)["duration"]), None
    except build_mod.BuildError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 — a probe must never kill the check
        return None, f"Unexpected probe error for {path}: {exc!r}"


def run_health_check(output_path: str, files: list[dict],
                     metadata: dict | None,
                     duration_tolerance_pct: float = DURATION_TOLERANCE_PCT,
                     duration_tolerance_min_seconds: float = DURATION_TOLERANCE_MIN_SECONDS,
                     ) -> dict:
    """Run every check on a finished .m4b. Never raises — a check
    that cannot run is recorded as a problem, not skipped silently.

    files: the confirmed-order input records, each with at least
      {name, duration, path?, chapter_title?, guessed_position?,
       track?, disc?}. Durations come from the record; integrity
      is re-probed from disk when the file still exists (the
      original path — sources stay in place until the book is
      successfully in Plex, so at check time the original path
      is where the file is; a ``processed_path`` is used only if
      one was recorded by an earlier successful placement).
    Returns the full report dict (also saved to
    data/jobs/<id>-health.json by the caller).
    """
    meta = metadata or {}
    expected_total = expected_runtime_seconds(meta)
    expected_chs = expected_chapters(meta)
    diagnoses: list[str] = []
    problems: list[str] = []   # fail-level
    warnings: list[str] = []   # warn-level
    checks: list[dict] = []

    def _check(name: str, ok: bool, detail: str, level: str = "fail") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "level": level})
        if not ok:
            (problems if level == "fail" else warnings).append(detail)

    # --- 1. Output integrity + duration --------------------------------
    output_duration, output_error = _probe_duration(output_path)
    if output_duration is None:
        _check("output_valid", False,
               f"Finished file failed validation (ffprobe): "
               f"{output_error or 'unknown error'} — the file may be "
               f"corrupt. It was NOT placed in Plex.")
        output_duration = 0.0
    else:
        _check("output_valid", True,
               f"Finished file probes cleanly: {fmt_hms(output_duration)} "
               f"({output_duration:.1f}s).")

    # --- 2. Input integrity + summed duration ---------------------------
    input_total = 0.0
    corrupt: list[str] = []
    zero_byte: list[str] = []
    for f in files:
        dur = float(f.get("duration") or 0)
        input_total += dur
        name = f.get("name") or os.path.basename(f.get("path", "?"))
        src = f.get("processed_path") or f.get("path") or ""
        if src and os.path.exists(src):
            if os.path.getsize(src) == 0:
                zero_byte.append(name)
            else:
                _dur, err = _probe_duration(src)
                if err is not None:
                    corrupt.append(f"{name}: {err}")
        elif dur <= 0:
            corrupt.append(f"{name}: no duration recorded and the "
                           f"source file is gone — cannot verify it.")
    if corrupt:
        _check("inputs_valid", False,
               "Corrupt / unverifiable input file(s): " + "; ".join(corrupt))
    elif zero_byte:
        _check("inputs_valid", False,
               "Zero-byte input file(s): " + ", ".join(zero_byte) +
               " — an empty file cannot be part of the book.")
    else:
        _check("inputs_valid", True,
               f"All {len(files)} input file(s) probe cleanly "
               f"({fmt_hms(input_total)} total).")

    # --- 3. Input vs output duration (the build's own gate, reported) ---
    if input_total > 0 and output_duration > 0:
        diff_pct = abs(output_duration - input_total) / input_total * 100
        if diff_pct <= INPUT_OUTPUT_TOLERANCE_PCT:
            _check("input_vs_output", True,
                   f"Output {fmt_hms(output_duration)} vs summed inputs "
                   f"{fmt_hms(input_total)} — difference {diff_pct:.2f}% "
                   f"(limit {INPUT_OUTPUT_TOLERANCE_PCT}%).")
        else:
            _check("input_vs_output", False,
                   f"Output {fmt_hms(output_duration)} does not match the "
                   f"summed inputs {fmt_hms(input_total)} — difference "
                   f"{diff_pct:.2f}% (limit "
                   f"{INPUT_OUTPUT_TOLERANCE_PCT}%). The merge itself "
                   f"looks incomplete or padded.")
    elif input_total <= 0:
        _check("input_vs_output", False,
               "Summed input duration is zero — the inputs carry no "
               "usable audio length to compare against.")

    # --- 4. Per-chapter table --------------------------------------------
    chapters: list[dict] = []
    start = 0.0
    for i, f in enumerate(files):
        dur = float(f.get("duration") or 0)
        title = (f.get("chapter_title")
                 or os.path.splitext(f.get("name") or f"Part {i + 1}")[0])
        exp = expected_chs[i] if i < len(expected_chs) else None
        exp_dur = float(exp["length_sec"]) if exp else None
        exp_title = (exp or {}).get("title") or ""
        status = "OK"
        note = ""
        if dur <= 0:
            status = "MISSING"
            note = "Zero-length — this part contributed nothing."
        elif exp_dur and exp_dur > 0 and dur < exp_dur * SHORT_CHAPTER_FRACTION:
            status = "SHORT"
            note = (f"Only {fmt_mmss(dur)} against an expected "
                    f"{fmt_mmss(exp_dur)} — less than half the expected "
                    f"length, check it is not truncated.")
        elif exp_dur is None and dur < SHORT_CHAPTER_SECONDS and input_total > 60:
            # No expected length to compare against, but in a book
            # longer than a minute a sub-10-second part is anomalous
            # (a truncated file, not a real chapter). Short test
            # clips and genuinely short books are not flagged.
            status = "SHORT"
            note = (f"Only {fmt_mmss(dur)} long — suspiciously short, "
                    f"check it is not truncated.")
        elif exp_dur and exp_dur > 0 and abs(dur - exp_dur) / exp_dur > 0.5:
            status = "UNCHECKED"
            note = (f"Length {fmt_mmss(dur)} differs a lot from the "
                    f"expected {fmt_mmss(exp_dur)} — worth a listen.")
        chapters.append({
            "number": i + 1,
            "title": title,
            "expected_title": exp_title,
            "expected_sec": exp_dur,
            "actual_sec": dur,
            "start_sec": start,
            "start_timestamp": fmt_timestamp(start),
            "status": status,
            "note": note,
        })
        start += dur
    # Expected chapters with no matching file at all.
    missing_chapters: list[dict] = []
    if len(expected_chs) > len(files):
        for j in range(len(files), len(expected_chs)):
            exp = expected_chs[j]
            missing_chapters.append({
                "number": j + 1,
                "title": exp.get("title") or f"Chapter {j + 1}",
                "expected_title": exp.get("title") or "",
                "expected_sec": float(exp.get("length_sec") or 0),
                "actual_sec": None,
                "start_sec": float(exp.get("start_sec") or 0),
                "start_timestamp": fmt_timestamp(exp.get("start_sec") or 0),
                "status": "MISSING",
                "note": "No input file for this chapter.",
            })
    for row in chapters:
        if row["status"] == "SHORT":
            warnings.append(
                f"Chapter {row['number']} ({row['title']}) is "
                f"{fmt_mmss(row['actual_sec'])} long — {row['note']} "
                f"It starts at {row['start_timestamp']} in the output.")
        elif row["status"] == "MISSING":
            problems.append(
                f"Chapter {row['number']} ({row['title']}) is zero-length "
                f"in the finished file — it contributed nothing.")
    for row in missing_chapters:
        problems.append(
            f"Expected {len(expected_chs)} chapters, found "
            f"{len(files)} file(s) — chapter/part {row['number']} "
            f"('{row['title']}') appears missing. It should be "
            f"{fmt_mmss(row['expected_sec'])} long and start at "
            f"{row['start_timestamp']} in the output.")

    # --- 5. File count vs expected chapters ------------------------------
    if expected_chs:
        if len(files) < len(expected_chs):
            _check("chapter_count", False,
                   f"Expected {len(expected_chs)} chapters, found "
                   f"{len(files)} file(s) — "
                   f"{len(expected_chs) - len(files)} chapter(s) missing.",
                   level="fail")
        elif len(files) > len(expected_chs):
            _check("chapter_count", False,
                   f"Found {len(files)} file(s) but metadata lists only "
                   f"{len(expected_chs)} chapters — "
                   f"{len(files) - len(expected_chs)} extra file(s). "
                   f"Check nothing was duplicated.",
                   level="warn")
        else:
            _check("chapter_count", True,
                   f"File count matches metadata: {len(files)} chapters.")
    else:
        _check("chapter_count", True,
               "No chapter list in metadata — count checked against "
               "the files themselves only.",
               level="warn")

    # --- 6. Numbering gaps / duplicates -----------------------------------
    positions = [f.get("guessed_position") for f in files
                 if isinstance(f.get("guessed_position"), int)]
    if positions:
        seen: set[int] = set()
        dups: set[int] = set()
        for p in positions:
            if p in seen:
                dups.add(p)
            seen.add(p)
        expected_set = set(range(1, len(files) + 1))
        gaps = sorted(expected_set - seen)
        if dups:
            _check("numbering", False,
                   "Duplicate part number(s): "
                   + ", ".join(str(d) for d in sorted(dups)) +
                   " — two files claim the same position; one of "
                   "them may be the wrong file.",
                   level="warn")
        elif gaps:
            _check("numbering", False,
                   "Gap(s) in the part numbering: missing position(s) "
                   + ", ".join(str(g) for g in gaps) +
                   " — a file may be missing from the middle of the book.",
                   level="warn")
        else:
            _check("numbering", True,
                   "Part numbering is contiguous 1–"
                   f"{len(files)} with no duplicates.")
    else:
        _check("numbering", True,
               "No track/part numbers to check — order came from "
               "filenames only.",
               level="warn")

    # --- 7. Metadata total vs actual (the headline check) -----------------
    missing_seconds: float | None = None
    if expected_total and expected_total > 0 and output_duration > 0:
        diff_sec = output_duration - expected_total
        missing_seconds = -diff_sec if diff_sec < 0 else None
        diff_pct = abs(diff_sec) / expected_total * 100
        tolerance = max(expected_total * duration_tolerance_pct / 100.0,
                        duration_tolerance_min_seconds)
        if abs(diff_sec) <= tolerance:
            _check("metadata_duration", True,
                   f"Output {fmt_hms(output_duration)} vs metadata "
                   f"{fmt_hms(expected_total)} — difference "
                   f"{fmt_hms(abs(diff_sec))} ({diff_pct:.1f}%, "
                   f"tolerance {fmt_hms(tolerance)}).")
        else:
            if diff_sec < 0:
                detail = (f"Expected {fmt_hms(expected_total)}, got "
                          f"{fmt_hms(output_duration)} — "
                          f"{fmt_hms(-diff_sec)} missing "
                          f"({diff_pct:.1f}%, tolerance "
                          f"{fmt_hms(tolerance)}).")
            else:
                detail = (f"Expected {fmt_hms(expected_total)}, got "
                          f"{fmt_hms(output_duration)} — "
                          f"{fmt_hms(diff_sec)} EXTRA "
                          f"({diff_pct:.1f}%, tolerance "
                          f"{fmt_hms(tolerance)}). The finished file is "
                          f"longer than the book should be — check for "
                          f"a duplicated part.")
            _check("metadata_duration", False, detail, level="fail")
    elif expected_total is None:
        _check("metadata_duration", True,
               "No total runtime in metadata — nothing to compare the "
               "total length against. This check was skipped, not passed.",
               level="warn")

    # --- 8. Plain-language diagnoses --------------------------------------
    diagnoses = list(problems) + list(warnings)
    if missing_seconds and missing_seconds > 0:
        # Attribute the missing time. Best suspect first: a chapter
        # that LOST about the missing amount (expected − actual ≈
        # shortfall — e.g. chapter 3 should be 2h but is 1h12m, and
        # 48 minutes are missing overall). Next: an expected chapter
        # with no file at all, whose whole length ≈ the shortfall.
        suspects: list[tuple[dict, str]] = []
        for row in chapters:
            exp_dur = row.get("expected_sec")
            actual = row.get("actual_sec") or 0
            if exp_dur and exp_dur > actual:
                loss = exp_dur - actual
                if abs(loss - missing_seconds) <= max(60.0, missing_seconds * 0.25):
                    suspects.append((row, "lost"))
        for row in missing_chapters:
            exp_dur = row.get("expected_sec") or 0
            if exp_dur and abs(exp_dur - missing_seconds) <= max(
                    60.0, missing_seconds * 0.25):
                suspects.append((row, "absent"))
        if suspects:
            for row, kind in suspects:
                if kind == "lost":
                    diagnoses.append(
                        f"Chapter {row['number']} ({row['title']}) is "
                        f"{fmt_mmss(row.get('actual_sec') or 0)} long "
                        f"against an expected "
                        f"{fmt_mmss(row.get('expected_sec') or 0)} — "
                        f"{fmt_hms(missing_seconds)} is missing from the "
                        f"total, and this chapter accounts for it. Verify "
                        f"chapter {row['number']} is complete; it should "
                        f"start at {row['start_timestamp']} in the output.")
                else:
                    diagnoses.append(
                        f"Chapter {row['number']} ({row['title']}) is "
                        f"{fmt_mmss(row.get('expected_sec') or 0)} long in "
                        f"the metadata, and {fmt_hms(missing_seconds)} is "
                        f"missing from the total — verify chapter "
                        f"{row['number']} is included in the file; it "
                        f"should start at {row['start_timestamp']} in "
                        f"the output.")
        else:
            short_rows = [r for r in chapters if r["status"] == "SHORT"]
            if short_rows:
                row = short_rows[0]
                diagnoses.append(
                    f"{fmt_hms(missing_seconds)} is missing from the "
                    f"total, and chapter {row['number']} "
                    f"({row['title']}) is only "
                    f"{fmt_mmss(row['actual_sec'])} long — check that "
                    f"chapter first; it starts at "
                    f"{row['start_timestamp']} in the output.")
            else:
                diagnoses.append(
                    f"{fmt_hms(missing_seconds)} is missing from the "
                    f"total length, but no single chapter accounts for "
                    f"it — a whole part may be missing, or several parts "
                    f"may each be short. Compare the chapter table below "
                    f"against the book's chapter list.")
    if not diagnoses:
        if expected_total is None and not expected_chs:
            diagnoses.append(
                "Limited check — no total runtime or chapter list in "
                "the metadata, so the total length could not be "
                "verified against the book's published length. The "
                "inputs, the merge and the file integrity all checked out.")
        else:
            diagnoses.append(
                f"All checks passed: {fmt_hms(output_duration)} total, "
                f"{len(files)} chapter(s), nothing missing.")

    limited = expected_total is None and not expected_chs
    if problems:
        verdict = "fail"
    elif warnings:
        verdict = "warn"
    elif limited:
        verdict = "limited"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "limited": limited,
        "diagnoses": diagnoses,
        "problems": problems,
        "warnings": warnings,
        "checks": checks,
        "chapters": chapters + missing_chapters,
        "expected_total_sec": expected_total,
        "expected_total": fmt_hms(expected_total),
        "actual_total_sec": output_duration,
        "actual_total": fmt_hms(output_duration),
        "input_total_sec": input_total,
        "input_total": fmt_hms(input_total),
        "missing_sec": missing_seconds,
        "missing": fmt_hms(missing_seconds) if missing_seconds else None,
        "expected_chapters": len(expected_chs),
        "file_count": len(files),
        "output_path": output_path,
    }


def health_report_path(jobs_dir: str, job_id: str) -> str:
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")
    return os.path.join(jobs_dir, f"{safe}-health.json")


def save_health_report(jobs_dir: str, job_id: str, report: dict) -> str:
    """Persist the report to data/jobs/<id>-health.json."""
    os.makedirs(jobs_dir, exist_ok=True)
    path = health_report_path(jobs_dir, job_id)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return path


def load_health_report(jobs_dir: str, job_id: str) -> dict | None:
    """Load a saved report, or None if the job has none yet."""
    try:
        with open(health_report_path(jobs_dir, job_id),
                   encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
