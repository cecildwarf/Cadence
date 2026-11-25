"""Queue tests — per-item % progress, cancel/clear, bulk queueing,
single-file auto-queue and folder-select routing.

Routing rule under test:
  1 file  -> auto-queued (status 'queued', no review)
  >1 file -> review flow (status 'created', needs_review)
  0 files -> 400, no job created at all
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
import cadence_queue as queue_mod


def _tone_mp3(path: str, seconds: int = 2, freq: int = 440) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "128k", path],
                   check=True, timeout=120)


@pytest.fixture(autouse=True)
def _freeze_runner():
    """Freeze the queue runner unless a test opts back in, so
    'queued' assertions are deterministic. Restores afterwards."""
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
        "temp_dir": str(data_dir / "temp"), "timezone": "UTC",
    })
    flog.init_logging(str(data_dir))
    flog.set_timezone("UTC")
    yield app_mod.app.test_client(), tmp_path
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update(saved_settings)


def _book(client, tmp_path, name, nfiles, seconds=2):
    folder = tmp_path / "inbox" / name
    folder.mkdir(parents=True)
    for i in range(1, nfiles + 1):
        # Distinct file titles per book: two books must never guess
        # the same title, or they'd collide on one Plex output path.
        _tone_mp3(str(folder / f"{name} {i:02d}.mp3"), seconds=seconds)
    return folder


# --- routing (requirement 4 + 5) -------------------------------------------

def test_single_file_folder_auto_queues(client):
    c, tmp_path = client
    _book(c, tmp_path, "Solo Book", 1)
    resp = c.post("/api/jobs", json={"folder": "Solo Book"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["route"] == "auto_queue"
    job = body["job"]
    assert job["status"] == "queued"
    assert job["auto_queued"] is True
    assert job["progress_pct"] == 0 and job["stage"] == "queued"
    assert job["queue_position"] == 1
    assert len(job["files"]) == 1


def test_multi_file_folder_goes_to_review(client):
    c, tmp_path = client
    _book(c, tmp_path, "Big Book", 3)
    resp = c.post("/api/jobs", json={"folder": "Big Book"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["route"] == "review"
    job = body["job"]
    assert job["status"] == "created"
    assert job["needs_review"] is True
    assert job["auto_queued"] is False


def test_empty_folder_creates_no_job(client):
    c, tmp_path = client
    (tmp_path / "inbox" / "Empty Book").mkdir(parents=True)
    resp = c.post("/api/jobs", json={"folder": "Empty Book"})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "No audio files" in resp.get_json()["error"]
    jobs_dir = tmp_path / "data" / "jobs"
    assert not jobs_dir.exists() or not list(jobs_dir.glob("*.json"))


def test_single_file_pick_auto_queues(client):
    c, tmp_path = client
    _tone_mp3(str(tmp_path / "inbox" / "Lone File.mp3"))
    resp = c.post("/api/jobs", json={"file": "Lone File.mp3"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["route"] == "auto_queue"
    assert resp.get_json()["job"]["status"] == "queued"


def test_routing_helpers():
    assert queue_mod.route_for_count(0) == "empty"
    assert queue_mod.route_for_count(1) == "auto_queue"
    assert queue_mod.route_for_count(2) == "review"
    assert queue_mod.route_for_count(9) == "review"
    guess = queue_mod.guess_metadata_from_name(
        "Wild Magic_B002V0TEQ8_LC_64_22050_Stereo.m4b")
    assert guess["asin"] == "B002V0TEQ8" and guess["title"] == "Wild Magic"
    assert queue_mod.progress_for_stage("converting", 0.5) == 60
    assert queue_mod.progress_for_stage("scanning", 1.0) == 10
    assert queue_mod.progress_for_stage("done") == 100


# --- bulk queueing (requirement 3) -----------------------------------------

def test_bulk_queue_one_entry_per_book(client):
    c, tmp_path = client
    _book(c, tmp_path, "Solo One", 1)
    _book(c, tmp_path, "Big One", 3)
    _book(c, tmp_path, "Solo Two", 1)
    resp = c.post("/api/queue", json={"items": [
        {"folder": "Solo One"}, {"folder": "Big One"}, {"folder": "Solo Two"}]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert len(body["results"]) == 3
    assert body["queued"] == 2 and body["needs_review"] == 1
    routes = [r["route"] for r in body["results"]]
    assert routes == ["auto_queue", "review", "auto_queue"]
    q = c.get("/api/queue").get_json()
    assert q["counts"]["queued"] == 2
    assert q["counts"]["needs_review"] == 1
    positions = [j["queue_position"] for j in q["queue"]]
    assert positions == [1, 2]


def test_bulk_queue_skips_empty_folder_but_queues_the_rest(client):
    c, tmp_path = client
    _book(c, tmp_path, "Solo", 1)
    (tmp_path / "inbox" / "Empty").mkdir()
    resp = c.post("/api/queue", json={"items": [{"folder": "Solo"}, {"folder": "Empty"}]})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["queued"] == 1
    assert body["results"][1]["route"] == "empty"


# --- cancel / clear (requirement 2) ----------------------------------------

def test_clear_queued_item_removes_it_and_keeps_sources(client):
    c, tmp_path = client
    src = _book(c, tmp_path, "Solo Book", 1)
    job_id = c.post("/api/jobs", json={"folder": "Solo Book"}).get_json()["job"]["id"]
    resp = c.delete(f"/api/queue/{job_id}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert c.get(f"/api/jobs/{job_id}").status_code == 404
    assert (src / "Solo Book 01.mp3").exists(), "clearing must not delete sources"
    assert not list((tmp_path / "out").rglob("*.m4b"))


def test_cancel_queued_item_marks_cancelled_not_failed(client):
    c, tmp_path = client
    src = _book(c, tmp_path, "Solo Book", 1)
    job_id = c.post("/api/jobs", json={"folder": "Solo Book"}).get_json()["job"]["id"]
    resp = c.post(f"/api/queue/{job_id}/cancel")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    job = resp.get_json()["job"]
    assert job["status"] == "cancelled"
    assert job["stage"] == "cancelled"
    assert job["cancelled_reason"]
    assert (src / "Solo Book 01.mp3").exists()
    assert not list((tmp_path / "out").rglob("*.m4b"))


def test_clear_finished_bulk_keeps_plex_output(client):
    c, tmp_path = client
    app_mod.QUEUE_RUNNER_ENABLED = True
    _book(c, tmp_path, "Solo Book", 1)
    job_id = c.post("/api/jobs", json={"folder": "Solo Book"}).get_json()["job"]["id"]
    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        final = c.get(f"/api/jobs/{job_id}").get_json()["job"]
        if final["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert final["status"] == "done", final
    out_files = list((tmp_path / "out").rglob("*.m4b"))
    assert len(out_files) == 1
    resp = c.post("/api/queue/clear", json={"which": "finished"})
    assert resp.status_code == 200
    assert resp.get_json()["removed"] == 1
    assert c.get(f"/api/jobs/{job_id}").status_code == 404
    assert out_files[0].exists(), "clearing a finished job must not delete Plex output"


# --- real progress + sequential builds (requirements 1 + 3) -----------------

def test_build_progress_reaches_100_and_completes_in_order(client):
    c, tmp_path = client
    app_mod.QUEUE_RUNNER_ENABLED = True
    _book(c, tmp_path, "First Book", 1)
    _book(c, tmp_path, "Second Book", 1)
    body = c.post("/api/queue", json={"items": [
        {"folder": "First Book"}, {"folder": "Second Book"}]}).get_json()
    ids = [r["job_id"] for r in body["results"]]
    deadline = time.time() + 60
    finals = {}
    while time.time() < deadline:
        for job_id in ids:
            job = c.get(f"/api/jobs/{job_id}").get_json()["job"]
            finals[job_id] = job
        if all(finals[i]["status"] in ("done", "failed") for i in ids):
            break
        time.sleep(0.5)
    assert all(finals[i]["status"] == "done" for i in ids), finals
    for job_id in ids:
        assert finals[job_id]["progress_pct"] == 100, finals[job_id]
        assert finals[job_id]["stage"] == "done"
        assert finals[job_id]["output_path"]
    # Sequential: the first book finished before (or as) the second did.
    assert finals[ids[0]]["finished_at"] <= finals[ids[1]]["finished_at"]
    assert len(list((tmp_path / "out").rglob("*.m4b"))) == 2


def test_build_m4b_progress_callback_is_real_and_increasing(tmp_path):
    folder = tmp_path / "src"
    folder.mkdir()
    files = []
    for i in range(1, 4):
        p = folder / f"{i:02d}.mp3"
        _tone_mp3(str(p))
        info = build_mod.probe(str(p))
        files.append({"path": str(p), "duration": info["duration"],
                      "codec": info["codec"], "sample_rate": info["sample_rate"],
                      "chapter_title": f"Chapter {i}"})
    seen = []
    out = str(tmp_path / "out.m4b")
    result = build_mod.build_m4b(
        files, {"title": "T", "author": "A"}, out, str(tmp_path / "temp"),
        progress_callback=lambda frac: seen.append(frac))
    assert seen, "progress callback was never called"
    assert seen[0] == 0.0 and seen[-1] == 1.0
    assert seen == sorted(seen), "progress must never go backwards"
    assert 0.0 < max(seen) <= 1.0
    assert abs(result["duration"] - 6.0) < 0.6


def test_cancel_running_build_marks_cancelled_and_no_output(client):
    c, tmp_path = client
    app_mod.QUEUE_RUNNER_ENABLED = True
    # A long encode gives the cancel a window to land in.
    _book(c, tmp_path, "Long Book", 1, seconds=300)
    job_id = c.post("/api/jobs", json={"folder": "Long Book"}).get_json()["job"]["id"]
    cancelled = None
    deadline = time.time() + 30
    while time.time() < deadline:
        job = c.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] == "building":
            c.post(f"/api/queue/{job_id}/cancel")
            break
        if job["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    deadline = time.time() + 30
    while time.time() < deadline:
        cancelled = c.get(f"/api/jobs/{job_id}").get_json()["job"]
        if cancelled["status"] in ("cancelled", "done", "failed"):
            break
        time.sleep(0.2)
    assert cancelled["status"] == "cancelled", cancelled
    assert not list((tmp_path / "out").rglob("*.m4b")), \
        "a cancelled build must leave no output file"
