"""Generic-install check: no hardcoded personal/host values anywhere
in the app's source, docs, UI or Docker files.

Forbidden (except the project's own repo home URL, which is the
one allowed 'cecildwarf' reference, in the README install steps):
  * a specific LAN IP, a specific disk layout, a fixed timezone,
    a specific host machine, a person's name.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# "/disks" is NOT in this list on purpose: it is the container-side
# mount point (MEDIA_ROOT on the host is mounted at /disks inside
# the container) — a fixed Docker convention of this image, not a
# host disk layout. It is allowed only as that mount target (see
# test_disks_only_as_container_mount below) and never as a path
# assumed in app code.
FORBIDDEN = [
    "192.168", "Denver", "Beelink", "Cecil", "N150",
    "America/Denver",
]

# Files that make up the shipped app (tests are excluded: they may
# use throwaway example names; data/ is runtime state, not source).
INCLUDE_EXTS = {".py", ".html", ".js", ".css", ".md", ".yml", ".yaml"}
INCLUDE_NAMES = {"Dockerfile", ".env.example"}


def _repo_files():
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in ("tests", "data", "__pycache__", ".venv",
                                ".pytest_cache", ".git")]
        for name in names:
            path = os.path.join(root, name)
            if (os.path.splitext(name)[1] in INCLUDE_EXTS
                    or name in INCLUDE_NAMES):
                yield path


def test_no_hardcoded_personal_or_host_values():
    hits = []
    for path in _repo_files():
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, start=1):
                for bad in FORBIDDEN:
                    if bad in line:
                        hits.append(f"{os.path.relpath(path, REPO)}:{lineno}: {bad}")
                if "cecildwarf" in line:
                    # Allowed exactly once: the project's own repo URL.
                    if not re.search(r"github\.com/cecildwarf/cadence", line):
                        hits.append(f"{os.path.relpath(path, REPO)}:{lineno}: cecildwarf")
    assert not hits, "Hardcoded personal/host values found:\n" + "\n".join(hits)


def test_repo_url_is_the_only_cecildwarf_reference():
    """Every 'cecildwarf' in the shipped repo must be part of the
    project's own repo URL — nowhere else."""
    bad = []
    for path in _repo_files():
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, start=1):
                if "cecildwarf" in line and \
                        "github.com/cecildwarf/cadence" not in line:
                    bad.append(f"{os.path.relpath(path, REPO)}:{lineno}")
    assert not bad, "cecildwarf outside the repo URL:\n" + "\n".join(bad)


def test_no_assumed_inbox_output_defaults_in_code():
    """Paths must come from first-run setup or the environment —
    never from a baked-in default in app code."""
    app_py = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    assert "/disks" not in app_py
    assert 'os.environ.get("INBOX_DIR", "")' in app_py
    assert 'os.environ.get("OUTPUT_DIR", "")' in app_py
    compose = open(os.path.join(REPO, "docker-compose.yml"),
                   encoding="utf-8").read()
    assert "192.168" not in compose


def test_disks_only_as_container_mount():
    """'/disks' may appear only as the container-side mount target
    of the single media mount — never as a host path the app
    assumes, and never in app code (the app reads MOUNTED_ROOT
    from the environment). The old per-folder host mounts are gone.
    """
    app_py = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    assert "/disks" not in app_py
    for name in ("docker-compose.yml", ".env.example"):
        text = open(os.path.join(REPO, name), encoding="utf-8").read()
        assert "INBOX_HOST_DIR" not in text, f"{name} still has INBOX_HOST_DIR"
        assert "OUTPUT_HOST_DIR" not in text, f"{name} still has OUTPUT_HOST_DIR"
    # The short README does not mention them at all; allow at most
    # one stray mention, never as live settings.
    readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
    assert readme.count("INBOX_HOST_DIR") <= 1
    assert readme.count("OUTPUT_HOST_DIR") <= 1
    compose = open(os.path.join(REPO, "docker-compose.yml"),
                   encoding="utf-8").read()
    assert "${MEDIA_ROOT:-/disks}:/disks:rw" in compose
    assert "MOUNTED_ROOT=/disks" in compose
    # Every variable in compose must use the :-default form so
    # the project runs with no .env file at all. An empty default
    # (${INBOX_DIR:-}) is a real default — it means "unset, the
    # first-run wizard will ask" — while a bare ${VAR} would make
    # compose fail/warn without a .env file.
    import re
    for ref in re.findall(r"\$\{[^}]+\}", compose):
        assert re.fullmatch(r"\$\{[A-Z_]+:-[^}]*\}", ref), \
            f"{ref} in docker-compose.yml lacks a :-default — .env would be required"
