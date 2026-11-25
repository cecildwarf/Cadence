"""
Cadence — Plex output paths.

Plex has no native audiobook library type; the community-recommended
setup is a separate Music library pointed at an author/book folder
tree, with "Prefer local metadata" on. The structure this module
builds is the one Plex's music agent matches most reliably:

    Author/Book Title/Book Title.m4b
    Author/Series Name/Year - Book Title [Series #]/Book Title (Year) [Series #].m4b

Rules:
  * One folder per book, one .m4b per book (the merged file).
  * Folder and file names carry the same author / title / series
    info as the embedded tags, so they can never contradict each
    other (Plex ignores files whose tags and folders disagree).
  * Filenames are sanitised for Windows / Linux / Plex safety:
    : ? * " < > | and control chars are stripped, / and \\ become
    "-", trailing dots/spaces are dropped, but the name stays
    human-readable — no hash, no slug.
"""

from __future__ import annotations

import os
import re

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN = re.compile(r"\s+")


def sanitise(name: str, fallback: str = "Unknown") -> str:
    """Make a string safe as a single path component, keep it readable."""
    if not name:
        return fallback
    name = _ILLEGAL.sub("", str(name))
    name = _WHITESPACE_RUN.sub(" ", name).strip()
    name = name.strip(". ")  # Windows hates trailing dots/spaces
    if not name:
        return fallback
    # Plex / Windows path-component limit is 255 chars; stay well under.
    return name[:180]


def plex_output_path(output_dir: str, metadata: dict) -> str:
    """Full destination path for the finished .m4b.

    metadata keys used: author, title, series, series_position, year.
    """
    author = sanitise(metadata.get("author") or metadata.get("authors") or "Unknown Author")
    title = sanitise(metadata.get("title") or "Unknown Title")
    series = (metadata.get("series") or "").strip()
    series_pos = (metadata.get("series_position") or "").strip()
    year = str(metadata.get("year") or "").strip()

    if series:
        series_folder = sanitise(series)
        book_folder_name = f"{year} - {title}" if year else title
        if series_pos:
            book_folder_name += f" [{series} {series_pos}]"
            file_name = f"{title} ({year}) [{series} {series_pos}].m4b" if year \
                else f"{title} [{series} {series_pos}].m4b"
        else:
            file_name = f"{title} ({year}).m4b" if year else f"{title}.m4b"
        return os.path.join(output_dir, author, series_folder,
                            book_folder_name, file_name)

    file_name = f"{title} ({year}).m4b" if year else f"{title}.m4b"
    return os.path.join(output_dir, author, title, file_name)


def cover_path_for(output_m4b_path: str) -> str:
    """cover.jpg sits next to the finished .m4b in the same folder."""
    return os.path.join(os.path.dirname(output_m4b_path), "cover.jpg")
