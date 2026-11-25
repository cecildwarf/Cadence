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

FORBIDDEN = [
    "192.168", "/disks", "Denver", "Beelink", "Cecil", "N150",
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
    assert "/disks" not in compose
    assert "192.168" not in compose
