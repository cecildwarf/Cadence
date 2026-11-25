"""Processed-folder + safe-cleanup tests.

Matcher: safe only when the completed .m4b exists, validates, and
its duration matches within 2% AND the job record is Done.
Delete guard: refuses anything outside the Processed folder.
End-to-end: a real build moves originals to Processed/<Author>/<Book>/,
cleanup lists the book safe, and deleting it frees the space while
the finished .m4b stays in Plex.
"""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod
import cleanup as cleanup_mod
import cadence_logging as flog


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
        "processed_dir": "",
        "temp_dir": str(data_dir / "temp"), "timezone": "UTC",
    })
    flog.init_logging(str(data_dir))
    flog.set_timezone("UTC")
    yield app_mod.app.test_client(), tmp_path
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update(saved_settings)


def _processed_book(tmp_path, author="Auth", title="Book", nfiles=2, seconds=2):
    folder = tmp_path / "Processed" / author / title
    folder.mkdir(parents=True)
    for i in range(1, nfiles + 1):
        _tone(str(folder / f"{title} {i:02d}.mp3"), seconds=seconds)
    return folder


def _done_job(tmp_path, author="Auth", title="Book", nfiles=2,
              out_seconds=4, out_path=None):
    out = out_path or str(tmp_path / "out" / author / title / f"{title}.m4b")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    _tone_m4b(out, seconds=out_seconds)
    return {"id": "job123", "status": "done",
            "metadata": {"author": author, "title": title},
            "folder": str(tmp_path / "inbox" / title),
            "files": [{"path": f"/x/{title} {i:02d}.mp3"} for i in range(1, nfiles + 1)],
            "output_path": out, "output_duration": float(out_seconds),
            "finished_at": "2026-11-24T00:00:00Z",
            "created": "2026-11-24T00:00:00Z"}


# --- matcher (pure logic) ---------------------------------------------------

def test_matcher_safe_when_output_exists_and_duration_matches(tmp_path):
    proc = _processed_book(tmp_path)
    job = _done_job(tmp_path)
    scan = cleanup_mod.scan_cleanup(str(tmp_path / "Processed"),
                                    str(tmp_path / "out"), [job])
    assert len(scan["safe"]) == 1, scan
    assert scan["needs_review"] == []
    item = scan["safe"][0]
    assert item["title"] == "Book" and item["author"] == "Auth"
    assert item["diff_pct"] is not None and item["diff_pct"] <= 2.0
    assert scan["total_reclaimable_bytes"] > 0


def test_matcher_not_safe_when_output_missing(tmp_path):
    _processed_book(tmp_path)
    job = _done_job(tmp_path)
    os.remove(job["output_path"])
    scan = cleanup_mod.scan_cleanup(str(tmp_path / "Processed"),
                                    str(tmp_path / "out"), [job])
    assert scan["safe"] == []
    assert len(scan["needs_review"]) == 1
    assert "missing" in scan["needs_review"][0]["reasons"][0].lower()


def test_matcher_not_safe_when_duration_mismatched(tmp_path):
    _processed_book(tmp_path)
    # The file in the output tree is 10s, but the job recorded 4s:
    # the completed file does not match what was actually built.
    job = _done_job(tmp_path, out_seconds=10)
    job["output_duration"] = 4.0
    scan = cleanup_mod.scan_cleanup(str(tmp_path / "Processed"),
                                    str(tmp_path / "out"), [job])
    assert scan["safe"] == []
    assert any("mismatch" in r.lower()
               for r in scan["needs_review"][0]["reasons"])


def test_matcher_not_safe_when_job_failed(tmp_path):
    _processed_book(tmp_path)
    job = _done_job(tmp_path)
    job["status"] = "failed"
    scan = cleanup_mod.scan_cleanup(str(tmp_path / "Processed"),
                                    str(tmp_path / "out"), [job])
    assert scan["safe"] == []
    assert scan["needs_review"], "a failed job must never be safe"
    assert any("job record" in r.lower()
               for r in scan["needs_review"][0]["reasons"])


def test_matcher_not_safe_when_output_corrupt(tmp_path):
    _processed_book(tmp_path)
    job = _done_job(tmp_path)
    with open(job["output_path"], "wb") as fh:
        fh.write(b"not audio at all" * 100)
    scan = cleanup_mod.scan_cleanup(str(tmp_path / "Processed"),
                                    str(tmp_path / "out"), [job])
    assert scan["safe"] == []
    assert scan["needs_review"]


def test_matcher_not_safe_when_match_ambiguous(tmp_path):
    _processed_book(tmp_path)
    job1 = _done_job(tmp_path)
    job2 = _done_job(tmp_path)
    job2["id"] = "job456"
    scan = cleanup_mod.scan_cleanup(str(tmp_path / "Processed"),
                                    str(tmp_path / "out"), [job1, job2])
    assert scan["safe"] == []
    assert any("ambiguous" in r.lower()
               for r in scan["needs_review"][0]["reasons"])


def test_safe_list_sorted_biggest_first(tmp_path):
    small = _processed_book(tmp_path, title="Small Book", seconds=2)
    big = _processed_book(tmp_path, title="Big Book", seconds=10)
    job_small = _done_job(tmp_path, title="Small Book", out_seconds=4)
    job_big = _done_job(tmp_path, title="Big Book", nfiles=2, out_seconds=20)
    scan = cleanup_mod.scan_cleanup(str(tmp_path / "Processed"),
                                    str(tmp_path / "out"), [job_small, job_big])
    assert [i["title"] for i in scan["safe"]] == ["Big Book", "Small Book"]


# --- delete guard -----------------------------------------------------------

def test_delete_guard_refuses_path_outside_processed(tmp_path):
    processed = tmp_path / "Processed"
    processed.mkdir()
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "keep.mp3").write_bytes(b"x")
    result = cleanup_mod.delete_processed_items(
        str(processed),
        [{"processed_path": str(evil), "title": "Evil"}])
    assert result["failed"] is not None
    assert "not inside" in result["failed"]["reason"]
    assert result["deleted"] == []
    assert (evil / "keep.mp3").exists(), "nothing outside Processed was touched"
    # The Processed folder itself is never a delete target either.
    result2 = cleanup_mod.delete_processed_items(
        str(processed),
        [{"processed_path": str(processed), "title": "All of it"}])
    assert result2["failed"] is not None
    assert processed.exists()


def test_delete_removes_only_the_book_folder(tmp_path):
    proc = _processed_book(tmp_path)
    other = _processed_book(tmp_path, title="Other Book")
    result = cleanup_mod.delete_processed_items(
        str(tmp_path / "Processed"),
        [{"processed_path": str(proc), "title": "Book"}])
    assert result["failed"] is None
    assert len(result["deleted"]) == 1 and result["freed_bytes"] > 0
    assert not proc.exists()
    assert other.exists(), "other books are untouched"


# --- end-to-end via the app -------------------------------------------------

def _build_book(client, tmp_path, name, nfiles=2):
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
                        "metadata": {"title": name, "author": "Auth"}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    deadline = time.time() + 60
    final = None
    while time.time() < deadline:
        final = c.get(f"/api/jobs/{job['id']}").get_json()["job"]
        if final["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.5)
    assert final["status"] == "done", final
    return final


def test_build_moves_originals_to_processed(client):
    c, tmp_path = client
    final = _build_book(client, tmp_path, "Proc Book")
    processed = tmp_path / "inbox" / "Processed" / "Auth" / "Proc Book"
    assert processed.is_dir()
    assert (processed / "Proc Book 01.mp3").exists()
    assert (processed / "Proc Book 02.mp3").exists()
    assert not (tmp_path / "inbox" / "Proc Book" / "Proc Book 01.mp3").exists()
    assert any("Processed" in (f.get("outcome") or "")
               for f in final["files"])
    assert (tmp_path / "out" / "Auth" / "Proc Book" / "Proc Book.m4b").exists()


def test_cleanup_api_lists_safe_then_delete_frees_space(client):
    c, tmp_path = client
    _build_book(client, tmp_path, "Proc Book")
    scan = c.get("/api/cleanup").get_json()
    assert scan["ok"] is True, scan
    assert len(scan["safe"]) == 1, scan
    item = scan["safe"][0]
    assert item["title"] == "Proc Book"
    assert scan["total_reclaimable_bytes"] > 0
    out_file = tmp_path / "out" / "Auth" / "Proc Book" / "Proc Book.m4b"
    resp = c.post("/api/cleanup/delete",
                  json={"processed_paths": [item["processed_path"]]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] and body["freed_bytes"] > 0
    assert not (tmp_path / "inbox" / "Processed" / "Auth" / "Proc Book").exists()
    assert out_file.exists(), "the finished .m4b must survive cleanup"
    assert c.get("/api/cleanup").get_json()["safe"] == []


def test_cleanup_keep_excludes_until_unkept(client):
    c, tmp_path = client
    _build_book(client, tmp_path, "Proc Book")
    scan = c.get("/api/cleanup").get_json()
    path = scan["safe"][0]["processed_path"]
    resp = c.post("/api/cleanup/keep",
                  json={"processed_path": path, "keep": True})
    assert resp.status_code == 200
    assert c.get("/api/cleanup").get_json()["safe"] == []
    kept = c.get("/api/cleanup?show_kept=1").get_json()
    assert len(kept["kept"]) == 1
    assert (tmp_path / "inbox" / "Processed" / "Auth" / "Proc Book").exists()
    c.post("/api/cleanup/keep",
           json={"processed_path": path, "keep": False})
    assert len(c.get("/api/cleanup").get_json()["safe"]) == 1


def test_cleanup_not_safe_when_output_deleted(client):
    c, tmp_path = client
    _build_book(client, tmp_path, "Proc Book")
    (tmp_path / "out" / "Auth" / "Proc Book" / "Proc Book.m4b").unlink()
    scan = c.get("/api/cleanup").get_json()
    assert scan["safe"] == []
    assert len(scan["needs_review"]) == 1
    assert scan["needs_review"][0]["title"] == "Proc Book"


def test_cleanup_delete_refuses_stale_unsafe_item(client):
    c, tmp_path = client
    _build_book(client, tmp_path, "Proc Book")
    scan = c.get("/api/cleanup").get_json()
    path = scan["safe"][0]["processed_path"]
    # The output disappears after the list was shown: delete must
    # re-verify and refuse, deleting nothing.
    (tmp_path / "out" / "Auth" / "Proc Book" / "Proc Book.m4b").unlink()
    resp = c.post("/api/cleanup/delete", json={"processed_paths": [path]})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert (tmp_path / "inbox" / "Processed" / "Auth" / "Proc Book").exists()


def test_processed_dir_setting_is_respected(client):
    c, tmp_path = client
    custom = tmp_path / "my-processed"
    custom.mkdir()
    resp = c.post("/api/settings", json={
        "inbox_dir": str(tmp_path / "inbox"),
        "output_dir": str(tmp_path / "out"),
        "processed_dir": str(custom), "timezone": "UTC"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    _build_book(client, tmp_path, "Proc Book")
    assert (custom / "Auth" / "Proc Book" / "Proc Book 01.mp3").exists()
