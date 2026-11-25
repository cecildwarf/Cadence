"""Single-mount tests: one media parent mounted into the container.

A running container cannot gain a new host bind-mount from inside
itself, so Cadence mounts ONE media parent (MOUNTED_ROOT, e.g.
/disks in the container) and every media path must live under it.
Paths under it can change live in Settings (no restart); a path
outside it is rejected with a plain message telling the user to
change MEDIA_ROOT in .env and recreate the container.

When MOUNTED_ROOT is unset (running without Docker / dev), there
is no mount restriction to enforce — paths behave as before.
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
    root = tmp_path / "disks"
    root.mkdir()
    saved_settings = dict(app_mod.SETTINGS)
    monkeypatch.setattr(app_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod, "JOBS_DIR", str(data_dir / "jobs"))
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", str(data_dir / "settings.json"))
    monkeypatch.setenv("MOUNTED_ROOT", str(root))
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update({
        "inbox_dir": "", "output_dir": "",
        "temp_dir": str(data_dir / "temp"), "bitrate": "96k",
        "audnexus_url": "https://api.audnexus.com",
        "keep_originals": "processed", "timezone": "UTC",
    })
    flog.init_logging(str(data_dir))
    flog.set_timezone("UTC")
    yield app_mod.app.test_client(), tmp_path, root
    app_mod.SETTINGS.clear()
    app_mod.SETTINGS.update(saved_settings)
    monkeypatch.delenv("MOUNTED_ROOT", raising=False)


def _dirs(root):
    inbox = root / "Audiobooks" / "Inbox"
    out = root / "Audiobooks"
    inbox.mkdir(parents=True)
    return inbox, out


def test_path_under_mounted_root_accepted(client):
    c, _tmp, root = client
    inbox, out = _dirs(root)
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out), "timezone": "UTC"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["setup_required"] is False


def test_path_outside_mounted_root_rejected_with_mount_message(client):
    c, tmp_path, root = client
    _inbox, out = _dirs(root)
    resp = c.post("/api/settings", json={
        "inbox_dir": "/etc", "output_dir": str(out), "timezone": "UTC"})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "isn't mounted into the container" in body["error"]
    assert "MEDIA_ROOT" in body["error"]
    assert "docker compose up -d" in body["error"]
    # The bad path was NOT saved.
    assert app_mod.SETTINGS["inbox_dir"] == ""


def test_processed_outside_mounted_root_rejected(client):
    c, tmp_path, root = client
    inbox, out = _dirs(root)
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out),
        "processed_dir": str(tmp_path / "elsewhere"),
        "timezone": "UTC"})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "isn't mounted into the container" in resp.get_json()["error"]
    assert app_mod.SETTINGS.get("processed_dir", "") == ""


def test_folder_picker_clamped_to_mounted_root(client):
    c, _tmp, root = client
    # Asking for the server root ("/") or an outside folder lands
    # on the mounted root instead — the picker can only offer
    # folders the container can see.
    for asked in ("/", "/etc", "/nonexistent"):
        resp = c.get("/api/folders", query_string={"path": asked})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["path"] == str(root), f"asked {asked}, got {body['path']}"
        assert body["root"] == str(root)
        assert body["parent"] == str(root)
    # Inside the root, browsing works normally.
    (root / "Audiobooks").mkdir(exist_ok=True)
    resp = c.get("/api/folders", query_string={"path": str(root)})
    assert resp.status_code == 200
    names = [e["name"] for e in resp.get_json()["entries"]]
    assert "Audiobooks" in names


def test_suggestions_stay_under_mounted_root(client):
    c, _tmp, root = client
    (root / "Audiobooks" / "Inbox").mkdir(parents=True)
    sugg = c.get("/api/folders/suggestions").get_json()
    assert sugg["ok"] and sugg["mounted_root"] == str(root)
    for s in sugg["suggestions"]:
        assert s["path"] == str(root) or s["path"].startswith(str(root) + "/"), \
            f"suggestion outside mount: {s['path']}"


def test_settings_reports_mounted_folders(client):
    c, _tmp, root = client
    body = c.get("/api/settings").get_json()
    assert body["mounted_root"] == str(root)
    mounts = body["mounts"]
    media = [m for m in mounts if m["path"] == str(root)]
    assert len(media) == 1
    assert media[0]["exists"] is True
    assert media[0]["readable"] is True
    assert media[0]["writable"] is True
    assert any(m["label"].startswith("App data") for m in mounts)
    health = c.get("/api/health").get_json()
    assert health["mounted_root"] == str(root)


def test_no_mounted_root_means_no_restriction(client, monkeypatch):
    # Running without Docker (MOUNTED_ROOT unset): any real folder
    # is accepted, as before.
    monkeypatch.delenv("MOUNTED_ROOT", raising=False)
    c, tmp_path, root = client
    inbox = tmp_path / "plain-inbox"
    out = tmp_path / "plain-out"
    inbox.mkdir()
    out.mkdir()
    resp = c.post("/api/settings", json={
        "inbox_dir": str(inbox), "output_dir": str(out), "timezone": "UTC"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
