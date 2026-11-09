#!/usr/bin/env python3
"""Write one Dell-side request file, its command file, and the MAC-bound digest.

The signed LAN queue carries one command per job. Free text, paths, and branch
names therefore travel in a request file on the same owner-provided LAN share
that already hosts the queue, and only the request's SHA-256 travels inside the
HMAC-signed job payload. The Lenovo side re-hashes the file and refuses a
request that the job did not name.

Two requests exist, both in [the workflow](../../docs/dell-lan.md):

- ``base`` asks Lenovo for a git bundle of the published base ref;
- ``publish`` asks Lenovo to import the staged task bundle and run the existing
  ``gitlab-mr-flow`` publication flow for it.

This script writes files only. It never submits a job, never contacts Lenovo,
and never publishes anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dell_lan_common import (
    BASE_REQUEST_SCHEMA, DETAIL_MAX, PUBLISH_REQUEST_SCHEMA, Stop, command_line,
    inside, safe_name, sha256_bytes,
)

TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
AUTHORIZABLE = ("import", "push", "mr", "reconcile")
PREVIOUS_RE = re.compile(r"^[0-9a-f]{40}$")


def usage(message: str) -> int:
    print(f"dell request usage: {message}", file=sys.stderr)
    return 2


def refuse(message: str) -> int:
    print(f"dell request refused: {message}", file=sys.stderr)
    return 1


def write_request(args, request: dict, driver: str) -> int:
    shared = Path(args.shared)
    if not shared.is_dir():
        return refuse(f"shared directory does not exist: {shared}")
    if args.repo and inside(shared, Path(args.repo)):
        return refuse("shared directory must stay outside the checkout")
    out_dir = Path(args.out_dir) if args.out_dir else shared
    if not inside(out_dir, shared):
        return refuse("request file must live on the shared LAN path Lenovo can read")
    require_absolute_lenovo_paths(
        ("--lenovo-skill-dir", args.lenovo_skill_dir),
        ("--python", args.python),
    )
    driver_dir = getattr(args, "lenovo_driver_dir", None)
    if driver_dir:
        require_absolute_lenovo_paths(("--lenovo-driver-dir", driver_dir))
        driver_path = str(Path(driver_dir) / driver)
    else:
        driver_path = str(Path(args.lenovo_skill_dir) / "scripts" / driver)
    request_path = out_dir / args.request_name
    text = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_bytes(text.encode("utf-8"))
    digest = sha256_bytes(text.encode("utf-8"))
    command = command_line([
        args.python,
        driver_path,
        "--request", str(request_path),
        "--request-sha256", digest,
    ] + flow_args(args))
    command_file = Path(args.command_file)
    command_file.parent.mkdir(parents=True, exist_ok=True)
    command_file.write_bytes((command + "\n").encode("utf-8"))
    result = {
        "schema": "dell-lan-request/1",
        "kind": request["schema"],
        "requestPath": str(request_path),
        "requestSha256": digest,
        "commandFile": str(command_file),
        "command": command,
        "returnFile": str(out_dir / request["manifestName"]) if "manifestName" in request else None,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def require_absolute_lenovo_paths(*pairs: tuple[str, str]) -> None:
    """A path that crosses to Lenovo must be absolute there; a relative one is meaningless."""

    for label, value in pairs:
        if not value or not Path(value).is_absolute():
            raise Stop("usage", code=2, detail=(
                f"{label} must be the absolute path on the Lenovo machine"))


def flow_args(args) -> list[str]:
    """Extra Lenovo command arguments for the publish driver, if any were named."""

    flow_dir = getattr(args, "lenovo_flow_dir", None)
    if not flow_dir:
        return []
    require_absolute_lenovo_paths(("--lenovo-flow-dir", flow_dir))
    return ["--flow-dir", str(Path(flow_dir))]


def base_request(args) -> dict:
    if not TOKEN_RE.match(args.token or ""):
        raise Stop("usage", code=2, detail="--token must be 16 lowercase hex")
    if not args.ref_full.startswith("refs/heads/") or not BRANCH_RE.match(args.ref_full[len("refs/heads/"):]):
        raise Stop("usage", code=2, detail="--ref-full must be refs/heads/<branch>")
    require_absolute_lenovo_paths(("--lenovo-checkout", args.lenovo_checkout))
    token = args.token
    return {
        "schema": BASE_REQUEST_SCHEMA,
        "token": token,
        "shared": str(Path(args.shared)),
        "lenovoCheckout": str(Path(args.lenovo_checkout)),
        "refFull": args.ref_full,
        "bundleName": f"dell-base-{token}.bundle",
        "manifestName": f"dell-base-{token}.manifest.json",
    }


def publish_request(args) -> dict:
    for name in ("expected_base", "expected_commit"):
        if not SHA_RE.match((getattr(args, name) or "").lower()):
            raise Stop("usage", code=2, detail=f"--{name.replace('_', '-')} must be 40 hex")
    if not TOKEN_RE.match(args.token or ""):
        raise Stop("usage", code=2, detail="--token must be 16 lowercase hex")
    for label, value in (("--branch", args.branch), ("--target-branch", args.target_branch)):
        if not BRANCH_RE.match(value or "") or ".." in (value or "") or value.startswith("-"):
            raise Stop("usage", code=2, detail=f"{label} is not a safe branch name")
    if args.change_type not in ("feat", "fix"):
        raise Stop("usage", code=2, detail="--change-type must be feat or fix")
    if not KEY_RE.match(args.jira_key or ""):
        raise Stop("usage", code=2, detail="--jira-key must look like ABC-123")
    if not args.subject.strip() or "\n" in args.subject or "\r" in args.subject:
        raise Stop("usage", code=2, detail="--subject must be one non-empty line")
    if args.state != "draft":
        raise Stop("usage", code=2, detail=(
            "iteration 1 publishes a Draft merge request only: a Ready transition needs the "
            "existing mr_prepare.py target-SHA evidence, which this flow does not produce"))
    if not args.host or any(char in args.host for char in "/\\@"):
        raise Stop("usage", code=2, detail="--host must be the bare GitLab hostname")
    if not args.project or "/" not in args.project:
        raise Stop("usage", code=2, detail="--project must be group/project")
    authorized = [item.strip() for item in (args.authorize or "").split(",") if item.strip()]
    unknown = sorted(set(authorized) - set(AUTHORIZABLE))
    if unknown:
        raise Stop("usage", code=2, detail=f"unknown authorization: {', '.join(unknown)}")
    shared = Path(args.shared)
    for label, value in (("--manifest", args.manifest), ("--bundle", args.bundle),
                         ("--description-file", args.description_file)):
        if not inside(Path(value), shared):
            raise Stop("usage", code=2, detail=f"{label} must live on the shared LAN path")
    if not safe_name(Path(args.bundle).name):
        raise Stop("usage", code=2, detail="--bundle must name one contained file")
    if not Path(args.description_file).is_file():
        raise Stop("usage", code=2, detail="--description-file does not exist")
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
        "changeType": args.change_type,
        "jiraKey": args.jira_key,
        "subject": args.subject.strip(),
        "descriptionFile": str(Path(args.description_file)),
        "state": args.state,
        "lenovoCheckout": str(Path(args.lenovo_checkout)),
        "lenovoWorktree": str(Path(args.lenovo_worktree)),
        "reconcileOnly": False,
        "authorized": authorized,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dell_request.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(item):
        item.add_argument("--shared", required=True, help="owner-provided LAN share that holds the queue")
        item.add_argument("--token", required=True, help="16 lowercase hex request token")
        item.add_argument("--lenovo-skill-dir", required=True, help="absolute installed skill dir on Lenovo")
        item.add_argument("--lenovo-driver-dir", default=None,
                          help="absolute dir holding the Lenovo drivers on that machine "
                               "(default: <lenovo-skill-dir>/scripts)")
        item.add_argument("--python", required=True,
                          help="absolute interpreter path on the Lenovo machine")
        item.add_argument("--out-dir", help="where to write the request (default: the shared dir)")
        item.add_argument("--request-name", required=True, help="request file name on the shared dir")
        item.add_argument("--command-file", required=True, help="UTF-8 command file for the bridge")
        item.add_argument("--repo", help="Dell checkout that must not contain the shared dir")

    base = sub.add_parser("base", help="ask Lenovo for the published base bundle")
    common(base)
    base.add_argument("--lenovo-checkout", required=True, help="Lenovo checkout to export from")
    base.add_argument("--ref-full", required=True, help="refs/heads/<published base branch>")
    base.set_defaults(func=lambda args: write_request(args, base_request(args), "lenovo_base_export.py"))

    publish = sub.add_parser("publish", help="ask Lenovo to import and publish the task bundle")
    common(publish)
    publish.add_argument("--manifest", required=True)
    publish.add_argument("--bundle", required=True)
    publish.add_argument("--description-file", required=True)
    publish.add_argument("--expected-base", required=True)
    publish.add_argument("--expected-commit", required=True)
    publish.add_argument("--expected-previous", default=None,
                         help="prior task-branch SHA for a repeat transfer; "
                              "the driver moves the branch only when it still "
                              "points there and the new commit descends from it")
    publish.add_argument("--branch", required=True)
    publish.add_argument("--target-branch", required=True)
    publish.add_argument("--host", required=True)
    publish.add_argument("--project", required=True)
    publish.add_argument("--change-type", required=True)
    publish.add_argument("--jira-key", required=True)
    publish.add_argument("--subject", required=True)
    publish.add_argument("--state", default="draft")
    publish.add_argument("--authorize", required=True,
                         help="comma-separated: import,push,mr[,reconcile]")
    publish.add_argument("--lenovo-checkout", required=True)
    publish.add_argument("--lenovo-worktree", required=True)
    publish.add_argument("--lenovo-flow-dir", default=None,
                         help="absolute flow scripts dir on Lenovo holding the external "
                              "publication flow (default: the driver's own default)")
    publish.set_defaults(
        func=lambda args: write_request(args, publish_request(args), "lenovo_publish.py"))
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Stop as stop:
        if stop.code == 2:
            return usage(stop.detail or stop.reason)
        return refuse(stop.detail or stop.reason)
    except OSError as error:
        return refuse(str(error)[:DETAIL_MAX])


if __name__ == "__main__":
    raise SystemExit(main())
