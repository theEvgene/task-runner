"""Standalone request builders: transfer-only and publish-existing.

Split from ``dell_request.py`` to stay under the repo file-token guard.
``transfer`` reuses the staged bundle shape (no MR metadata);
``publish-existing`` names a branch that already exists on Lenovo
(no bundle, no second GitLab client). Small regexes mirror
``dell_request.py`` so neither side imports the other CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

from dell_lan_common import Stop, inside, safe_name
from dell_standalone_common import (
    PUBLISH_EXISTING_REQUEST_SCHEMA, TRANSFER_REQUEST_SCHEMA,
)

TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
AUTHORIZABLE = ("import", "push", "mr", "undraft", "reconcile")
PREVIOUS_RE = re.compile(r"^[0-9a-f]{40}$")


def require_absolute_lenovo_paths(*pairs: tuple[str, str]) -> None:
    for label, value in pairs:
        if not value or not Path(value).is_absolute():
            raise Stop("usage", code=2, detail=(
                f"{label} must be the absolute path on the Lenovo machine"))


def transfer_request(args) -> dict:
    """Standalone transfer: the same staged bundle, no MR metadata required."""

    for name in ("expected_base", "expected_commit"):
        if not SHA_RE.match((getattr(args, name) or "").lower()):
            raise Stop("usage", code=2, detail=f"--{name.replace('_', '-')} must be 40 hex")
    if not TOKEN_RE.match(args.token or ""):
        raise Stop("usage", code=2, detail="--token must be 16 lowercase hex")
    if not BRANCH_RE.match(args.branch or "") or ".." in (args.branch or ""):
        raise Stop("usage", code=2, detail="--branch is not a safe branch name")
    authorized = [item.strip() for item in (args.authorize or "").split(",") if item.strip()]
    unknown = sorted(set(authorized) - set(AUTHORIZABLE))
    if unknown:
        raise Stop("usage", code=2, detail=f"unknown authorization: {', '.join(unknown)}")
    if "push" in authorized or "mr" in authorized or "undraft" in authorized:
        raise Stop("usage", code=2, detail="transfer authorizes only import[,reconcile]")
    shared = Path(args.shared)
    for label, value in (("--manifest", args.manifest), ("--bundle", args.bundle)):
        if not inside(Path(value), shared):
            raise Stop("usage", code=2, detail=f"{label} must live on the shared LAN path")
    if not safe_name(Path(args.bundle).name):
        raise Stop("usage", code=2, detail="--bundle must name one contained file")
    require_absolute_lenovo_paths(
        ("--lenovo-checkout", args.lenovo_checkout),
        ("--lenovo-worktree", args.lenovo_worktree),
    )
    previous = (args.expected_previous or "").strip().lower() or None
    if previous is not None and not PREVIOUS_RE.match(previous):
        raise Stop("usage", code=2, detail="--expected-previous must be 40 hex")
    if previous == args.expected_commit.lower():
        previous = None
    return {
        "schema": TRANSFER_REQUEST_SCHEMA,
        "token": args.token,
        "shared": str(shared),
        "manifest": str(Path(args.manifest)),
        "bundle": str(Path(args.bundle)),
        "expectedBaseSha": args.expected_base.lower(),
        "expectedCommit": args.expected_commit.lower(),
        "expectedPreviousCommit": previous,
        "branch": args.branch,
        "lenovoCheckout": str(Path(args.lenovo_checkout)),
        "lenovoWorktree": str(Path(args.lenovo_worktree)),
        "reconcileOnly": False,
        "authorized": authorized,
    }


def publish_existing_request(args) -> dict:
    """Standalone publication of a branch that already exists on Lenovo."""

    for name in ("expected_base", "expected_commit"):
        if not SHA_RE.match((getattr(args, name) or "").lower()):
            raise Stop("usage", code=2, detail=f"--{name.replace('_', '-')} must be 40 hex")
    if not TOKEN_RE.match(args.token or ""):
        raise Stop("usage", code=2, detail="--token must be 16 lowercase hex")
    for label, value in (("--branch", args.branch), ("--target-branch", args.target_branch)):
        if not BRANCH_RE.match(value or "") or ".." in (value or "") or value.startswith("-"):
            raise Stop("usage", code=2, detail=f"{label} is not a safe branch name")
    if not args.host or any(char in args.host for char in "/\\@"):
        raise Stop("usage", code=2, detail="--host must be the bare GitLab hostname")
    if not args.project or "/" not in args.project:
        raise Stop("usage", code=2, detail="--project must be group/project")
    authorized = [item.strip() for item in (args.authorize or "").split(",") if item.strip()]
    unknown = sorted(set(authorized) - set(AUTHORIZABLE))
    if unknown:
        raise Stop("usage", code=2, detail=f"unknown authorization: {', '.join(unknown)}")
    if "push" not in authorized or "mr" not in authorized:
        raise Stop("usage", code=2, detail="publish-existing needs push and mr in --authorize")
    if (args.state or "draft") not in ("draft", "ready"):
        raise Stop("usage", code=2, detail="--state must be draft or ready")
    if args.state == "ready" and "undraft" not in authorized:
        raise Stop("usage", code=2, detail="--state ready needs undraft in --authorize")
    shared = Path(args.shared)
    description = (args.description_file or "").strip() or None
    if description is not None and not inside(Path(description), shared):
        raise Stop("usage", code=2, detail="--description-file must live on the shared LAN path")
    require_absolute_lenovo_paths(
        ("--lenovo-checkout", args.lenovo_checkout),
        ("--lenovo-worktree", args.lenovo_worktree),
    )
    return {
        "schema": PUBLISH_EXISTING_REQUEST_SCHEMA,
        "token": args.token,
        "shared": str(shared),
        "manifest": "",
        "bundle": "",
        "expectedBaseSha": args.expected_base.lower(),
        "expectedCommit": args.expected_commit.lower(),
        "expectedPreviousCommit": None,
        "branch": args.branch,
        "targetBranch": args.target_branch,
        "host": args.host,
        "project": args.project,
        "changeType": args.change_type or "",
        "jiraKey": args.jira_key or "",
        "subject": (args.subject or "").strip(),
        "descriptionFile": str(Path(description)) if description else "",
        "state": args.state or "draft",
        "lenovoCheckout": str(Path(args.lenovo_checkout)),
        "lenovoWorktree": str(Path(args.lenovo_worktree)),
        "reconcileOnly": False,
        "authorized": authorized,
    }


__all__ = ["publish_existing_request", "transfer_request"]
