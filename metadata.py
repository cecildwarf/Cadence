"""
Cadence — metadata lookup via audnexus (public, no API key).

Primary source: https://api.audnexus.com/books/<ASIN>
  e.g. https://api.audnexus.com/books/B0C5NYVMW4
Returns title, authors, narrators, description, series name /
position, genres, release year, cover URL (image), chapters
(when present), ASIN, etc.

Fallback search: audnexus also exposes a search endpoint; we try
``/books?title=<title>&author=<author>`` and take the first match.
If both fail, the caller proceeds with manual fields — the lookup
is never allowed to block a build. Every request, response status
and fallback is logged by the caller (see app.py) so a failed
lookup is visible, not silent.

audnexus field mapping (as returned by the API):
  title, authors: [{name}], narrators: [{name}],
  series: [{name, position}], genres: [{name}],
  releaseDate / publicationDate, description, image, chapters.
"""

from __future__ import annotations

import re
import requests

DEFAULT_API_URL = "https://api.audnexus.com"
TIMEOUT = 15

_ASIN_RE = re.compile(r"(?<![A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])", re.IGNORECASE)


def extract_asin(text: str) -> str | None:
    """Pull a bare ASIN (B0...) out of pasted text / a URL / a filename."""
    if not text:
        return None
    m = _ASIN_RE.search(text)
    return m.group(1).upper() if m else None


def _first_name(items) -> str:
    if not items:
        return ""
    first = items[0]
    return first.get("name", "") if isinstance(first, dict) else str(first)


def _all_names(items) -> str:
    if not items:
        return ""
    names = []
    for it in items:
        names.append(it.get("name", "") if isinstance(it, dict) else str(it))
    return ", ".join(n for n in names if n)


def _runtime_seconds(book: dict) -> float | None:
    """Expected total runtime in seconds, from whatever audnexus
    field carries it. The book endpoint reports ``runtimeLengthMin``
    (minutes); the chapters endpoint reports ``runtimeLengthMs`` /
    ``runtimeLengthSec``. A bare ``length`` is ambiguous (minutes
    on the book endpoint): values up to 3000 are read as minutes,
    larger ones as seconds — a real audiobook is never 3000
    seconds (50 min) short of... in practice runtimes are 1–30 h,
    so the split is safe. Returns None when nothing usable exists.
    """
    for key, unit in (("runtime_seconds", "s"), ("runtimeLengthSec", "s"),
                      ("duration_seconds", "s"), ("runtimeLengthMs", "ms"),
                      ("runtimeLengthMin", "min"), ("runtime_minutes", "min"),
                      ("runtimeLengthMinutes", "min"), ("lengthMin", "min"),
                      ("lengthMinutes", "min")):
        val = book.get(key)
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        if unit == "ms":
            return num / 1000.0
        if unit == "min":
            return num * 60.0
        return num
    val = book.get("length")
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    # Bare `length`: minutes when it looks like minutes (<= 3000),
    # seconds when it is clearly a second count.
    return num * 60.0 if num <= 3000 else num


def _chapter_seconds(ch: dict) -> tuple[float, float]:
    """(start_sec, length_sec) for one audnexus chapter entry.

    The book endpoint uses ``startTimeMs``/``lengthMs``; the
    chapters endpoint uses ``startOffsetMs``/``startOffsetSec``/
    ``lengthMs``. Millisecond values are > 100000 for any real
    start/length past ~100 s, so anything above that is ms.
    """
    def _secs(*keys):
        for key in keys:
            val = ch.get(key)
            if val is None:
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if num < 0:
                continue
            if key.lower().endswith("ms"):
                return num / 1000.0
            if num > 100000:
                return num / 1000.0
            return num
        return 0.0

    start = _secs("startOffsetSec", "startOffsetMs", "startTimeMs",
                  "startTimeSec", "start")
    length = _secs("lengthMs", "lengthSec", "length")
    return start, length


def normalise(book: dict) -> dict:
    """Flatten one audnexus book object into Cadence's metadata shape."""
    series_name, series_pos = "", ""
    series = book.get("series") or []
    if series:
        first = series[0] if isinstance(series[0], dict) else {}
        series_name = first.get("name", "") or ""
        series_pos = str(first.get("position", "") or "")

    year = ""
    for key in ("releaseDate", "publicationDate", "publishedDate"):
        val = book.get(key)
        if val:
            year = str(val)[:4]
            break

    chapters = []
    for ch in book.get("chapters") or []:
        if isinstance(ch, dict):
            start_sec, length_sec = _chapter_seconds(ch)
            chapters.append({
                "title": ch.get("title", ""),
                "start": ch.get("startTimeMs", ch.get("start", 0)),
                "length": ch.get("lengthMs", ch.get("length", 0)),
                "start_sec": start_sec,
                "length_sec": length_sec,
            })

    runtime_seconds = _runtime_seconds(book)
    if runtime_seconds is None and chapters:
        # No total given, but chapters add up to one.
        total = sum(c["length_sec"] for c in chapters)
        if total > 0:
            runtime_seconds = total

    return {
        "asin": book.get("asin", ""),
        "title": book.get("title", ""),
        "subtitle": book.get("subtitle", ""),
        "author": _first_name(book.get("authors")),
        "authors": _all_names(book.get("authors")),
        "narrator": _first_name(book.get("narrators")),
        "narrators": _all_names(book.get("narrators")),
        "series": series_name,
        "series_position": series_pos,
        "genres": _all_names(book.get("genres")),
        "genre": _first_name(book.get("genres")),
        "year": year,
        "description": book.get("description", "") or book.get("summary", ""),
        "cover_url": book.get("image", "") or "",
        "chapters": chapters,
        "runtime_seconds": runtime_seconds,
        "runtime_minutes": (runtime_seconds / 60.0
                            if runtime_seconds else None),
        "source": "audnexus",
    }


def fetch_by_asin(asin: str, api_url: str = DEFAULT_API_URL) -> dict:
    """Fetch one book by ASIN. Raises requests / ValueError on failure.

    Returns the normalised dict. The caller logs the request and
    the outcome — success or the exact failure.
    """
    asin = (asin or "").strip().upper()
    if not asin:
        raise ValueError("No ASIN supplied for metadata lookup")
    url = f"{api_url.rstrip('/')}/books/{asin}"
    resp = requests.get(url, timeout=TIMEOUT,
                        headers={"Accept": "application/json"})
    if resp.status_code == 404:
        raise ValueError(f"audnexus: no book found for ASIN {asin} (HTTP 404)")
    resp.raise_for_status()
    data = resp.json()
    if not data or not data.get("title"):
        raise ValueError(f"audnexus: empty/incomplete response for ASIN {asin}")
    result = normalise(data)
    result["asin"] = asin
    return result


def search(title: str, author: str = "", api_url: str = DEFAULT_API_URL) -> dict:
    """Best-effort title/author search. Raises on failure / no match."""
    if not title:
        raise ValueError("No title supplied for metadata search")
    url = f"{api_url.rstrip('/')}/books"
    params = {"title": title}
    if author:
        params["author"] = author
    resp = requests.get(url, params=params, timeout=TIMEOUT,
                        headers={"Accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()
    # The search endpoint may return a list or a single object.
    if isinstance(data, list):
        if not data:
            raise ValueError(f"audnexus: no match for title '{title}'")
        return normalise(data[0])
    if isinstance(data, dict) and data.get("title"):
        return normalise(data)
    raise ValueError(f"audnexus: no match for title '{title}'")
