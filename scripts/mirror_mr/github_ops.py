"""GitHub operations for the open-MR mirror route (issue #142).

Production implementation through the ``gh`` CLI (no UI automation): the
operator's authenticated ``gh`` context owns the credentials, and this
module passes repository names only. Every function fails closed with
``GithubError``; callers decide the verdict. A fixture double with the same
function names stands in for tests (see ``tests/mirror_mr``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DETAIL_MAX = 400


class GithubError(Exception):
    """A GitHub read or write that cannot be proven."""


def _gh(*args: str) -> str:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False)
    if proc.returncode != 0:
        raise GithubError((proc.stderr or proc.stdout).strip()[:DETAIL_MAX]
                          or "gh failed")
    return proc.stdout.strip()


def _loads(text: str, what: str) -> dict | list:
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError as error:
        raise GithubError(f"{what} is not JSON: {error}")


def repo_visibility(repo: str) -> str:
    """The current visibility of one GitHub repository (``private`` expected)."""

    data = _loads(_gh("repo", "view", repo, "--json", "visibility"), "repo view")
    visibility = str(data.get("visibility") or "").lower()
    if not visibility:
        raise GithubError(f"gh reports no visibility for {repo}")
    return visibility


def find_open_pr(repo: str, head: str, base: str) -> dict | None:
    """The single open PR for one head/base pair, or None.

    More than one match is an ambiguity the caller refuses instead of
    guessing; callers reconcile an existing PR instead of opening a second.
    """

    rows = _loads(_gh("pr", "list", "--repo", repo, "--state", "open",
                      "--head", head, "--base", base, "--json",
                      "url,number,state,headRefName,baseRefName"),
                  "pr list")
    if not isinstance(rows, list):
        raise GithubError("pr list returned no array")
    matches = [row for row in rows
               if isinstance(row, dict) and row.get("headRefName") == head
               and row.get("baseRefName") == base]
    if len(matches) > 1:
        raise GithubError(f"{len(matches)} open PRs for {head} -> {base}")
    return matches[0] if matches else None


def create_pr(repo: str, title: str, body: str, base: str, head: str) -> dict:
    """Open one PR; the caller reconciles first so this never duplicates."""

    out = _gh("pr", "create", "--repo", repo, "--title", title,
              "--body", body, "--base", base, "--head", head)
    url = out.strip().splitlines()[-1] if out.strip() else ""
    if not url.startswith("https://"):
        raise GithubError(f"pr create returned no URL: {out[:DETAIL_MAX]}")
    record = read_pr(url)
    record["url"] = url
    return record


def read_pr(url: str) -> dict:
    """Read one PR back by URL: identity, state, branches, and SHAs."""

    data = _loads(_gh("pr", "view", url, "--json",
                      "url,number,state,title,headRefName,headRefOid,"
                      "baseRefName,baseRefOid"), "pr view")
    if not isinstance(data, dict) or not data.get("url"):
        raise GithubError(f"pr view returned no PR: {url}")
    return data


def mirror_refs(repo_dir: Path, remote: str = "origin") -> dict:
    """Locally observed remote heads: the readback's independent ref check."""

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
