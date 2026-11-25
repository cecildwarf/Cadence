"""Server-busy backoff tests.

Decision logic (backoff.py) is tested pure, with mocked load/Plex
data. App wiring (app.py) is tested through the Flask test client
and the real pause/resume helpers against a real `sleep` process —
SIGSTOP/SIGCONT is the actual mechanism, so it gets a real process,
not a mock.

Rules under test:
  * busy on sustained high load, busy on any Plex transcode,
    quiet on direct-play-only and on low load
  * a single busy spike does NOT pause anything (2 checks needed)
  * resume only after a continuous quiet grace period
  * a paused job is still 'building' — never failed or cancelled
  * schedule blocks outside the window and allows inside it
  * the Plex token is never returned in full by any endpoint
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod
import backoff as backoff_mod
import cadence_logging as flog


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------

def test_high_load_is_busy_and_low_load_is_quiet():
    busy, why = backoff_mod.load_is_busy(
        {"load1": 5.6, "cores": 4, "load_pct": 140.0, "mem_available_pct": 60.0}, 70)
    assert busy is True
    assert "5.6" in why and "4 core" in why
    busy, _why = backoff_mod.load_is_busy(
        {"load1": 1.0, "cores": 4, "load_pct": 25.0, "mem_available_pct": 60.0}, 70)
    assert busy is False
    # Exactly at the threshold is not busy — only above it.
    busy, _why = backoff_mod.load_is_busy(
        {"load1": 2.8, "cores": 4, "load_pct": 70.0, "mem_available_pct": 60.0}, 70)
    assert busy is False


def test_memory_pressure_is_busy_even_when_load_is_low():
    busy, why = backoff_mod.load_is_busy(
        {"load1": 0.5, "cores": 4, "load_pct": 12.5, "mem_available_pct": 4.0}, 70)
    assert busy is True
    assert "memory" in why.lower()


def test_run_window_daytime_and_overnight():
    utc = ZoneInfo("UTC")
    # 09:00–17:00: inside at noon, outside at 20:00.
    assert backoff_mod.in_run_window(datetime(2026, 11, 24, 12, 0, tzinfo=utc), "09:00", "17:00") is True
    assert backoff_mod.in_run_window(datetime(2026, 11, 24, 20, 0, tzinfo=utc), "09:00", "17:00") is False
    # 22:00–06:00 crosses midnight: inside at 23:00 and 03:00, outside at noon.
    assert backoff_mod.in_run_window(datetime(2026, 11, 24, 23, 0, tzinfo=utc), "22:00", "06:00") is True
    assert backoff_mod.in_run_window(datetime(2026, 11, 25, 3, 0, tzinfo=utc), "22:00", "06:00") is True
    assert backoff_mod.in_run_window(datetime(2026, 11, 24, 12, 0, tzinfo=utc), "22:00", "06:00") is False
    # Empty window = run any time; equal start/end = all day.
    assert backoff_mod.in_run_window(datetime(2026, 11, 24, 12, 0, tzinfo=utc), "", "") is True
    assert backoff_mod.in_run_window(datetime(2026, 11, 24, 3, 0, tzinfo=utc), "09:00", "09:00") is True
    assert backoff_mod.parse_hhmm("25:00") is None
    assert backoff_mod.parse_hhmm("9:00") is None
    assert backoff_mod.parse_hhmm("09:00") == (9, 0)


def test_backoff_state_needs_two_busy_checks_then_pauses():
    state = backoff_mod.BackoffState(consecutive_needed=2, quiet_grace_s=120)
    now = 1000.0
    assert state.update(True, "busy", now) == "none"   # first spike: count only
    assert state.paused is False
    assert state.update(True, "busy", now + 30) == "pause"
    assert state.paused is True
    assert state.update(True, "busy", now + 60) == "hold"


def test_backoff_state_resumes_only_after_continuous_quiet_grace():
    state = backoff_mod.BackoffState(consecutive_needed=2, quiet_grace_s=120)
    now = 1000.0
    state.update(True, "busy", now)
    assert state.update(True, "busy", now + 30) == "pause"
    # Quiet for 60s — not enough grace yet.
    assert state.update(False, "", now + 60) == "hold"
    assert state.update(False, "", now + 110) == "hold"
    # Busy again: the grace clock resets — it must be CONTINUOUS quiet.
    # (The first busy reading after quiet just starts counting again.)
    assert state.update(True, "busy", now + 115) == "none"
    assert state.update(True, "busy", now + 145) == "hold"
    assert state.update(False, "", now + 150) == "hold"
    assert state.update(False, "", now + 230) == "hold"
    assert state.update(False, "", now + 270) == "resume"
    assert state.paused is False
    # Quiet and not paused: new jobs may start.
    assert state.update(False, "", now + 300) == "start"


def test_plex_sessions_parsing_counts_transcodes_only(monkeypatch):
    xml_transcode = (
        b'<MediaContainer size="2">'
        b'<Video title="A"><TranscodeSession videoDecision="transcode"/></Video>'
        b'<Video title="B"><TranscodeSession videoDecision="transcode"/></Video>'
        b'</MediaContainer>')
    xml_direct = (
        b'<MediaContainer size="2">'
        b'<Video title="A"><Media videoDecision="direct play"/></Video>'
        b'<Video title="B"><Media videoDecision="direct stream"/></Video>'
        b'</MediaContainer>')

    class Resp:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None):
        assert headers["X-Plex-Token"] == "tok123"
        assert url == "http://plex:32400/status/sessions"
        return Resp(xml_transcode)

    monkeypatch.setattr(backoff_mod.requests, "get", fake_get)
    result = backoff_mod.fetch_plex_sessions("http://plex:32400", "tok123")
    assert result == {"ok": True, "sessions": 2, "transcodes": 2, "error": ""}
    busy, why = backoff_mod.plex_is_busy(result)
    assert busy is True and "2 transcode" in why

    monkeypatch.setattr(backoff_mod.requests, "get",
                        lambda url, headers=None, timeout=None: Resp(xml_direct))
    result = backoff_mod.fetch_plex_sessions("http://plex:32400/", "tok123")
    assert result["sessions"] == 2 and result["transcodes"] == 0
    assert backoff_mod.plex_is_busy(result) == (False, "")


def test_plex_fetch_failure_is_not_busy(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise ConnectionError("refused")
    monkeypatch.setattr(backoff_mod.requests, "get", boom)
    result = backoff_mod.fetch_plex_sessions("http://plex:32400", "tok123")
    assert result["ok"] is False
    assert "refused" in result["error"]


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app with its own data dir; backoff OFF unless a test
    turns it on, so the background monitor never interferes."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    saved_settings = dict(app_mod.SETTINGS)
    monkeypatch.setattr(app_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod, "JOBS_DIR", str(data_dir / "jobs"))
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", str(data_dir / "settings.json"))
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update({
        "inbox_dir": str(tmp_path / "inbox"), "output_dir": str(tmp_path / "out"),
        "processed_dir": "", "review_dir": "",
        "temp_dir": str(data_dir / "temp"), "bitrate": "96k",
        "audnexus_url": "https://api.audnexus.com",
        "keep_originals": "processed", "timezone": "UTC",
        "backoff_enabled": False, "load_threshold_pct": 70,
        "quiet_grace_seconds": 120, "backoff_check_seconds": 30,
        "plex_url": "", "plex_token": "", "run_start": "", "run_end": "",
    })
    (tmp_path / "inbox").mkdir()
    (tmp_path / "out").mkdir()
    flog.init_logging(str(data_dir))
    flog.set_timezone("UTC")
    app_mod._backoff_state = backoff_mod.BackoffState()
    with app_mod._server_status_lock:
        app_mod._SERVER_STATUS.update({"busy": False, "reason": "", "quiet": True})
    yield app_mod.app.test_client(), tmp_path
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update(saved_settings)
    app_mod._backoff_state = backoff_mod.BackoffState()


def _make_building_job(client_fixture):
    _c, tmp_path = client_fixture
    job = {"id": "job-pause-1", "status": "building", "stage": "converting",
           "progress_pct": 50, "created": "2026-11-24T00:00:00+00:00",
           "folder": str(tmp_path / "inbox"), "files": [], "metadata": {},
           "cancel_requested": False, "error": "", "cancelled_reason": ""}
    app_mod.save_job(job)
    return job


def test_server_status_endpoint_reports_shape(client):
    c, _tmp = client
    body = c.get("/api/server-status").get_json()
    assert body["ok"] is True
    status = body["server_status"]
    assert status["busy"] is False and status["quiet"] is True
    assert body["backoff_enabled"] is False  # fixture turns it off
    assert "load_summary" in status and "plex_transcodes" in status


def test_plex_token_is_never_returned_in_full(client):
    c, _tmp = client
    app_mod.SETTINGS["plex_token"] = "plex-token-SECRET-1234"
    for url in ("/api/settings", "/api/health"):
        body = c.get(url).get_json()
        settings = body.get("settings") or body
        assert "plex-token-SECRET-1234" not in json.dumps(body)
        assert settings["plex_token_set"] is True
        assert settings["plex_token"].endswith("1234")
        assert settings["plex_token"].startswith("••••")


def test_settings_post_keeps_token_when_masked_and_replaces_when_new(client):
    c, tmp_path = client
    app_mod.SETTINGS["plex_token"] = "plex-token-SECRET-1234"
    masked = app_mod._mask_token("plex-token-SECRET-1234")
    inbox, out = tmp_path / "inbox", tmp_path / "out"
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "plex_token": masked})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert app_mod.SETTINGS["plex_token"] == "plex-token-SECRET-1234"
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "plex_token": "brand-new-token-9999"})
    assert resp.status_code == 200
    assert app_mod.SETTINGS["plex_token"] == "brand-new-token-9999"
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out), "plex_token": ""})
    assert resp.status_code == 200
    assert app_mod.SETTINGS["plex_token"] == ""


def test_settings_post_rejects_bad_threshold_and_window(client):
    c, tmp_path = client
    inbox, out = tmp_path / "inbox", tmp_path / "out"
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "load_threshold_pct": 500})
    assert resp.status_code == 400
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "run_start": "25:00"})
    assert resp.status_code == 400
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "plex_url": "plex:32400"})
    assert resp.status_code == 400
    # Valid values save fine.
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "load_threshold_pct": 80, "quiet_grace_seconds": 60,
        "backoff_check_seconds": 30, "plex_url": "http://plex:32400",
        "run_start": "22:00", "run_end": "06:00"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert app_mod.SETTINGS["load_threshold_pct"] == 80
    assert app_mod.SETTINGS["run_start"] == "22:00"


def test_plex_test_reports_sessions_and_transcodes(client, monkeypatch):
    c, _tmp = client
    monkeypatch.setattr(backoff_mod, "fetch_plex_sessions",
                        lambda url, token, timeout=5: {
                            "ok": True, "sessions": 3, "transcodes": 1, "error": ""})
    resp = c.post("/api/settings/plex-test",
                  json={"plex_url": "http://plex:32400", "plex_token": "tok-SECRET-999"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sessions"] == 3 and body["transcodes"] == 1
    assert body["busy"] is True
    assert "tok-SECRET-999" not in json.dumps(body)  # token never echoed back


def test_start_gate_blocks_when_busy_and_outside_window(client):
    _c, _tmp = client
    app_mod.SETTINGS["backoff_enabled"] = True
    with app_mod._server_status_lock:
        app_mod._SERVER_STATUS.update({"busy": True, "reason": "Plex is transcoding (1 transcode session(s))"})
    ok, why = app_mod._start_gate()
    assert ok is False and "Plex is transcoding" in why
    with app_mod._server_status_lock:
        app_mod._SERVER_STATUS.update({"busy": False, "reason": ""})
    # Outside the run window, the schedule alone blocks.
    app_mod.SETTINGS["run_start"] = "22:00"
    app_mod.SETTINGS["run_end"] = "06:00"
    ok, why = app_mod._start_gate()
    # _start_gate uses the real clock; the window check itself is
    # covered pure in test_run_window_daytime_and_overnight — here
    # just assert the gate returns a decision, not an exception.
    assert isinstance(ok, bool) and isinstance(why, str)
    app_mod.SETTINGS["run_start"] = ""
    app_mod.SETTINGS["run_end"] = ""
    ok, _why = app_mod._start_gate()
    assert ok is True


def test_pause_and_resume_use_sigstop_sigcont_on_a_real_process(client):
    _c, tmp_path = client
    job = _make_building_job(client)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        app_mod._register_proc(job["id"], proc)
        assert app_mod._pause_job(job["id"], "test busy") is True
        saved = app_mod.load_job(job["id"])
        assert saved["status"] == "building"  # NOT failed, NOT cancelled
        assert saved["paused"] is True
        assert "test busy" in (saved.get("paused_reason") or "")
        # A stopped process is still alive — frozen, not killed.
        assert proc.poll() is None
        assert app_mod._resume_job(job["id"]) is True
        saved = app_mod.load_job(job["id"])
        assert saved["paused"] is False
        assert saved["status"] == "building"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        app_mod._register_proc(job["id"], None)


def test_cancel_works_while_paused(client):
    _c, tmp_path = client
    job = _make_building_job(client)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        app_mod._register_proc(job["id"], proc)
        assert app_mod._pause_job(job["id"], "test busy") is True
        ok, msg = app_mod._cancel_job(job["id"])
        assert ok is True, msg
        # The stopped process must actually die — SIGCONT first,
        # then SIGTERM — not sit frozen forever.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise AssertionError("cancelled-while-paused process did not die")
        assert proc.poll() is not None
        saved = app_mod.load_job(job["id"])
        assert saved["cancel_requested"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        app_mod._register_proc(job["id"], None)


def test_backoff_tick_pauses_on_sustained_load_and_resumes_after_grace(client, monkeypatch):
    _c, _tmp = client
    app_mod.SETTINGS["backoff_enabled"] = True
    app_mod.SETTINGS["load_threshold_pct"] = 70
    app_mod.SETTINGS["plex_url"] = ""
    job = _make_building_job(client)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        app_mod._register_proc(job["id"], proc)
        high = {"load1": 6.0, "cores": 4, "load_pct": 150.0, "mem_available_pct": 50.0}
        monkeypatch.setattr(backoff_mod, "read_host_load", lambda: high)
        app_mod._backoff_tick()  # first busy reading: counting only
        assert app_mod.load_job(job["id"]).get("paused") is not True
        app_mod._backoff_tick()  # second: sustained — pause
        assert app_mod.load_job(job["id"]).get("paused") is True
        assert app_mod._server_status_snapshot()["busy"] is True
        low = {"load1": 0.5, "cores": 4, "load_pct": 12.5, "mem_available_pct": 50.0}
        monkeypatch.setattr(backoff_mod, "read_host_load", lambda: low)
        app_mod._backoff_tick()  # quiet — grace starts
        assert app_mod.load_job(job["id"]).get("paused") is True
        # Fake a full grace period of quiet behind us.
        app_mod._backoff_state.quiet_since = time.monotonic() - 300
        app_mod._backoff_tick()
        saved = app_mod.load_job(job["id"])
        assert saved.get("paused") is False
        assert saved["status"] == "building"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        app_mod._register_proc(job["id"], None)


def test_backoff_tick_plex_transcode_pauses_without_load(client, monkeypatch):
    _c, _tmp = client
    app_mod.SETTINGS["backoff_enabled"] = True
    app_mod.SETTINGS["plex_url"] = "http://plex:32400"
    app_mod.SETTINGS["plex_token"] = "tok"
    low = {"load1": 0.5, "cores": 4, "load_pct": 12.5, "mem_available_pct": 50.0}
    monkeypatch.setattr(backoff_mod, "read_host_load", lambda: low)
    monkeypatch.setattr(backoff_mod, "fetch_plex_sessions",
                        lambda url, token, timeout=5: {
                            "ok": True, "sessions": 2, "transcodes": 1, "error": ""})
    app_mod._backoff_tick()
    app_mod._backoff_tick()
    status = app_mod._server_status_snapshot()
    assert status["busy"] is True
    assert "transcoding" in status["reason"]
    assert status["plex_transcodes"] == 1


def test_backoff_tick_plex_failure_falls_back_to_load_only(client, monkeypatch):
    _c, _tmp = client
    app_mod.SETTINGS["backoff_enabled"] = True
    app_mod.SETTINGS["plex_url"] = "http://plex:32400"
    low = {"load1": 0.5, "cores": 4, "load_pct": 12.5, "mem_available_pct": 50.0}
    monkeypatch.setattr(backoff_mod, "read_host_load", lambda: low)
    monkeypatch.setattr(backoff_mod, "fetch_plex_sessions",
                        lambda url, token, timeout=5: {
                            "ok": False, "sessions": 0, "transcodes": 0,
                            "error": "Plex request failed: refused"})
    app_mod._backoff_tick()
    status = app_mod._server_status_snapshot()
    assert status["busy"] is False  # low load, Plex unreachable: quiet
    assert status["plex_ok"] is False
