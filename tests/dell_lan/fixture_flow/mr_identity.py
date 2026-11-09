"""Fixture double for the MR readback seam (tests only, never production).

Implements the narrow ``capture_identity`` surface the Lenovo publish driver
and the read-only reconcile probe consume: bind the canonical MR URL plus the
recorded head SHA to the local checkout head. The receipt carries the fields
the Dell readback binds (schema version, host, project, branches, MR URL,
integer IID, both SHAs, capture time, local head proof); it never contacts a
remote. Also usable as a CLI: ``mr_identity.py capture --url <mr> --repo
<checkout>`` prints the receipt, which is how the readback capture chain
exercises it through the signed queue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import gitlab_api

CAPTURE_SCHEMA_VERSION = 1
_MR_RE = re.compile(r"^https?://([^/]+)/(.+)/-/merge_requests/(\d+)$")


class IdentityError(Exception):
    """The MR identity could not be captured."""


class ContextMismatch(Exception):
    """The MR identity does not belong to this checkout."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state() -> dict:
    path = os.environ.get("DELL_FIXTURE_STATE", "")
    if not path:
        raise IdentityError("DELL_FIXTURE_STATE is not set")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IdentityError(f"unreadable fixture state: {error}")


def _local_head(worktree) -> str | None:
    if worktree is None:
        return None
    proc = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().lower()


def capture_identity(mr_url: str, _args, worktree, _extra) -> dict:
    """Read the pinned receipt for one canonical MR URL."""

    match = _MR_RE.match((mr_url or "").rstrip("/"))
    if not match:
        raise IdentityError(f"not a canonical MR URL: {mr_url}")
    host, project, iid = match.group(1), match.group(2), int(match.group(3))
    state = _state()
    if host != state.get("host") or project != state.get("project"):
        raise IdentityError(f"unknown fixture target: {host}/{project}")
    record = state.get("mrs", {}).get(str(iid))
    if not isinstance(record, dict):
        raise IdentityError(f"unknown fixture MR: {mr_url}")
    head_sha = str(record.get("head_sha") or "").lower()
    target_sha = str(record.get("target_sha") or "").lower()
    if len(head_sha) != 40 or len(target_sha) != 40:
        raise IdentityError("fixture MR record carries no SHAs")
    local_head = _local_head(worktree)
    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "captured_at": _now_iso(),
        "gitlab_host": host,
        "project_path": project,
        "project_path_encoded": project.replace("/", "%2F"),
        "iid": iid,
        "mr_url": gitlab_api.merge_request_url(host, project, iid),
        "title": record.get("title", ""),
        "state": "opened",
        "source_branch": record.get("source_branch", ""),
        "target_branch": record.get("target_branch", ""),
        "head_sha": head_sha,
        "target_sha": target_sha,
        "changes_count": "1",
        "changed_files": ["work.txt"],
        "links": {"mr": gitlab_api.merge_request_url(host, project, iid)},
        "local": {
            "repo": str(worktree) if worktree is not None else None,
            "matching_remotes": ["origin"],
            "source_branch_head": local_head,
            "head_matches_receipt": local_head is not None and local_head == head_sha,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mr_identity.py")
    parser.add_argument("verb", choices=("capture",))
    parser.add_argument("--url", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = capture_identity(args.url, [], Path(args.repo), None)
    except (IdentityError, ContextMismatch) as error:
        print(f"mr_identity capture refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
