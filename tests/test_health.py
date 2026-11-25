"""Health check + quarantine Review tests.

Pure logic: duration vs metadata, missing/short chapters, limited
mode (no metadata), and Cecil's example — 6h expected, 5h12m
actual, 48 minutes missing, blamed on the chapter that lost it.
Flask level: approve moves the held file into Plex (and records
the override) and only then moves the originals to Processed;
reject deletes the held file and leaves the originals in their
source folder. End-to-end: a mismatched build is held in
Review with its originals still in source, a matching build goes
straight to Plex, and a failed build leaves its sources untouched.
Move timing rule: nothing moves until the book is successfully
in Plex.
"""
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod
import build as build_mod
import cadence_logging as flog
import health as health_mod


def _tone(path: str, seconds: int = 2) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "128k", path],
                   check=True, timeout=120)


def _tone_m4b(path: str, seconds: int = 2) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "aac", "-b:a", "96k", path],
                   check=True, timeout=120)


@pytest.fixture(autouse=True)
def _freeze_runner():
    app_mod.QUEUE_RUNNER_ENABLED = False
    yield
    app_mod.QUEUE_RUNNER_ENABLED = True


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    inbox.mkdir(parents=True)
    out.mkdir(parents=True)
    saved_settings = dict(app_mod.SETTINGS)
    monkeypatch.setattr(app_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod, "JOBS_DIR", str(data_dir / "jobs"))
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", str(data_dir / "settings.json"))
    app_mod.SETTINGS.update({
        "inbox_dir": str(inbox), "output_dir": str(out),
        "processed_dir": "", "review_dir": "",
        "temp_dir": str(data_dir / "temp"), "timezone": "UTC",
    })
    flog.init_logging(str(data_dir))
    flog.set_timezone("UTC")
    yield app_mod.app.test_client(), tmp_path
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update(saved_settings)


def _files(tmp_path, name, durations=(2, 2, 2)):
    """Real tone files + matching job file records."""
    folder = tmp_path / "book"
    folder.mkdir(exist_ok=True)
    records = []
    for i, secs in enumerate(durations, start=1):
        path = folder / f"{name} {i:02d}.mp3"
        _tone(str(path), seconds=secs)
        records.append({"path": str(path), "name": path.name,
                        "duration": float(secs), "guessed_position": i,
                        "chapter_title": f"Chapter {i}"})
    return records


def _output(tmp_path, seconds=6):
    out = tmp_path / "out.m4b"
    _tone_m4b(str(out), seconds=seconds)
    return str(out)


def _meta(runtime_seconds=None, chapters=None):
    meta = {}
    if runtime_seconds is not None:
        meta["runtime_seconds"] = float(runtime_seconds)
    if chapters is not None:
        meta["chapters"] = chapters
    return meta


def _chapters(n, each=2):
    return [{"title": f"Chapter {i + 1}", "start_sec": i * each,
             "length_sec": each} for i in range(n)]


# --- pure health checks -----------------------------------------------------

def test_duration_matches_metadata_passes(tmp_path):
    files = _files(tmp_path, "Book")
    report = health_mod.run_health_check(
        _output(tmp_path, 6), files,
        _meta(6.0, _chapters(3, 2)))
    assert report["verdict"] == "pass", report["diagnoses"]
    assert report["actual_total_sec"] == pytest.approx(6.0, abs=0.2)
    assert all(c["status"] == "OK" for c in report["chapters"])
    assert report["chapters"][2]["start_timestamp"] == "00:00:04"


def test_six_hours_expected_five_twelve_actual_is_held(tmp_path, monkeypatch):
    """Cecil's example: 6h expected, 5h12m actual — 48 minutes
    missing, blamed on the chapter that lost exactly that much."""
    files = _files(tmp_path, "Book", durations=(7200, 7200, 4320))
    out = str(tmp_path / "out.m4b")
    _tone_m4b(out, seconds=2)  # real file; durations are stubbed below

    def fake_probe(path):
        if path == out:
            return {"duration": 18720.0, "size": 1, "codec": "aac",
                    "sample_rate": 44100, "channels": 2}
        for f in files:
            if path == f["path"]:
                return {"duration": f["duration"], "size": 1,
                        "codec": "mp3", "sample_rate": 44100, "channels": 2}
        raise AssertionError(f"unexpected probe of {path}")

    monkeypatch.setattr(build_mod, "probe", fake_probe)
    meta = _meta(21600, [{"title": f"Chapter {i + 1}",
                          "start_sec": i * 7200, "length_sec": 7200}
                         for i in range(3)])
    report = health_mod.run_health_check(out, files, meta)
    assert report["verdict"] == "fail", report["diagnoses"]
    assert report["missing_sec"] == pytest.approx(2880.0)
    joined = " ".join(report["diagnoses"])
    assert "48m" in joined or "48 minutes" in joined, joined
    assert "6h 00m" in joined and "5h 12m" in joined, joined
    # The blame lands on chapter 3, with its start timestamp.
    assert "Chapter 3" in joined, joined
    assert "04:00:00" in joined, joined


def test_missing_chapter_count_names_the_chapter(tmp_path):
    files = _files(tmp_path, "Book")  # 3 files
    report = health_mod.run_health_check(
        _output(tmp_path, 6), files,
        _meta(8.0, _chapters(4, 2)))
    assert report["verdict"] == "fail", report["diagnoses"]
    assert report["file_count"] == 3 and report["expected_chapters"] == 4
    joined = " ".join(report["diagnoses"])
    assert "Chapter 4" in joined, joined
    assert any(c["number"] == 4 and c["status"] == "MISSING"
               for c in report["chapters"])


def test_short_chapter_is_flagged(tmp_path):
    files = _files(tmp_path, "Book", durations=(2, 2, 2))
    report = health_mod.run_health_check(
        _output(tmp_path, 6), files,
        _meta(66.0, [{"title": "Chapter 1", "start_sec": 0, "length_sec": 2},
                     {"title": "Chapter 2", "start_sec": 2, "length_sec": 2},
                     {"title": "Chapter 3", "start_sec": 4, "length_sec": 62}]))
    assert report["verdict"] in ("warn", "fail"), report["diagnoses"]
    third = [c for c in report["chapters"] if c["number"] == 3][0]
    assert third["status"] == "SHORT", third
    assert "Chapter 3" in " ".join(report["diagnoses"])


def test_no_metadata_runtime_is_limited_not_held(tmp_path):
    files = _files(tmp_path, "Book")
    report = health_mod.run_health_check(_output(tmp_path, 6), files, {})
    assert report["verdict"] == "limited", report["diagnoses"]
    assert report["limited"] is True
    assert any("Limited" in d for d in report["diagnoses"])


def test_corrupt_output_fails(tmp_path):
    files = _files(tmp_path, "Book")
    bad = tmp_path / "bad.m4b"
    bad.write_bytes(b"not audio at all" * 100)
    report = health_mod.run_health_check(str(bad), files, _meta(6.0))
    assert report["verdict"] == "fail"
    assert any("ffprobe" in d.lower() or "validation" in d.lower()
               for d in report["diagnoses"])


def test_metadata_runtime_parsing():
    assert health_mod.expected_runtime_seconds(
        {"runtimeLengthMin": 360}) == pytest.approx(21600.0)
    assert health_mod.expected_runtime_seconds(
        {"runtimeLengthMs": 21600000}) == pytest.approx(21600.0)
    assert health_mod.expected_runtime_seconds(
        {"runtime_seconds": 123.5}) == pytest.approx(123.5)
    assert health_mod.expected_runtime_seconds({}) is None
    chs = health_mod.expected_chapters(
        {"chapters": [{"title": "Intro", "startOffsetMs": 0, "lengthMs": 30000},
                      {"title": "One", "startOffsetMs": 30000, "lengthMs": 60000}]})
    assert chs[1]["start_sec"] == pytest.approx(30.0)
    assert chs[1]["length_sec"] == pytest.approx(60.0)


# --- Flask: approve / reject -------------------------------------------------

def _held_job(client, tmp_path, name="Held Book", nfiles=2):
    """Fabricate a held job: review file + health report + originals
    still in their ORIGINAL SOURCE folder, exactly as _run_build
    leaves them now — nothing moves to Processed while a file is
    held in Review."""
    c, _ = client
    review_base = tmp_path / "inbox" / "Review"
    review_path = review_base / "Auth" / name / f"{name}.m4b"
    review_path.parent.mkdir(parents=True)
    _tone_m4b(str(review_path), seconds=4)
    (review_path.parent / "cover.jpg").write_bytes(b"fake cover")
    source = tmp_path / "inbox" / name
    source.mkdir(parents=True, exist_ok=True)
    for i in range(1, nfiles + 1):
        _tone(str(source / f"{name} {i:02d}.mp3"))
    job = {"id": "heldjob01", "status": "needs_health_review",
           "stage": "needs_health_review", "progress_pct": 100,
           "created": "2026-11-24T00:00:00Z",
           "held_at": "2026-11-24T00:00:00Z",
           "folder": str(tmp_path / "inbox" / name),
           "files": [{"path": str(source / f"{name} {i:02d}.mp3"),
                      "name": f"{name} {i:02d}.mp3", "duration": 2.0,
                      "guessed_position": i,
                      "outcome": "original kept in source — held in Review"}
                     for i in range(1, nfiles + 1)],
           "metadata": {"title": name, "author": "Auth",
                        "runtime_seconds": 360.0},
           "output_path": str(tmp_path / "out" / "Auth" / name / f"{name}.m4b"),
           "review_path": str(review_path),
           "review_cover_path": str(review_path.parent / "cover.jpg"),
           "health": {"verdict": "fail", "limited": False,
                      "expected_total": "6h 00m", "actual_total": "4s",
                      "missing": "5h 59m",
                      "diagnoses": ["Expected 6h 00m, got 4s — 5h 59m missing"]}}
    jobs_dir = tmp_path / "data" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "heldjob01.json").write_text(json.dumps(job))
    report = {"verdict": "fail", "limited": False,
              "diagnoses": ["Expected 6h 00m, got 4s — 5h 59m missing"],
              "problems": ["Expected 6h 00m, got 4s — 5h 59m missing"],
              "warnings": [], "checks": [], "chapters": [],
              "expected_total_sec": 21600.0, "actual_total_sec": 4.0,
              "input_total_sec": 4.0, "missing_sec": 21596.0,
              "expected_chapters": 0, "file_count": 2,
              "output_path": str(review_path)}
    (jobs_dir / "heldjob01-health.json").write_text(json.dumps(report))
    return job, review_path, source


def test_review_lists_held_job_with_report(client, tmp_path):
    c, _ = client
    _held_job(client, tmp_path)
    body = c.get("/api/review").get_json()
    assert body["ok"] and body["count"] == 1, body
    item = body["review"][0]
    assert item["status"] == "needs_health_review"
    assert item["health_report"]["verdict"] == "fail"
    assert "5h 59m" in " ".join(item["health_report"]["diagnoses"])
    health = c.get("/api/jobs/heldjob01/health").get_json()
    assert health["ok"] and health["health"]["verdict"] == "fail"


def test_approve_moves_held_file_to_plex_and_records_override(client, tmp_path):
    c, _ = client
    job, review_path, source = _held_job(client, tmp_path)
    resp = c.post("/api/jobs/heldjob01/approve",
                  json={"note": "Listened to the end — it is all there."})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] and body["job"]["status"] == "done", body
    assert body["job"]["approved_override"]["verdict_overridden"] == "fail"
    assert "Listened" in body["job"]["approved_override"]["note"]
    dest = tmp_path / "out" / "Auth" / "Held Book" / "Held Book.m4b"
    assert dest.exists(), "approved file must land in Plex"
    assert (dest.parent / "cover.jpg").exists()
    assert not review_path.exists(), "Review copy must be gone after approve"
    # Success path (2): only now, with the file in Plex, do the
    # originals move to Processed.
    processed = tmp_path / "inbox" / "Processed" / "Auth" / "Held Book"
    assert processed.is_dir(), "originals move to Processed on approve"
    assert (processed / "Held Book 01.mp3").exists()
    assert (processed / "Held Book 02.mp3").exists()
    assert not (source / "Held Book 01.mp3").exists(), \
        "source folder is empty once the originals have moved"
    assert not body["job"].get("originals_warning"), body["job"]
    assert c.get("/api/review").get_json()["count"] == 0


def test_reject_deletes_held_file_and_keeps_source(client, tmp_path):
    c, _ = client
    job, review_path, source = _held_job(client, tmp_path)
    resp = c.post("/api/jobs/heldjob01/reject", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] and body["job"]["status"] == "rejected", body
    assert not review_path.exists(), "held file must be deleted"
    assert not (tmp_path / "out" / "Auth" / "Held Book" / "Held Book.m4b").exists()
    # Reject: originals were never moved — they stay in source.
    assert (source / "Held Book 01.mp3").exists(), \
        "originals stay in the source folder after a reject"
    assert (source / "Held Book 02.mp3").exists()
    assert not (tmp_path / "inbox" / "Processed" / "Auth" / "Held Book").exists(), \
        "nothing was ever moved to Processed for a held book"
    assert c.get("/api/review").get_json()["count"] == 0


def test_rerun_updates_report_without_placing(client, tmp_path):
    c, _ = client
    job, review_path, source = _held_job(client, tmp_path)
    resp = c.post("/api/jobs/heldjob01/health/rerun", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] and body["health"]["verdict"] == "fail"
    assert body["job"]["status"] == "needs_health_review", \
        "a re-run must never auto-place the file"
    assert not (tmp_path / "out" / "Auth" / "Held Book" / "Held Book.m4b").exists()


def test_held_job_cannot_be_cleared_from_queue(client, tmp_path):
    c, _ = client
    _held_job(client, tmp_path)
    resp = c.delete("/api/queue/heldjob01")
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert c.get("/api/jobs/heldjob01").get_json()["job"]["status"] == \
        "needs_health_review"


def test_review_dir_inside_output_is_refused(client, tmp_path):
    c, _ = client
    resp = c.post("/api/settings", json={
        "inbox_dir": str(tmp_path / "inbox"),
        "output_dir": str(tmp_path / "out"),
        "review_dir": str(tmp_path / "out" / "Review"),
        "timezone": "UTC"})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "outside" in resp.get_data(as_text=True).lower()


# --- end-to-end via the app ---------------------------------------------------

def _wait_for(client, job_id, wanted, timeout=60):
    c, _ = client
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = c.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in wanted:
            return job
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} never reached {wanted}; last: {job}")


def _build_book(client, tmp_path, name, runtime_seconds, nfiles=2):
    c, _ = client
    app_mod.QUEUE_RUNNER_ENABLED = True
    folder = tmp_path / "inbox" / name
    folder.mkdir(parents=True)
    for i in range(1, nfiles + 1):
        _tone(str(folder / f"{name} {i:02d}.mp3"))
    resp = c.post("/api/jobs", json={"folder": name})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    job = resp.get_json()["job"]
    assert resp.get_json()["route"] == "review"
    resp = c.post(f"/api/jobs/{job['id']}/confirm",
                  json={"files": job["files"],
                        "metadata": {"title": name, "author": "Auth",
                                     "runtime_seconds": runtime_seconds}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return job["id"]


def test_mismatched_build_is_held_then_approved_to_plex(client, tmp_path):
    c, _ = client
    job_id = _build_book(client, tmp_path, "Short Book", 360.0)
    final = _wait_for(client, job_id,
                      ("needs_health_review", "done", "failed"))
    assert final["status"] == "needs_health_review", final
    assert final["health"]["verdict"] == "fail", final["health"]
    assert "missing" in " ".join(final["health"]["diagnoses"]).lower()
    review_dir = tmp_path / "inbox" / "Review"
    held = review_dir / "Auth" / "Short Book" / "Short Book.m4b"
    assert held.exists(), "held file must be in Review, not Plex"
    assert not (tmp_path / "out" / "Auth" / "Short Book" / "Short Book.m4b").exists()
    # State (2): held in Review — originals STAY in source, nothing
    # in Processed.
    source = tmp_path / "inbox" / "Short Book"
    assert (source / "Short Book 01.mp3").exists(), \
        "held book: originals stay in the source folder"
    assert (source / "Short Book 02.mp3").exists()
    assert not (tmp_path / "inbox" / "Processed" / "Auth" / "Short Book").exists(), \
        "held book: nothing may move to Processed while it is held"
    assert c.get("/api/cleanup").get_json()["safe"] == [], \
        "a held book must not appear in Cleanup (it is not in Processed)"
    assert c.get("/api/review").get_json()["count"] == 1
    resp = c.post(f"/api/jobs/{job_id}/approve", json={"note": "checked, fine"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["job"]["status"] == "done"
    assert (tmp_path / "out" / "Auth" / "Short Book" / "Short Book.m4b").exists()
    assert not held.exists()
    # State (3): approved into Plex — ONLY now do the originals move.
    processed = tmp_path / "inbox" / "Processed" / "Auth" / "Short Book"
    assert processed.is_dir(), "originals move to Processed on approve"
    assert (processed / "Short Book 01.mp3").exists()
    assert not (source / "Short Book 01.mp3").exists()
    assert c.get("/api/review").get_json()["count"] == 0


def test_held_book_keeps_originals_in_source(client, tmp_path):
    """State (2) on its own: a book whose .m4b builds fine but FAILS
    the health check is held in Review with its source files
    verified still in the source folder and nothing in Processed."""
    c, _ = client
    job_id = _build_book(client, tmp_path, "Held Source Book", 360.0)
    final = _wait_for(client, job_id,
                      ("needs_health_review", "done", "failed"))
    assert final["status"] == "needs_health_review", final
    source = tmp_path / "inbox" / "Held Source Book"
    assert (source / "Held Source Book 01.mp3").exists()
    assert (source / "Held Source Book 02.mp3").exists()
    assert not (tmp_path / "inbox" / "Processed").exists(), \
        "no Processed folder may be created for a held book"
    assert (tmp_path / "inbox" / "Review" / "Auth" / "Held Source Book"
            / "Held Source Book.m4b").exists()


def test_failed_build_keeps_originals_in_source(client, tmp_path):
    """State (a): a build that fails (corrupt input file) leaves
    every source file exactly where it was — nothing in Processed,
    nothing in Plex."""
    c, _ = client
    app_mod.QUEUE_RUNNER_ENABLED = True
    folder = tmp_path / "inbox" / "Broken Book"
    folder.mkdir(parents=True)
    _tone(str(folder / "Broken Book 01.mp3"))
    (folder / "Broken Book 02.mp3").write_bytes(b"not audio at all" * 100)
    resp = c.post("/api/jobs", json={"folder": "Broken Book"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    job = resp.get_json()["job"]
    resp = c.post(f"/api/jobs/{job['id']}/confirm",
                  json={"files": job["files"],
                        "metadata": {"title": "Broken Book", "author": "Auth",
                                     "runtime_seconds": 4.0}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    final = _wait_for(client, job["id"],
                      ("needs_health_review", "done", "failed"))
    assert final["status"] == "failed", final
    assert (folder / "Broken Book 01.mp3").exists()
    assert (folder / "Broken Book 02.mp3").exists()
    assert not (tmp_path / "inbox" / "Processed").exists(), \
        "a failed build must not move anything to Processed"
    assert not (tmp_path / "out" / "Auth" / "Broken Book" / "Broken Book.m4b").exists()


def test_partial_move_is_done_with_warning(client, tmp_path):
    """Partial-move safety, at the helper level: the first file
    moves, the second cannot (source gone) — the helper logs the
    exact file, keeps going, and returns a warning instead of
    raising. The caller marks the job Done-with-warning, not Failed.
    (Uses the client fixture so SETTINGS points at this test's
    inbox — without it there is no Processed folder configured.)"""
    c, _ = client
    folder = tmp_path / "inbox" / "Part Book"
    folder.mkdir(parents=True)
    _tone(str(folder / "Part Book 01.mp3"))
    _tone(str(folder / "Part Book 02.mp3"))
    files = [{"path": str(folder / "Part Book 01.mp3"),
              "name": "Part Book 01.mp3", "duration": 2.0},
             {"path": str(folder / "Part Book 02.mp3"),
              "name": "Part Book 02.mp3", "duration": 2.0}]
    # Second source vanishes before the move (disk error, user
    # moved it, etc.) — the move must fail for that file only.
    os.remove(str(folder / "Part Book 02.mp3"))
    logged = []
    warning, moved, failed = app_mod._move_originals_to_processed(
        "testjob", files, {"title": "Part Book", "author": "Auth"},
        str(folder),
        lambda stage, level, msg, **kw: logged.append((level, msg)))
    assert warning, "a partial move must produce a warning"
    assert "Part Book 02.mp3" in warning, warning
    assert str(folder) in warning, "warning must say where the unmoved file is"
    assert moved == ["Part Book 01.mp3"]
    assert failed == ["Part Book 02.mp3"]
    assert (tmp_path / "inbox" / "Processed" / "Auth" / "Part Book"
            / "Part Book 01.mp3").exists()
    assert files[0].get("processed_path"), "moved file is recorded"
    assert "processed_path" not in files[1], \
        "unmoved file keeps its original path, not a Processed one"
    assert any(level == "ERROR" and "Part Book 02.mp3" in msg
               for level, msg in logged), logged


def test_matching_build_goes_straight_to_plex(client, tmp_path):
    c, _ = client
    job_id = _build_book(client, tmp_path, "Good Book", 4.0)
    final = _wait_for(client, job_id,
                      ("needs_health_review", "done", "failed"))
    assert final["status"] == "done", final
    assert final["health"]["verdict"] in ("pass", "limited"), final["health"]
    assert (tmp_path / "out" / "Auth" / "Good Book" / "Good Book.m4b").exists()
    # State (1): health passed, file in Plex — originals now in Processed.
    processed = tmp_path / "inbox" / "Processed" / "Auth" / "Good Book"
    assert processed.is_dir(), "originals move to Processed on a passing build"
    assert (processed / "Good Book 01.mp3").exists()
    assert (processed / "Good Book 02.mp3").exists()
    assert not (tmp_path / "inbox" / "Good Book" / "Good Book 01.mp3").exists()
    assert not final.get("originals_warning"), final
    assert c.get("/api/review").get_json()["count"] == 0
