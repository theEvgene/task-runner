"""Fixture GitHub double for the mirror-mr tests (never production).

Implements the `github_ops` function surface `dell_mirror_import.py`
consumes (`repo_visibility`, `find_open_pr`, `create_pr`, `read_pr`,
`mirror_refs`) against a disposable JSON state file
(`MIRROR_FIXTURE_GITHUB`) plus the local bare repository standing in for
the private GitHub mirror (`MIRROR_FIXTURE_REMOTE`). SHAs are observed with
real `git` commands against that bare remote, so a pushed-but-unrecorded
ref is still evidence. `MIRROR_FIXTURE_FAIL_AFTER_WRITE=1` persists the PR
write and then raises, proving the driver's unknown-outcome path.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

DETAIL_MAX = 400


class GithubError(Exception):
    """The fixture GitHub operation could not be proven."""


def _state_path() -> Path:
    path = os.environ.get("MIRROR_FIXTURE_GITHUB", "")
    if not path:
        raise GithubError("MIRROR_FIXTURE_GITHUB is not set")
    return Path(path)


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GithubError(f"unreadable fixture PR state: {error}")


def _save(state: dict) -> None:
    _state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remote() -> str:
    remote = os.environ.get("MIRROR_FIXTURE_REMOTE", "")
    if not remote:
        raise GithubError("MIRROR_FIXTURE_REMOTE is not set")
    return remote


def _remote_head(branch: str) -> str:
    proc = subprocess.run(
        ["git", "--git-dir", _remote(), "rev-parse", "--verify",
         f"refs/heads/{branch}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)
    if proc.returncode != 0:
        raise GithubError(f"fixture mirror has no {branch}")
    return proc.stdout.strip().lower()


def _pr_url(repo: str, number: int) -> str:
    return f"https://github.fixture.invalid/{repo}/pull/{number}"


def repo_visibility(repo: str) -> str:
    state = _load()
    if repo != state.get("repo"):
        raise GithubError(f"unknown fixture mirror: {repo}")
    visibility = str(state.get("visibility") or "").lower()
    if not visibility:
        raise GithubError(f"fixture mirror {repo} reports no visibility")
    return visibility


def find_open_pr(repo: str, head: str, base: str) -> dict | None:
    state = _load()
    if repo != state.get("repo"):
        raise GithubError(f"unknown fixture mirror: {repo}")
    matches = [record for record in state.get("prs", {}).values()
               if isinstance(record, dict) and record.get("state") == "open"
               and record.get("headRefName") == head
               and record.get("baseRefName") == base]
    if len(matches) > 1:
        raise GithubError(f"{len(matches)} open fixture PRs for {head} -> {base}")
    return dict(matches[0]) if matches else None


def create_pr(repo: str, title: str, body: str, base: str, head: str) -> dict:
    state = _load()
    if repo != state.get("repo"):
        raise GithubError(f"unknown fixture mirror: {repo}")
    head_sha = _remote_head(head)
    base_sha = _remote_head(base)
    number = int(state.get("nextNumber", 1))
    state["nextNumber"] = number + 1
    record = {"url": _pr_url(repo, number), "number": number, "state": "open",
              "title": title, "body": body, "headRefName": head,
              "baseRefName": base, "headRefOid": head_sha,
              "baseRefOid": base_sha}
    state.setdefault("prs", {})[str(number)] = record
    _save(state)
    if os.environ.get("MIRROR_FIXTURE_FAIL_AFTER_WRITE") == "1":
        raise GithubError("simulated write loss after persist")
    return dict(record)


def read_pr(url: str) -> dict:
    state = _load()
    for record in state.get("prs", {}).values():
        if isinstance(record, dict) and record.get("url") == url:
            return dict(record)
    raise GithubError(f"unknown fixture PR: {url}")


def mirror_refs(repo_dir: Path, remote: str = "origin") -> dict:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-remote", "--heads", remote],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)
    if proc.returncode != 0:
        raise GithubError((proc.stderr or proc.stdout).strip()[:DETAIL_MAX])
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            refs[parts[1][len("refs/heads/"):]] = parts[0].lower()
    return refs
