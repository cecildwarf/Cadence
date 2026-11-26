"""install.sh / update.sh data-ownership tests.

The first-install bug: Docker creates a missing ./data bind-mount
folder as root, the container runs as 1000:1000 and cannot write
/app/data/logs, so it restart-loops with
"Permission denied: '/app/data/logs'". Both scripts must fix the
ownership *before* `docker compose up` — do nothing when it is
already right, never delete ./data contents, and fail with the
exact manual command when sudo cannot fix it.

Tests run the real scripts against stubbed docker / curl / git /
sudo / id binaries on PATH, in a throwaway directory. The
throwaway directory lives under /tmp (not the workspace): the
workspace filesystem does not permit chown, so ownership
assertions would fail there for filesystem reasons, not code ones.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def workroot():
    base = Path(tempfile.mkdtemp(prefix="cadence-scripts-", dir="/tmp"))
    yield base
    shutil.rmtree(base, ignore_errors=True)


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(body)
    p.chmod(0o755)


def _setup(base, sudo_mode="working", stub_id=False):
    """Return (workdir, calls_log, env). Scripts are copied in so
    `cd "$(dirname "$0")"` lands in the throwaway dir, never the
    real repo."""
    work = base / "work"
    work.mkdir()
    for name in ("install.sh", "update.sh"):
        shutil.copy2(REPO / name, work / name)
    bindir = base / "bin"
    bindir.mkdir()
    log = base / "calls.log"
    log.write_text("")
    _stub(bindir, "docker",
          f'#!/bin/sh\necho "docker $@" >> "{log}"\nexit 0\n')
    _stub(bindir, "curl", '#!/bin/sh\nexit 0\n')
    _stub(bindir, "git",
          f'#!/bin/sh\necho "git $@" >> "{log}"\nexit 0\n')
    if sudo_mode == "working":
        _stub(bindir, "sudo",
              f'#!/bin/sh\necho "sudo $@" >> "{log}"\n"$@"\n')
    elif sudo_mode == "failing":
        _stub(bindir, "sudo", '#!/bin/sh\nexit 1\n')
    elif sudo_mode == "absent":
        # Simulate sudo not being installed: from the script's side
        # this is indistinguishable from a missing binary — the
        # chown attempt fails and the error path must print the
        # manual command. (A real "no sudo on PATH" case cannot be
        # built here: the sandbox itself has sudo, and stripping it
        # from PATH would also hide stat/grep/mkdir.)
        _stub(bindir, "sudo",
              '#!/bin/sh\necho "sudo: command not found" >&2\nexit 127\n')
    if stub_id:
        # Pretend we are uid 1000 (not root) so the scripts take
        # the sudo branch instead of chowning directly.
        _stub(bindir, "id", '#!/bin/sh\necho 1000\n')
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    return work, log, env


def _run(work, script, env):
    return subprocess.run(["bash", str(work / script)],
                          cwd=str(work), env=env,
                          capture_output=True, text=True, timeout=60)


def _owner(path: Path) -> str:
    st = path.stat()
    return f"{st.st_uid}:{st.st_gid}"


def test_install_data_already_correct_does_nothing(workroot):
    work, log, env = _setup(workroot)
    data = work / "data"
    data.mkdir()
    (data / "keep.txt").write_text("settings go here")
    os.chown(data, 1000, 1000)
    res = _run(work, "install.sh", env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (data / "keep.txt").read_text() == "settings go here"
    calls = log.read_text()
    assert "docker compose up -d --build" in calls
    assert "chown" not in calls, calls


def test_install_root_owned_data_fixed_via_sudo(workroot):
    work, log, env = _setup(workroot, stub_id=True)
    data = work / "data"
    data.mkdir()
    (data / "keep.txt").write_text("settings go here")
    assert _owner(data) != "1000:1000"  # root-owned, as Docker leaves it
    res = _run(work, "install.sh", env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _owner(data) == "1000:1000"
    assert (data / "keep.txt").read_text() == "settings go here"
    calls = log.read_text()
    assert calls.index("sudo chown -R 1000:1000 ./data") < \
        calls.index("docker compose up -d --build"), calls


def test_install_sudo_fails_prints_exact_command_and_stops(workroot):
    work, log, env = _setup(workroot, sudo_mode="failing", stub_id=True)
    (work / "data").mkdir()
    res = _run(work, "install.sh", env)
    assert res.returncode != 0
    assert "sudo chown -R 1000:1000 ./data" in res.stdout + res.stderr
    assert "docker compose up" not in log.read_text()


def test_install_sudo_missing_prints_exact_command_and_stops(workroot):
    work, log, env = _setup(workroot, sudo_mode="absent", stub_id=True)
    (work / "data").mkdir()
    res = _run(work, "install.sh", env)
    assert res.returncode != 0
    assert "sudo chown -R 1000:1000 ./data" in res.stdout + res.stderr
    assert "docker compose up" not in log.read_text()


def test_install_creates_missing_data_owned_right(workroot):
    work, log, env = _setup(workroot)
    assert not (work / "data").exists()
    res = _run(work, "install.sh", env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _owner(work / "data") == "1000:1000"


def test_install_respects_puid_pgid_from_dotenv(workroot):
    work, log, env = _setup(workroot, stub_id=True)
    (work / ".env").write_text("PUID=1001\nPGID=1001\n")
    (work / "data").mkdir()
    res = _run(work, "install.sh", env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _owner(work / "data") == "1001:1001"
    assert "sudo chown -R 1001:1001 ./data" in log.read_text()


def test_update_fixes_data_after_git_pull(workroot):
    work, log, env = _setup(workroot, stub_id=True)
    (work / "data").mkdir()
    res = _run(work, "update.sh", env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _owner(work / "data") == "1000:1000"
    calls = log.read_text()
    assert calls.index("git pull --ff-only") < \
        calls.index("sudo chown -R 1000:1000 ./data") < \
        calls.index("docker compose up -d --build"), calls
