#!/usr/bin/env python3
"""Write one Dell-side request file, its command file, and the MAC-bound digest.

The signed LAN queue carries one command per job. Free text, paths, and branch
names therefore travel in a request file on the same owner-provided LAN share
that already hosts the queue, and only the request's SHA-256 travels inside the
HMAC-signed job payload. The Lenovo side re-hashes the file and refuses a
request that the job did not name.

Five requests exist, all in [the workflow](../../docs/dell-lan.md):

- ``base`` asks Lenovo for a git bundle of the published base ref;
- ``first`` asks Lenovo for the first verified copy of an explicitly named
  remote branch when no Lenovo checkout exists yet (``docs/first-import.md``);
- ``publish`` asks Lenovo to import the staged task bundle and run the existing
  ``gitlab-mr-flow`` publication flow for it.
- ``transfer`` asks Lenovo to import the staged task bundle only (no push/MR;
  `docs/dell-lan-standalone.md`).
- ``publish-existing`` asks Lenovo to publish a branch that already exists
  there, reusing the same publication flow (no bundle, no second client;
  `docs/dell-lan-standalone.md`).

This script writes files only. It never submits a job, never contacts Lenovo,
and never publishes anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from shell_command import ShellDialectError, build_for_transport
from dell_request_common import (
    SingleDialect, add_common_arguments, add_reconcile_argument, flow_args,
    render_reconcile, require_absolute_lenovo_paths,
)

from dell_lan_common import (
    BASE_REQUEST_SCHEMA, DETAIL_MAX, FIRST_REQUEST_SCHEMA, Stop,
    expected_request_name, inside, remote_has_credentials, request_file_token,
    safe_name, sha256_bytes,
)
from dell_managed_claims import ClaimRefused, derived_claims
from dell_publish_request import publish_request
from dell_standalone_requests import publish_existing_request, transfer_request

TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def usage(message: str) -> int:
    print(f"dell request usage: {message}", file=sys.stderr)
    return 2


def refuse(message: str) -> int:
    print(f"dell request refused: {message}", file=sys.stderr)
    return 1


def expected_working_directory(args, request: dict) -> str | None:
    """The pinned Lenovo working directory this request asks the job to use.

    An explicit ``--expected-cwd`` wins; otherwise a route that pins a Lenovo
    checkout declares that checkout. A route without such a path presents no
    expectation: the job then stays ``unverified-target`` instead of a match.
    """

    declared = getattr(args, "expected_cwd", None)
    if declared:
        require_absolute_lenovo_paths(("--expected-cwd", declared))
        return str(Path(declared))
    pinned = request.get("lenovoCheckout")
    return str(pinned) if pinned else None


def write_request(args, request: dict, driver: str, dialect: str) -> int:
    shared = Path(args.shared)
    if not shared.is_dir():
        return refuse(f"shared directory does not exist: {shared}")
    if args.repo and inside(shared, Path(args.repo)):
        return refuse("shared directory must stay outside the checkout")
    token = str(request.get("token") or "")
    schema = str(request.get("schema") or "")
    if schema == FIRST_REQUEST_SCHEMA:
        expected_name = expected_request_name(schema, token)
        if str(args.request_name) != (expected_name or ""):
            raise Stop("usage", code=2, detail=(
                f"--request-name must be {expected_name}: the file name is bound "
                "to the internal token"))
        if request_file_token(str(args.request_name)) != token:
            raise Stop("usage", code=2, detail=(
                "--request-name token must equal the internal token"))
    elif not safe_name(str(args.request_name or "")):
        raise Stop("usage", code=2, detail="--request-name must be one contained file name")
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
    # Resolve the declared target and the managed-write footprint before the
    # first write: a rejected --expected-cwd or an unprovable footprint must
    # leave neither the request nor the command file.
    expected_cwd = expected_working_directory(args, request)
    try:
        claims = derived_claims(request)
    except ClaimRefused as error:
        raise Stop("usage", code=2, detail=str(error)) from error
    request_path = out_dir / args.request_name
    flow_dir = getattr(args, "lenovo_flow_dir", None)
    if flow_dir:
        require_absolute_lenovo_paths(("--lenovo-flow-dir", flow_dir))
        flow_dir = str(Path(flow_dir))
    # The authentication block below is inside the digest the signed job
    # carries: the submit derives the executed command from it, so a command
    # file or writer record cannot name another request, dialect, driver, or
    # working directory.
    request = dict(request)
    request["requestPath"] = str(request_path)
    return_file = (str(out_dir / request["manifestName"])
                   if "manifestName" in request else None)
    request["execution"] = {
        "python": str(args.python),
        "driverPath": driver_path,
        "dialect": dialect,
        "flowDir": flow_dir,
        "expectedCwd": expected_cwd,
        "returnFile": return_file,
    }
    text = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
    digest = sha256_bytes(text.encode("utf-8"))
    command_parts = [
        args.python,
        driver_path,
        "--request", str(request_path),
        "--request-sha256", digest,
    ] + flow_args(args)
    try:
        command = build_for_transport(command_parts, dialect)
        reconcile_command = render_reconcile(
            command_parts, driver, dialect, getattr(args, "reconcile", False))
    except ShellDialectError as error:
        raise Stop("usage", code=2, detail=str(error)) from error
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_bytes(text.encode("utf-8"))
    command_file = Path(args.command_file)
    command_file.parent.mkdir(parents=True, exist_ok=True)
    command_file.write_bytes(command.encode("utf-8"))
    result = {
        "schema": "dell-lan-request/1",
        "kind": request["schema"],
        "requestPath": str(request_path),
        "requestSha256": digest,
        "commandFile": str(command_file),
        "command": command,
        "reconcileCommand": reconcile_command,
        "dialect": dialect,
        "receiptScriptPath": driver_path,
        "expectedCwd": expected_cwd,
        "resourceClaims": claims,
        "readOnly": request.get("reconcileOnly") is True,
        "returnFile": return_file,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


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


def first_request(args) -> dict:
    """First verified copy: the remote is named explicitly, no Lenovo checkout.

    The bundle still travels over the owner-provided LAN share while only the
    request's SHA-256 travels inside the MAC-bound job. The Lenovo driver
    observes the actual remote tip into an isolated temp bare repo, so no
    checkout is read or written there.
    """

    if not TOKEN_RE.match(args.token or ""):
        raise Stop("usage", code=2, detail="--token must be 16 lowercase hex")
    remote = (args.remote_url or "").strip()
    if not remote or any(char in remote for char in ("\n", "\r", "\0")):
        raise Stop("usage", code=2, detail="--remote-url must be one non-empty line")
    if remote.startswith("-"):
        raise Stop("usage", code=2, detail="--remote-url must not start with '-'")
    if remote_has_credentials(remote):
        raise Stop("usage", code=2, detail="--remote-url must not carry userinfo credentials")
    if not args.ref_full.startswith("refs/heads/") or not BRANCH_RE.match(args.ref_full[len("refs/heads/"):]):
        raise Stop("usage", code=2, detail="--ref-full must be refs/heads/<branch>")
    branch = args.ref_full[len("refs/heads/"):]
    if branch.startswith("-") or ".." in branch:
        raise Stop("usage", code=2, detail="--ref-full must be refs/heads/<branch>")
    token = args.token
    return {
        "schema": FIRST_REQUEST_SCHEMA,
        "token": token,
        "shared": str(Path(args.shared)),
        "remoteUrl": remote,
        "refFull": args.ref_full,
        "bundleName": f"dell-first-{token}.bundle",
        "manifestName": f"dell-first-{token}.manifest.json",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dell_request.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    base = sub.add_parser("base", help="ask Lenovo for the published base bundle")
    add_common_arguments(base)
    base.add_argument("--lenovo-checkout", required=True, help="Lenovo checkout to export from")
    base.add_argument("--ref-full", required=True, help="refs/heads/<published base branch>")
    base.set_defaults(func=lambda args: write_request(
        args, base_request(args), "lenovo_base_export.py", "cmd"))

    first = sub.add_parser("first", help="ask Lenovo for the first verified copy of a remote branch")
    add_common_arguments(first)
    first.add_argument("--remote-url", required=True, help="explicit Git remote URL or path on Lenovo")
    first.add_argument("--ref-full", required=True, help="refs/heads/<branch> to import")
    first.set_defaults(func=lambda args: write_request(
        args, first_request(args), "lenovo_first_export.py", "cmd"))

    publish = sub.add_parser("publish", help="ask Lenovo to import and publish the task bundle")
    add_common_arguments(publish)
    publish.add_argument("--dialect", required=True, choices=("cmd", "powershell"),
                         action=SingleDialect,
                         help="shell that will execute the generated command file; never inferred")
    publish.add_argument("--manifest", required=True)
    publish.add_argument("--bundle", required=True)
    publish.add_argument("--description-file", default="",
                         help="required only when no open MR exists")
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
    publish.add_argument("--change-type", default="",
                         help="feat/fix; required only when no open MR exists")
    publish.add_argument("--jira-key", default="",
                         help="ABC-123; required only when no open MR exists")
    publish.add_argument("--subject", default="",
                         help="one-line title; required only when no open MR exists")
    publish.add_argument("--state", default="draft",
                           help="draft, or ready (Ready needs undraft in --authorize)")
    publish.add_argument("--authorize", required=True,
                         help="comma-separated: import,push,mr[,undraft][,reconcile] "
                              "(undraft consents to the remove_draft step of a ready request)")
    publish.add_argument("--lenovo-checkout", required=True)
    publish.add_argument("--lenovo-worktree", required=True)
    publish.add_argument("--lenovo-flow-dir", default=None,
                         help="absolute flow scripts dir on Lenovo holding the external "
                              "publication flow (default: the driver's own default)")
    add_reconcile_argument(publish)
    publish.set_defaults(
        func=lambda args: write_request(
            args, publish_request(args), "lenovo_publish.py", args.dialect))

    transfer = sub.add_parser("transfer", help="ask Lenovo to import the task bundle only")
    add_common_arguments(transfer)
    transfer.add_argument("--dialect", required=True, choices=("cmd", "powershell"),
                          action=SingleDialect,
                          help="shell that will execute the generated command file; never inferred")
    transfer.add_argument("--manifest", required=True)
    transfer.add_argument("--bundle", required=True)
    transfer.add_argument("--expected-base", required=True)
    transfer.add_argument("--expected-commit", required=True)
    transfer.add_argument("--expected-previous", default=None,
                          help="prior task-branch SHA for a repeat transfer")
    transfer.add_argument("--branch", required=True)
    transfer.add_argument("--authorize", default="import",
                          help="comma-separated: import[,reconcile] (never push/mr)")
    transfer.add_argument("--lenovo-checkout", required=True)
    transfer.add_argument("--lenovo-worktree", required=True)
    add_reconcile_argument(transfer)
    transfer.set_defaults(
        func=lambda args: write_request(
            args, transfer_request(args), "lenovo_transfer.py", args.dialect))

    existing = sub.add_parser("publish-existing",
                              help="ask Lenovo to publish a branch that already exists there")
    add_common_arguments(existing)
    existing.add_argument("--dialect", required=True, choices=("cmd", "powershell"),
                          action=SingleDialect,
                          help="shell that will execute the generated command file; never inferred")
    existing.add_argument("--expected-base", required=True)
    existing.add_argument("--expected-commit", required=True)
    existing.add_argument("--branch", required=True)
    existing.add_argument("--target-branch", required=True)
    existing.add_argument("--host", required=True)
    existing.add_argument("--project", required=True)
    existing.add_argument("--change-type", default="",
                          help="feat/fix; required only when no open MR exists")
    existing.add_argument("--jira-key", default="",
                          help="ABC-123; required only when no open MR exists")
    existing.add_argument("--subject", default="",
                          help="one-line title; required only when no open MR exists")
    existing.add_argument("--description-file", default="",
                          help="shared MR description; required only when no open MR exists")
    existing.add_argument("--state", default="draft",
                          help="draft, or ready (Ready needs undraft in --authorize)")
    existing.add_argument("--authorize", required=True,
                          help="comma-separated: push,mr[,undraft][,reconcile]")
    existing.add_argument("--lenovo-checkout", required=True)
    existing.add_argument("--lenovo-worktree", required=True)
    existing.add_argument("--lenovo-flow-dir", default=None,
                          help="absolute flow scripts dir on Lenovo holding the external "
                               "publication flow (default: the driver's own default)")
    add_reconcile_argument(existing)
    existing.set_defaults(
        func=lambda args: write_request(
            args, publish_existing_request(args), "lenovo_publish_existing.py", args.dialect))
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
