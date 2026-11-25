"""Auto-sort tests — run: python -m pytest tests/  (or python -m unittest)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sort import auto_sort, parse_filename_number


def _files(names, **kw):
    return [{"path": f"/in/{n}", "name": n, "mtime": 1000.0 + i,
             "track": None, "disc": None, **kw} for i, n in enumerate(names)]


def test_leading_numbers():
    # Filenames arrive shuffled; leading numbers must win.
    out = auto_sort(_files(["03 - Part Three.mp3", "01 - Intro.mp3", "02 - Middle.mp3"]))
    names = [f["name"] for f in out["ordered"]]
    assert names == ["01 - Intro.mp3", "02 - Middle.mp3", "03 - Part Three.mp3"]
    assert all(f["confidence"] == "high" for f in out["ordered"])
    assert out["ordered"][0]["guessed_position"] == 1


def test_part_xx():
    out = auto_sort(_files(["Book Part 2.mp3", "Book Part 10.mp3", "Book Part 1.mp3"]))
    assert [f["name"] for f in out["ordered"]] == [
        "Book Part 1.mp3", "Book Part 2.mp3", "Book Part 10.mp3"]
    assert out["ordered"][0]["reason"].startswith("Filename part number")


def test_chapter_names():
    out = auto_sort(_files(["Chapter 12 - End.mp3", "Chapter 2 - Start.mp3", "Chapter 10.mp3"]))
    assert [f["name"] for f in out["ordered"]] == [
        "Chapter 2 - Start.mp3", "Chapter 10.mp3", "Chapter 12 - End.mp3"]


def test_cd_track_split():
    out = auto_sort(_files(["CD2-01.mp3", "CD1-03.mp3", "CD1-01.mp3", "CD2-02.mp3"]))
    assert [f["name"] for f in out["ordered"]] == [
        "CD1-01.mp3", "CD1-03.mp3", "CD2-01.mp3", "CD2-02.mp3"]
    assert out["summary"]["discs_seen"] == [1, 2]


def test_no_numbers_low_confidence():
    out = auto_sort(_files(["zebra.mp3", "apple.mp3", "mango.mp3"]))
    assert [f["name"] for f in out["ordered"]] == ["apple.mp3", "mango.mp3", "zebra.mp3"]
    assert all(f["confidence"] == "low" for f in out["ordered"])
    assert any("no number" in w.lower() for w in out["warnings"])


def test_duplicates_flagged():
    out = auto_sort(_files(["01 - A.mp3", "01 - B.mp3", "02 - C.mp3"]))
    assert out["summary"]["duplicates"] == 1
    assert any("Duplicate" in w for w in out["warnings"])
    # Duplicates are downgraded from high to medium.
    assert out["ordered"][0]["confidence"] == "medium"


def test_gaps_flagged():
    out = auto_sort(_files(["01.mp3", "02.mp3", "05.mp3"]))
    assert out["summary"]["gaps"] == [3, 4]
    assert any("Gap" in w for w in out["warnings"])


def test_tag_numbers_beat_filename():
    files = _files(["99 - Wrong.mp3", "01 - Right.mp3"])
    files[0]["track"] = 1
    files[1]["track"] = 2
    out = auto_sort(files)
    assert [f["name"] for f in out["ordered"]] == ["99 - Wrong.mp3", "01 - Right.mp3"]
    assert out["ordered"][0]["reason"].startswith("ID3/MP4 track 1")


def test_of_total_and_natural_tiebreak():
    out = auto_sort(_files(["3 of 9.mp3", "1 of 9.mp3", "2 of 9.mp3"]))
    assert [f["name"] for f in out["ordered"]] == ["1 of 9.mp3", "2 of 9.mp3", "3 of 9.mp3"]
    assert parse_filename_number("07 of 12.mp3")["kind"] == "of-total"


def test_parse_filename_number_kinds():
    assert parse_filename_number("CD1-03.mp3")["kind"] == "disc-track"
    assert parse_filename_number("Chapter 12.mp3")["kind"] == "chapter"
    assert parse_filename_number("Part 04.mp3")["kind"] == "part"
    assert parse_filename_number("Track 7.mp3")["kind"] == "track"
    assert parse_filename_number("05 - Title.mp3")["kind"] == "leading"
    assert parse_filename_number("NoNumbersHere.mp3") is None
