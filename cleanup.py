"""
Cadence — safe cleanup of processed originals.

A processed book is "safe to remove" ONLY if ALL hold:
  (a) a matching completed .m4b exists in the output tree,
  (b) that .m4b is a valid audio file (ffprobe succeeds, duration > 0),
  (c) its duration is within 2% of the summed duration of the
      processed source files — or of the recorded job duration in
      data/jobs/<id>.json when one is available,
  (d) the job record for it is Done (not Failed / Cancelled).
If ANY check fails, or the match is ambiguous, the item goes in
"needs review" with the reason why — never on the safe list, never
deletable from it.

Deleting only ever touches files under the configured Processed
folder. Every delete path is validated to be inside Processed
first; anything else is refused and logged. Deletion stops at the
first failure and reports it — never silently skips.

Pure functions (no Flask): the matcher takes paths + job records,
so it is easy to unit-test (see tests/test_cleanup.py).
"""

from __future__ import annotations

import os
import shutil

import build as build_mod
from plex_output import sanitise

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".aac", ".ogg", ".wav"}
DURATION_TOLERANCE_PCT = 2.0


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _audio_files_under(folder: str) -> list[str]:
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                found.append(os.path.join(root, name))
    return sorted(found)


def scan_processed(processed_dir: str) -> list[dict]:
    """One item per processed book folder.

    Layout is Processed/<Author>/<Book>/... (audio recursively
    under the book folder). A book directly under Processed/
    (single level, e.g. from an older layout) is also recognised,
    with author unknown. Empty folders are skipped.
    """
    items = []
    if not processed_dir or not os.path.isdir(processed_dir):
        return items
    for author_name in sorted(os.listdir(processed_dir)):
        author_path = os.path.join(processed_dir, author_name)
        if not os.path.isdir(author_path):
            continue
        direct_audio = [f for f in os.listdir(author_path)
                        if os.path.isfile(os.path.join(author_path, f))
                        and os.path.splitext(f)[1].lower() in AUDIO_EXTS]
        if direct_audio:
            # Single-level book: Processed/<Book>/file.mp3
            items.append({"author": "", "title": author_name,
                          "processed_path": author_path,
                          "files": _audio_files_under(author_path)})
            continue
        for book_name in sorted(os.listdir(author_path)):
            book_path = os.path.join(author_path, book_name)
            if not os.path.isdir(book_path):
                continue
            files = _audio_files_under(book_path)
            if files:
                items.append({"author": author_name, "title": book_name,
                              "processed_path": book_path, "files": files})
    return items


def _probe_durations(files: list[str]) -> tuple[float, list[str]]:
    """Sum of ffprobe durations. Returns (total_seconds, [problems]).
    Any file that cannot be probed is a problem, not a silent skip."""
    total = 0.0
    problems = []
    for path in files:
        try:
            total += float(build_mod.probe(path)["duration"])
        except build_mod.BuildError as exc:
            problems.append(f"Could not probe {os.path.basename(path)}: {exc}")
    return total, problems


def _find_output_candidates(output_dir: str, author: str, title: str) -> list[str]:
    """Completed .m4b files whose folder names match Author/Book."""
    found = []
    if not output_dir or not os.path.isdir(output_dir):
        return found
    for root, _dirs, names in os.walk(output_dir):
        for name in names:
            if os.path.splitext(name)[1].lower() != ".m4b":
                continue
            book_folder = os.path.basename(root)
            author_folder = os.path.basename(os.path.dirname(root))
            # Book folders may carry a year/series prefix
            # ("2010 - Title [Series 1]"), so the title needs to be
            # contained in the folder name, not equal to it.
            if _norm(sanitise(title)) in _norm(book_folder):
                if not author or _norm(author_folder) == _norm(sanitise(author)):
                    found.append(os.path.join(root, name))
    return sorted(found)


def match_item(item: dict, output_dir: str, done_jobs: list[dict]) -> dict:
    """Check ONE processed book against the output tree + job records.

    Returns the item plus:
      safe: bool, reasons: [str], output_path, output_duration,
      expected_duration, diff_pct, job_id, job_date.
    """
    result = {**item, "safe": False, "reasons": [],
              "output_path": "", "output_duration": 0.0,
              "expected_duration": 0.0, "diff_pct": None,
              "job_id": "", "job_date": ""}
    source_total, problems = _probe_durations(item["files"])
    if problems:
        result["reasons"].extend(problems)
        return result
    result["processed_size"] = sum(
        os.path.getsize(f) for f in item["files"] if os.path.exists(f))

    # (d) Find the Done job for this book: matching author/title
    # (sanitised, case-insensitive) AND the same source filenames.
    item_names = {os.path.basename(f) for f in item["files"]}
    job_matches = []
    for job in done_jobs:
        meta = job.get("metadata") or {}
        # Same fallbacks as the Processed move and the Plex path
        # builder, so a book with no author/title recorded still
        # matches the folder it was actually moved into.
        job_author = sanitise(meta.get("author") or meta.get("authors")
                              or "Unknown Author")
        job_title = sanitise(
            meta.get("title")
            or os.path.basename((job.get("folder") or "").rstrip("/"))
            or "Unknown Book")
        if _norm(job_author) != _norm(sanitise(item["author"])) and item["author"]:
            continue
        if _norm(job_title) != _norm(sanitise(item["title"])):
            continue
        job_names = {os.path.basename(f.get("path", "")) for f in job.get("files") or []}
        if job_names and job_names != item_names:
            continue
        job_matches.append(job)
    if len(job_matches) > 1:
        result["reasons"].append(
            f"Ambiguous match: {len(job_matches)} completed jobs match "
            f"'{item['title']}' — refusing to guess which output is the right one.")
        return result

    job = job_matches[0] if job_matches else None
    output_path = ""
    if job is not None:
        output_path = job.get("output_path") or ""
        result["job_id"] = job.get("id", "")
        result["job_date"] = job.get("finished_at") or job.get("created", "")
        if not output_path or not os.path.isfile(output_path):
            result["reasons"].append(
                "The completed job's output file is missing from the "
                f"output tree: {output_path or '(no path recorded)'}.")
            return result
    else:
        # No job record: fall back to an output-tree search, but the
        # item can never be 'safe' without a Done job to verify (d).
        candidates = _find_output_candidates(output_dir, item["author"], item["title"])
        if len(candidates) > 1:
            result["reasons"].append(
                f"Ambiguous match: {len(candidates)} completed files match "
                f"'{item['title']}' in the output tree and no job record "
                f"pins down the right one.")
            return result
        if len(candidates) == 1:
            output_path = candidates[0]
            result["reasons"].append(
                "No completed job record found for this book, so the "
                "match cannot be verified — review it manually.")
            result["output_path"] = output_path
            return result
        result["reasons"].append(
            "No completed job record and no matching completed file in "
            "the output tree.")
        return result

    # (a) + (b): output exists (checked above) and is valid audio.
    result["output_path"] = output_path
    try:
        out_info = build_mod.probe(output_path)
        out_dur = float(out_info["duration"])
    except build_mod.BuildError as exc:
        result["reasons"].append(
            f"Completed file failed validation (ffprobe): {exc}")
        return result
    if out_dur <= 0:
        result["reasons"].append("Completed file has zero/unknown duration.")
        return result
    result["output_duration"] = out_dur

    # (c): duration within 2% — recorded job duration when available,
    # otherwise the summed processed source durations.
    recorded = float(job.get("output_duration") or 0)
    expected = recorded if recorded > 0 else source_total
    result["expected_duration"] = expected
    if expected <= 0:
        result["reasons"].append("No usable expected duration to compare against.")
        return result
    diff_pct = abs(out_dur - expected) / expected * 100
    result["diff_pct"] = round(diff_pct, 2)
    if diff_pct > DURATION_TOLERANCE_PCT:
        result["reasons"].append(
            f"Duration mismatch: completed file is {out_dur:.1f}s but the "
            f"{'recorded job' if recorded > 0 else 'processed sources'} "
            f"total {expected:.1f}s — difference {diff_pct:.2f}% "
            f"(limit {DURATION_TOLERANCE_PCT}%).")
        return result
    result["safe"] = True
    return result


def scan_cleanup(processed_dir: str, output_dir: str, jobs: list[dict]) -> dict:
    """Full cleanup scan: safe items (biggest first) + needs-review
    items, with the total reclaimable size of the safe list."""
    done_jobs = [j for j in jobs if j.get("status") == "done"]
    items = scan_processed(processed_dir)
    safe: list[dict] = []
    needs_review: list[dict] = []
    for item in items:
        checked = match_item(item, output_dir, done_jobs)
        checked["processed_size"] = checked.get(
            "processed_size",
            sum(os.path.getsize(f) for f in item["files"] if os.path.exists(f)))
        if checked["safe"]:
            safe.append(checked)
        else:
            needs_review.append(checked)
    safe.sort(key=lambda i: i.get("processed_size", 0), reverse=True)
    needs_review.sort(key=lambda i: i.get("processed_size", 0), reverse=True)
    return {
        "safe": safe,
        "needs_review": needs_review,
        "total_reclaimable_bytes": sum(i.get("processed_size", 0) for i in safe),
        "total_reclaimable_count": len(safe),
        "processed_dir": processed_dir,
    }


def _is_inside(base: str, path: str) -> bool:
    base_real = os.path.realpath(base)
    target = os.path.realpath(path)
    return os.path.commonpath([base_real, target]) == base_real


def delete_processed_items(processed_dir: str, items: list[dict]) -> dict:
    """Delete processed book folders, one by one, stopping at the
    first failure. Returns {deleted: [{path, size}], failed: None |
    {path, reason}, freed_bytes}. Only paths inside Processed are
    ever touched — anything else is refused, not deleted."""
    deleted = []
    freed = 0
    for item in items:
        path = item.get("processed_path") or ""
        if not path or not _is_inside(processed_dir, path) or \
                os.path.realpath(path) == os.path.realpath(processed_dir):
            return {"deleted": deleted, "freed_bytes": freed,
                    "failed": {"path": path,
                               "reason": f"Refused: {path or '(empty path)'} is not "
                                         f"inside the Processed folder "
                                         f"{processed_dir} — nothing was deleted for it."}}
        if not os.path.isdir(path):
            return {"deleted": deleted, "freed_bytes": freed,
                    "failed": {"path": path,
                               "reason": f"Processed folder no longer exists: {path}."}}
        size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _d, fs in os.walk(path) for f in fs)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            return {"deleted": deleted, "freed_bytes": freed,
                    "failed": {"path": path,
                               "reason": f"Could not delete {path}: {exc}"}}
        deleted.append({"path": path, "size": size,
                        "title": item.get("title", ""),
                        "author": item.get("author", "")})
        freed += size
    return {"deleted": deleted, "freed_bytes": freed, "failed": None}
