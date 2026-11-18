"""Build the ``publish`` request document.

Split from ``dell_request.py`` to stay under the repo file-token guard. The
route pins an absolute Lenovo checkout and task worktree; the writer turns
both into the claim set and the authentication block that
``dell_managed_submit`` re-checks before publication.
"""

from __future__ import annotations

import re
from pathlib import Path

from dell_lan_common import PUBLISH_REQUEST_SCHEMA, Stop, inside, safe_name, sha256_bytes

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


def publish_request(args) -> dict:
    for name in ("expected_base", "expected_commit"):
        if not SHA_RE.match((getattr(args, name) or "").lower()):
            raise Stop("usage", code=2, detail=f"--{name.replace('_', '-')} must be 40 hex")
    if not TOKEN_RE.match(args.token or ""):
        raise Stop("usage", code=2, detail="--token must be 16 lowercase hex")
    for label, value in (("--branch", args.branch), ("--target-branch", args.target_branch)):
        if not BRANCH_RE.match(value or "") or ".." in (value or "") or value.startswith("-"):
            raise Stop("usage", code=2, detail=f"{label} is not a safe branch name")
    if args.change_type and args.change_type not in ("feat", "fix"):
        raise Stop("usage", code=2, detail="--change-type must be feat or fix")
    if args.jira_key and not KEY_RE.match(args.jira_key):
        raise Stop("usage", code=2, detail="--jira-key must look like ABC-123")
    subject = (args.subject or "").strip()
    if subject and ("\n" in subject or "\r" in subject):
        raise Stop("usage", code=2, detail="--subject must be one line")
    if args.state not in ("draft", "ready"):
        raise Stop("usage", code=2, detail=(
            "--state must be draft or ready"))
    if args.state == "ready" and "undraft" not in [
            item.strip() for item in (args.authorize or "").split(",") if item.strip()]:
        raise Stop("usage", code=2, detail=(
            "--state ready needs undraft in --authorize: removing Draft is a "
            "separate consented action, never silent"))
    if not args.host or any(char in args.host for char in "/\\@"):
        raise Stop("usage", code=2, detail="--host must be the bare GitLab hostname")
    if not args.project or "/" not in args.project:
        raise Stop("usage", code=2, detail="--project must be group/project")
    authorized = [item.strip() for item in (args.authorize or "").split(",") if item.strip()]
    unknown = sorted(set(authorized) - set(AUTHORIZABLE))
    if unknown:
        raise Stop("usage", code=2, detail=f"unknown authorization: {', '.join(unknown)}")
    shared = Path(args.shared)
    for label, value in (("--manifest", args.manifest), ("--bundle", args.bundle)):
        if not inside(Path(value), shared):
            raise Stop("usage", code=2, detail=f"{label} must live on the shared LAN path")
    if not safe_name(Path(args.bundle).name):
        raise Stop("usage", code=2, detail="--bundle must name one contained file")
    description_path = Path(args.description_file) if args.description_file else None
    description_bytes = None
    description_sha = None
    if description_path is not None:
        if not inside(description_path, shared):
            raise Stop("usage", code=2,
                       detail="--description-file must live on the shared LAN path")
        if not description_path.is_file():
            raise Stop("usage", code=2, detail="--description-file does not exist")
        try:
            raw_description = description_path.read_bytes()
            raw_description.decode("utf-8")
        except OSError as error:
            raise Stop("usage", code=2, detail=f"--description-file is unreadable: {error}")
        except UnicodeDecodeError as error:
            raise Stop("usage", code=2,
                       detail=f"--description-file is not valid UTF-8: {error}")
        description_bytes = len(raw_description)
        description_sha = sha256_bytes(raw_description)
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
        "schema": PUBLISH_REQUEST_SCHEMA,
        "token": args.token,
        "shared": str(shared),
        "manifest": str(Path(args.manifest)),
        "bundle": str(Path(args.bundle)),
        "expectedBaseSha": args.expected_base.lower(),
        "expectedCommit": args.expected_commit.lower(),
        "expectedPreviousCommit": previous,
        "branch": args.branch,
        "targetBranch": args.target_branch,
        "host": args.host,
        "project": args.project,
        "changeType": args.change_type or "",
        "jiraKey": args.jira_key or "",
        "subject": subject,
        "descriptionFile": str(description_path) if description_path else "",
        "descriptionBytes": description_bytes,
        "descriptionSha256": description_sha,
        "state": args.state,
        "lenovoCheckout": str(Path(args.lenovo_checkout)),
        "lenovoWorktree": str(Path(args.lenovo_worktree)),
        "reconcileOnly": bool(getattr(args, "reconcile", False)),
        "authorized": authorized,
    }
