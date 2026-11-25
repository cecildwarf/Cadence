"""
Cadence — auto-sort.

Given a list of audio files for one book, assign each a guessed
position, a confidence (high / medium / low) and a human-readable
reason, then return them in guessed reading order.

Priority order (highest first):
  (a) embedded track / disc numbers from ID3 / MP4 tags
  (b) numbers in the filename — leading numbers, "Part 01",
      "Chapter 12", "CD1-03", "01 of 24", disc/track split
  (c) natural sort on the filename as fallback
  (d) file mtime only as a last-resort tiebreak, flagged low-confidence

Also detects and flags: duplicate numbers, gaps in the sequence,
files with no number at all, mismatched disc numbers.

Pure functions — no I/O, no ffmpeg, no network — so they are easy
to unit-test (see tests/test_sort.py).

A file is a dict with at least:
    {"path": str, "name": str, "mtime": float}
and optionally:
    {"track": int | None, "disc": int | None,
     "track_total": int | None, "disc_total": int | None}
"""

from __future__ import annotations

import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# Filename number parsing
# ---------------------------------------------------------------------------

# CD / disc + track, e.g. "CD1-03", "Disc 2 Track 4", "CD 1_12", "1-03"
_DISC_TRACK_RE = re.compile(
    r"(?:cd|disc|disk|d)\s*(\d+)\s*[-_\s.]+(?:track|tr|t|ch(?:apter)?)?\s*(\d+)",
    re.IGNORECASE,
)
# "Part 03", "Pt. 3", "Volume 2", "Vol 1" — a part/volume number alone.
_PART_RE = re.compile(
    r"(?:part|pt|volume|vol|book)\s*\.?\s*(\d+)",
    re.IGNORECASE,
)
# "Chapter 12", "Ch 12", "Chap. 3"
_CHAPTER_RE = re.compile(
    r"(?:chapter|chap|ch)\s*\.?\s*(\d+)",
    re.IGNORECASE,
)
# "Track 03", "Tr 3"
_TRACK_RE = re.compile(r"(?:track|tr)\s*\.?\s*(\d+)", re.IGNORECASE)
# "01 of 24", "3 of 18"
_OF_RE = re.compile(r"(\d+)\s+of\s+(\d+)", re.IGNORECASE)
# Leading number: "01 - Title", "03_Title", "12. Title", "(04) Title", "[05]"
_LEADING_RE = re.compile(r"^\s*[\(\[]?\s*(\d{1,4})\s*[\)\]\s._\-–—]+")
# Trailing number before the extension: "Title - 03", "Title_12"
_TRAILING_RE = re.compile(r"[\s._\-–—]+(\d{1,4})\s*$")
# Any standalone number as a last resort inside the stem.
_ANY_NUMBER_RE = re.compile(r"(?<![\d.])(\d{1,4})(?![\d.])")


def natural_key(text: str) -> list:
    """Split text into digit / non-digit chunks for natural sorting.

    'Chapter 10' sorts after 'Chapter 9' (plain string sort would
    put 10 before 9).
    """
    parts = re.split(r"(\d+)", text.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def parse_filename_number(name: str) -> dict | None:
    """Extract (disc, track, kind, raw) from a filename, or None.

    ``kind`` is one of: disc-track, part, chapter, track, of-total,
    leading, trailing, embedded. The order of the checks below is
    the priority order — the most explicit signal wins.
    """
    stem = os.path.splitext(os.path.basename(name))[0]

    m = _DISC_TRACK_RE.search(stem)
    if m:
        return {"disc": int(m.group(1)), "track": int(m.group(2)),
                "kind": "disc-track", "raw": m.group(0)}
    m = _CHAPTER_RE.search(stem)
    if m:
        return {"disc": None, "track": int(m.group(1)),
                "kind": "chapter", "raw": m.group(0)}
    m = _PART_RE.search(stem)
    if m:
        return {"disc": None, "track": int(m.group(1)),
                "kind": "part", "raw": m.group(0)}
    m = _TRACK_RE.search(stem)
    if m:
        return {"disc": None, "track": int(m.group(1)),
                "kind": "track", "raw": m.group(0)}
    m = _OF_RE.search(stem)
    if m:
        return {"disc": None, "track": int(m.group(1)),
                "kind": "of-total", "raw": m.group(0),
                "total": int(m.group(2))}
    m = _LEADING_RE.match(stem)
    if m:
        return {"disc": None, "track": int(m.group(1)),
                "kind": "leading", "raw": m.group(0)}
    m = _TRAILING_RE.search(stem)
    if m:
        return {"disc": None, "track": int(m.group(1)),
                "kind": "trailing", "raw": m.group(0)}
    m = _ANY_NUMBER_RE.search(stem)
    if m:
        return {"disc": None, "track": int(m.group(1)),
                "kind": "embedded", "raw": m.group(0)}
    return None


def _effective_number(disc: int | None, track: int | None) -> int | None:
    """Collapse (disc, track) into a single sortable number.

    Disc 2 track 3 -> 1003, so disc boundaries sort correctly and
    a single-disc book (disc 1 / None) keeps its plain track number.
    """
    if track is None:
        return None
    return (disc - 1) * 1000 + track if disc else track


# ---------------------------------------------------------------------------
# Auto-sort
# ---------------------------------------------------------------------------

def auto_sort(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Sort one book's files and annotate each with position/confidence.

    Returns:
        {
          "ordered": [file dicts, each with added keys
                      guessed_position, confidence, reason, sort_signal],
          "warnings": [str, ...],
          "summary": {"total": n, "high": n, "medium": n, "low": n,
                      "numbered": n, "unnumbered": n, "duplicates": n,
                      "gaps": [int, ...], "discs_seen": [int, ...]},
        }

    The input list is not mutated; each entry is shallow-copied.
    """
    warnings: list[str] = []
    enriched: list[dict[str, Any]] = []

    for f in files:
        entry = dict(f)
        name = entry.get("name") or os.path.basename(entry.get("path", ""))
        entry["name"] = name
        tag_track = entry.get("track")
        tag_disc = entry.get("disc")
        parsed = parse_filename_number(name)

        number: int | None = None
        confidence = "low"
        reason = ""
        signal = "none"

        if tag_track is not None:
            # (a) Embedded tags win — they are the most reliable signal.
            number = _effective_number(tag_disc, tag_track)
            confidence = "high"
            signal = "tag"
            if tag_disc:
                reason = f"ID3/MP4 track {tag_track} (disc {tag_disc})"
            else:
                reason = f"ID3/MP4 track {tag_track}"
            # Cross-check: if the filename disagrees with the tag, say so.
            if parsed is not None and parsed["track"] != tag_track:
                warnings.append(
                    f"{name}: tag track {tag_track} disagrees with "
                    f"filename number {parsed['track']} "
                    f"({parsed['kind']} '{parsed['raw'].strip()}') — "
                    f"trusting the tag."
                )
                confidence = "medium"
                reason += " — filename disagrees, trusting tag"
        elif parsed is not None:
            # (b) Filename numbers.
            number = _effective_number(parsed.get("disc"), parsed["track"])
            signal = f"filename:{parsed['kind']}"
            kind = parsed["kind"]
            if kind in ("disc-track", "chapter", "part", "track", "leading", "of-total"):
                confidence = "high"
            elif kind == "trailing":
                confidence = "medium"
            else:  # embedded — a bare number somewhere in the middle
                confidence = "medium"
            disc_txt = f"disc {parsed['disc']} " if parsed.get("disc") else ""
            reason = (f"Filename {kind} number {parsed['track']} "
                      f"({disc_txt}'{parsed['raw'].strip()}')")
        else:
            # (c)/(d) No number anywhere — natural filename order,
            # mtime only as a tiebreak. Flag it clearly.
            number = None
            confidence = "low"
            signal = "natural+mtime"
            reason = ("No track number in tags or filename — "
                      "natural filename order, mtime tiebreak "
                      "(low confidence, please check)")

        entry["_number"] = number
        entry["_confidence"] = confidence
        entry["_reason"] = reason
        entry["_signal"] = signal
        entry["_natural"] = natural_key(name)
        entry["_mtime"] = float(entry.get("mtime") or 0)
        enriched.append(entry)

    # --- duplicate / disc / gap detection (before sorting) -------------
    seen_numbers: dict[int, list[str]] = {}
    for e in enriched:
        if e["_number"] is not None:
            seen_numbers.setdefault(e["_number"], []).append(e["name"])
    duplicates = {n: names for n, names in seen_numbers.items() if len(names) > 1}
    for num, names in sorted(duplicates.items()):
        warnings.append(
            f"Duplicate position {num}: {len(names)} files claim it — "
            + ", ".join(names)
            + ". They are ordered by filename; please check."
        )
        for e in enriched:
            if e["_number"] == num:
                e["_confidence"] = "medium" if e["_confidence"] == "high" else e["_confidence"]
                e["_reason"] += " — DUPLICATE number, check order"

    # Discs from tags OR from filenames (e.g. CD1-03) — both count.
    discs_seen = sorted({
        d for e in enriched
        for d in (e.get("disc"),
                  (parse_filename_number(e["name"]) or {}).get("disc"))
        if d
    })
    if len(discs_seen) > 1:
        warnings.append(
            f"Multiple discs seen: {discs_seen}. Files are ordered "
            f"disc-by-disc; check no disc is missing."
        )
    tag_discs = {e.get("disc") for e in enriched if e.get("disc") is not None}
    filename_discs = {
        (parse_filename_number(e["name"]) or {}).get("disc")
        for e in enriched
    } - {None}
    if tag_discs and filename_discs and tag_discs != filename_discs:
        warnings.append(
            f"Mismatched disc numbers: tags say discs {sorted(tag_discs)}, "
            f"filenames say discs {sorted(filename_discs)}. "
            f"Tags win; please check."
        )
    # Files with a disc number mixed with files without one.
    has_disc = [e["name"] for e in enriched if e.get("disc") or
                (parse_filename_number(e["name"]) or {}).get("disc")]
    if has_disc and len(has_disc) != len(enriched):
        warnings.append(
            "Some files carry a disc number and some do not — "
            "the disc-less files may be misplaced. Please check."
        )

    numbers = sorted(n for n in (e["_number"] for e in enriched) if n is not None)
    gaps: list[int] = []
    if numbers:
        # Gaps only make sense for a single-disc run starting near 1.
        # (Disc-collapsed numbers like 1003 would fake a huge gap.)
        plain = [n for n in numbers if n < 1000]
        if plain and min(plain) >= 1 and len(plain) >= 2:
            expected = set(range(min(plain), max(plain) + 1))
            gaps = sorted(expected - set(plain))
            if gaps:
                warnings.append(
                    f"Gap in sequence: missing position(s) {gaps} "
                    f"(have {min(plain)}–{max(plain)}). "
                    f"A file may be missing, or a number misread."
                )

    unnumbered = [e["name"] for e in enriched if e["_number"] is None]
    if unnumbered:
        warnings.append(
            f"{len(unnumbered)} file(s) have no number at all and were "
            f"placed by filename order (low confidence): "
            + ", ".join(unnumbered)
        )

    # --- sort -----------------------------------------------------------
    # Numbered files first, in number order; unnumbered after them in
    # natural filename order. Within equal numbers, natural filename
    # order, then mtime as the last tiebreak.
    def sort_key(e: dict[str, Any]):
        return (
            0 if e["_number"] is not None else 1,
            e["_number"] if e["_number"] is not None else 0,
            e["_natural"],
            e["_mtime"],
        )

    ordered = sorted(enriched, key=sort_key)

    result: list[dict[str, Any]] = []
    counts = {"high": 0, "medium": 0, "low": 0}
    for i, e in enumerate(ordered, start=1):
        out = {k: v for k, v in e.items() if not k.startswith("_")}
        out["guessed_position"] = i
        out["confidence"] = e["_confidence"]
        out["reason"] = e["_reason"]
        out["sort_signal"] = e["_signal"]
        counts[e["_confidence"]] += 1
        result.append(out)

    summary = {
        "total": len(result),
        "high": counts["high"],
        "medium": counts["medium"],
        "low": counts["low"],
        "numbered": len(numbers),
        "unnumbered": len(unnumbered),
        "duplicates": len(duplicates),
        "gaps": gaps,
        "discs_seen": discs_seen,
    }
    return {"ordered": result, "warnings": warnings, "summary": summary}
