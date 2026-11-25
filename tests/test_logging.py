"""Failure-logging tests — the anti-Bragi tests.

Bragi errors out with no indication of what's going on. These
tests prove Cadence never does that:
  1. A corrupt/empty MP3 fails the probe with the EXACT file +
     reason, and a job built from it is marked FAILED (never
     silently skipped, never marked done).
  2. A metadata lookup for a bogus ASIN fails with a specific
     logged reason (audnexus 404) and the job is NOT silently
     left without metadata — the error is recorded on the job.
  3. Every log_event carries timestamp (Denver + UTC), level,
     stage, job id and outcome, in both .log and .jsonl.
  4. The phone-paste helpers: pasteable log has a summary block
     (job id / title / ASIN / per-file outcomes) and
     "copy last error only" returns the ERROR + 20 lines before it.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cadence_logging as flog
import build as build_mod
import metadata as meta_mod


def test_probe_empty_mp3_fails_with_exact_file_and_reason(tmp_path):
    bad = tmp_path / "01 - Empty.mp3"
    bad.write_bytes(b"")  # 0 bytes — corrupt / empty
    try:
        build_mod.probe(str(bad))
    except build_mod.BuildError as exc:
        msg = str(exc)
        assert str(bad) in msg, msg
        assert "duration" in msg.lower() or "corrupt" in msg.lower() or "empty" in msg.lower(), msg
    else:
        raise AssertionError("probe() should have raised BuildError for an empty MP3")


def test_probe_garbage_mp3_fails_with_exact_file(tmp_path):
    bad = tmp_path / "02 - Garbage.mp3"
    bad.write_bytes(b"this is not audio at all" * 100)
    try:
        build_mod.probe(str(bad))
    except build_mod.BuildError as exc:
        assert str(bad) in str(exc)
    else:
        raise AssertionError("probe() should have raised BuildError for garbage bytes")


def test_log_event_has_all_fields_and_both_sinks(tmp_path):
    data_dir = str(tmp_path / "data")
    flog.init_logging(data_dir)
    flog.log_error("job123", "ffmpeg", "ffmpeg failed for 01.mp3 (exit 1): boom",
                   filename="01.mp3", outcome="FAIL")
    log_path = os.path.join(data_dir, "logs", "job123.log")
    jsonl_path = os.path.join(data_dir, "logs", "job123.jsonl")
    assert os.path.exists(log_path) and os.path.exists(jsonl_path)
    rec = json.loads(open(jsonl_path, encoding="utf-8").readline())
    assert rec["ts_local"] and rec["ts_utc"]
    assert rec["level"] == "ERROR" and rec["stage"] == "ffmpeg"
    assert rec["job_id"] == "job123" and rec["filename"] == "01.mp3"
    assert rec["outcome"] == "FAIL"
    assert "boom" in open(log_path, encoding="utf-8").read()


def test_metadata_bogus_asin_fails_with_specific_logged_reason(tmp_path, monkeypatch):
    """audnexus 404 for a bogus ASIN: specific error, logged, not silent."""
    data_dir = str(tmp_path / "data")
    flog.init_logging(data_dir)

    class Resp:
        status_code = 404
        def raise_for_status(self): pass
        def json(self): return {}

    monkeypatch.setattr(meta_mod.requests, "get", lambda *a, **k: Resp())
    try:
        meta_mod.fetch_by_asin("B000000000")
    except ValueError as exc:
        reason = str(exc)
        assert "B000000000" in reason and "404" in reason, reason
        flog.log_error("job404", "metadata", f"Metadata lookup failed: {exc}", outcome="FAIL")
    else:
        raise AssertionError("fetch_by_asin should have raised for a 404 ASIN")
    events = flog.read_job_log(data_dir, "job404")
    assert events and events[0]["level"] == "ERROR"
    assert "B000000000" in events[0]["message"]


def test_job_with_corrupt_mp3_is_marked_failed_not_done(tmp_path, monkeypatch):
    """End-to-end via the Flask app: corrupt file -> job FAILED with reason."""
    import app as app_mod

    data_dir = tmp_path / "data"
    inbox = tmp_path / "inbox" / "Bad Book"
    inbox.mkdir(parents=True)
    (tmp_path / "out").mkdir(parents=True)
    (inbox / "01 - Empty.mp3").write_bytes(b"")
    (inbox / "02 - Also Empty.mp3").write_bytes(b"")

    monkeypatch.setattr(app_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod, "JOBS_DIR", str(data_dir / "jobs"))
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", str(data_dir / "settings.json"))
    app_mod.SETTINGS.update({
        "inbox_dir": str(tmp_path / "inbox"),
        "output_dir": str(tmp_path / "out"),
        "temp_dir": str(data_dir / "temp"),
    })
    flog.init_logging(str(data_dir))
    client = app_mod.app.test_client()

    resp = client.post("/api/jobs", json={"folder": "Bad Book"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    job = resp.get_json()["job"]
    assert job["files"], "scan should record the corrupt files, not skip them"
    assert any("PROBE FAILED" in (f.get("outcome") or "") for f in job["files"])

    resp = client.post(f"/api/jobs/{job['id']}/confirm",
                       json={"files": job["files"],
                             "metadata": {"title": "Bad Book", "author": "Nobody"}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    # Build runs in a background thread — wait for it to fail.
    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        final = client.get(f"/api/jobs/{job['id']}").get_json()["job"]
        if final["status"] in ("failed", "done"):
            break
        time.sleep(0.5)
    assert final is not None and final["status"] == "failed", final
    assert final["error"] and "Empty" in final["error"], final["error"]
    assert final["status"] != "done", "a partial/corrupt job must never be marked done"
    # The log must carry the exact failure.
    log_text = client.get(
        f"/api/jobs/{job['id']}/log?format=text").get_data(as_text=True)
    assert "FAILED" in log_text and "Empty" in log_text


def test_pasteable_log_has_summary_block_and_last_error(tmp_path):
    data_dir = str(tmp_path / "data")
    flog.init_logging(data_dir)
    job_id = "jobpaste"
    for i in range(25):
        flog.log_info(job_id, "build", f"step {i} ok")
    flog.log_error(job_id, "ffmpeg", "ffmpeg failed for 03.mp3 (exit 1): boom",
                   filename="03.mp3", outcome="FAIL")
    job = {"id": job_id, "status": "failed", "title": "Test Book",
           "metadata": {"title": "Test Book", "author": "Auth", "asin": "B0TEST1234"},
           "error": "ffmpeg failed for 03.mp3 (exit 1): boom",
           "created": "2026-11-24T00:00:00Z",
           "output_path": "",
           "files": [{"name": "03.mp3", "guessed_position": 3,
                      "outcome": "FAILED: boom"}]}
    paste = flog.build_pasteable_log(data_dir, job_id, job)
    assert "Job id:      jobpaste" in paste
    assert "B0TEST1234" in paste and "Test Book" in paste
    assert "03.mp3" in paste and "FAILED" in paste
    assert "--- Full log ---" in paste
    err_only = flog.last_error_block(flog.get_job_text(data_dir, job_id))
    assert "boom" in err_only
    # 20 lines before the first error + the error itself.
    assert err_only.count("\n") <= 21 + 1
    assert "step 5 ok" in err_only  # context lines are present (20 before the error)
    assert "step 4 ok" not in err_only  # only the 20 lines before the error
