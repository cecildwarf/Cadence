"""First-run setup tests.

Fresh settings (no settings file, no paths) -> the app reports
setup-required. Saving valid paths clears it. Saving an invalid
path is rejected with a clear error and nothing bad is saved.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod
import cadence_logging as flog


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    saved_settings = dict(app_mod.SETTINGS)
    monkeypatch.setattr(app_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod, "JOBS_DIR", str(data_dir / "jobs"))
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", str(data_dir / "settings.json"))
    # Fresh install: no settings file, no paths at all.
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update({
        "inbox_dir": "", "output_dir": "",
        "temp_dir": str(data_dir / "temp"), "bitrate": "96k",
        "audnexus_url": "https://api.audnexus.com",
        "keep_originals": "processed", "timezone": "UTC",
    })
    flog.init_logging(str(data_dir))
    flog.set_timezone("UTC")
    yield app_mod.app.test_client(), tmp_path
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update(saved_settings)


def test_fresh_settings_reports_setup_required(client):
    c, _tmp = client
    health = c.get("/api/health").get_json()
    assert health["setup_required"] is True
    assert health["setup_errors"], "setup errors must name what's missing"
    assert any(e["field"] == "inbox_dir" for e in health["setup_errors"])
    assert any(e["field"] == "output_dir" for e in health["setup_errors"])
    # Browsing is blocked until setup is done — no silent defaults.
    resp = c.get("/api/browse")
    assert resp.status_code == 400
    assert resp.get_json()["setup_required"] is True


def test_saving_valid_paths_clears_setup(client):
    c, tmp_path = client
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    inbox.mkdir()
    out.mkdir()
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out), "timezone": "UTC"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["setup_required"] is False
    assert c.get("/api/health").get_json()["setup_required"] is False
    # Persisted: a fresh load sees the same paths.
    assert app_mod.load_settings()["inbox_dir"] == str(inbox)


def test_saving_invalid_inbox_is_rejected(client):
    c, tmp_path = client
    out = tmp_path / "out"
    out.mkdir()
    resp = c.post("/api/settings", json={
        "inbox_dir": "/definitely/not/a/real/folder",
        "output_dir": str(out), "timezone": "UTC"})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["errors"] and "inbox" in body["error"].lower()
    assert c.get("/api/health").get_json()["setup_required"] is True
    # The bad path was NOT saved.
    assert app_mod.SETTINGS["inbox_dir"] == ""


def test_saving_invalid_timezone_is_rejected(client):
    c, tmp_path = client
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    inbox.mkdir()
    out.mkdir()
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "timezone": "Not/AZone"})
    assert resp.status_code == 400
    assert "timezone" in resp.get_json()["error"].lower()


def test_output_folder_can_be_created_on_save(client):
    c, tmp_path = client
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    missing_out = tmp_path / "new-library"
    # Without the flag: rejected, clear reason.
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(missing_out),
        "timezone": "UTC"})
    assert resp.status_code == 400
    assert "Create output folder if missing" in resp.get_json()["error"]
    # With the flag: created and accepted.
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(missing_out),
        "timezone": "UTC", "create_output": True})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert missing_out.is_dir()
    assert c.get("/api/health").get_json()["setup_required"] is False


def test_folder_picker_lists_server_folders(client):
    c, tmp_path = client
    (tmp_path / "media" / "Book A").mkdir(parents=True)
    (tmp_path / "media" / "Book B").mkdir(parents=True)
    resp = c.get("/api/folders", query_string={"path": str(tmp_path / "media")})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    names = [e["name"] for e in resp.get_json()["entries"]]
    assert names == ["Book A", "Book B"]
    sugg = c.get("/api/folders/suggestions").get_json()
    assert sugg["ok"] and isinstance(sugg["suggestions"], list)
    assert any(s["path"] == "/" for s in sugg["suggestions"])


def test_broken_saved_path_shows_setup_required(client):
    c, tmp_path = client
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    inbox.mkdir()
    out.mkdir()
    c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out), "timezone": "UTC"})
    assert c.get("/api/health").get_json()["setup_required"] is False
    # Disk unmounted / folder deleted after setup:
    out.rmdir()
    health = c.get("/api/health").get_json()
    assert health["setup_required"] is True
    assert any(e["field"] == "output_dir" for e in health["setup_errors"])
