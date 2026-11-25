"""Plex output path tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plex_output import plex_output_path, sanitise, cover_path_for


def test_simple_book():
    p = plex_output_path("/disks/Audiobooks",
                         {"author": "Brandon Sanderson", "title": "The Way of Kings"})
    assert p == "/disks/Audiobooks/Brandon Sanderson/The Way of Kings/The Way of Kings.m4b"


def test_series_book():
    p = plex_output_path("/disks/Audiobooks",
                         {"author": "Brandon Sanderson", "title": "The Way of Kings",
                          "series": "The Stormlight Archive", "series_position": "1",
                          "year": "2010"})
    assert p == ("/disks/Audiobooks/Brandon Sanderson/The Stormlight Archive/"
                 "2010 - The Way of Kings [The Stormlight Archive 1]/"
                 "The Way of Kings (2010) [The Stormlight Archive 1].m4b")


def test_sanitise_strips_illegal_chars_but_stays_readable():
    assert sanitise('Book: A *Test*? "Quoted"') == "Book A Test Quoted"
    assert sanitise("  ") == "Unknown"
    assert sanitise("A/B\\C") == "ABC"


def test_cover_sits_beside_m4b():
    p = plex_output_path("/out", {"author": "A", "title": "T"})
    assert cover_path_for(p) == "/out/A/T/cover.jpg"
