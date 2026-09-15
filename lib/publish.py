"""Publish the tool to GitHub via the gh CLI.

Publishes the *tool*, never the media. Uploaded footage, narration takes and
rendered output are all gitignored, so running this on a machine holding customer
recordings cannot push them to a remote — the safety of that guarantee is why
add() enumerates an explicit file list instead of using `git add -A`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Everything that constitutes the tool. An explicit allowlist, so a stray .mp4 in
# the project root can never be committed even if .gitignore is edited.
TRACKED = [
    "app.py", "requirements.txt", "README.md", ".gitignore", "LICENSE",
    ".streamlit/config.toml",
    "lib/__init__.py", "lib/profile.py", "lib/transcript.py",
    "lib/audio.py", "lib/assemble.py", "lib/publish.py",
    "tests/test_app.py", "tests/test_lib.py",
    "samples/example_transcript.txt",
]


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def gh_ready() -> tuple[bool, str]:
    if not _run(["command", "-v", "gh"]).returncode == 0:
        if _run(["which", "gh"]).returncode != 0:
            return False, "gh CLI is not installed (brew install gh)."
    r = _run(["gh", "auth", "status"])
    if r.returncode != 0:
        return False, "gh is installed but not authenticated (gh auth login)."
    who = _run(["gh", "api", "user", "-q", ".login"]).stdout.strip()
    return True, who or "authenticated"


def repo_exists(slug: str) -> bool:
    return _run(["gh", "repo", "view", slug, "-q", ".name"]).returncode == 0


def publish(root: Path, slug: str, private: bool = True,
            message: str = "Narration Studio") -> dict:
    """Create or update the repo. Returns a result dict with a log."""
    log: list[str] = []

    def step(label: str, r: subprocess.CompletedProcess) -> bool:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        log.append(f"{'ok  ' if r.returncode == 0 else 'FAIL'} {label}"
                   + (f" — {tail[-1][:160]}" if tail and r.returncode != 0 else ""))
        return r.returncode == 0

    ok, who = gh_ready()
    if not ok:
        return {"ok": False, "log": [f"FAIL {who}"], "url": ""}

    if not (root / ".git").exists():
        step("git init", _run(["git", "init", "-b", "main"], root))
    else:
        log.append("ok   repo already initialised")

    present = [f for f in TRACKED if (root / f).exists()]
    step(f"stage {len(present)} tool files", _run(["git", "add", "--"] + present, root))

    r = _run(["git", "diff", "--cached", "--quiet"], root)
    if r.returncode == 0:
        log.append("ok   nothing new to commit")
    else:
        step("commit", _run(["git", "commit", "-m", message], root))

    if not repo_exists(slug):
        vis = "--private" if private else "--public"
        if not step(f"create {slug} ({vis[2:]})",
                    _run(["gh", "repo", "create", slug, vis,
                          "--source", ".", "--remote", "origin"], root)):
            return {"ok": False, "log": log, "url": ""}
    else:
        log.append(f"ok   {slug} already exists")
        if _run(["git", "remote", "get-url", "origin"], root).returncode != 0:
            step("add remote", _run(
                ["git", "remote", "add", "origin",
                 f"https://github.com/{slug}.git"], root))

    pushed = step("push", _run(["git", "push", "-u", "origin", "main"], root))
    url = _run(["gh", "repo", "view", slug, "-q", ".url"], root).stdout.strip()
    return {"ok": pushed, "log": log, "url": url or f"https://github.com/{slug}"}


def status(root: Path) -> str:
    if not (root / ".git").exists():
        return "not a git repo yet"
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
    dirty = _run(["git", "status", "--porcelain"], root).stdout.strip().splitlines()
    remote = _run(["git", "remote", "get-url", "origin"], root).stdout.strip()
    bits = [f"branch {branch or '—'}"]
    if remote:
        bits.append(remote)
    bits.append(f"{len(dirty)} uncommitted path(s)" if dirty else "clean")
    return " · ".join(bits)
