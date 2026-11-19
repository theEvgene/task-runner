#!/usr/bin/env python3
"""Dell side: receive, verify, and import the first verified copy exactly once.

The first copy is the one case with no Lenovo checkout: Lenovo observes the
explicitly named remote branch into an isolated temp bare repo and ships one
hashed git bundle over the owner-provided LAN share. Its SHA-256, byte count,
tip commit, remote URL, and branch travel inside the MAC-verified sealed
result, so a bundle swapped on the share cannot be imported as this job's
copy. The copy is created once at ``--repo-out`` and is never moved, replaced,
or cleaned afterwards.

``--repo`` names an existing Dell checkout used only as a key-custody anchor
(never written); the key must be proven outside it, the queue, and the share.

Exit codes: ``0`` imported, ``1`` refused/mismatch, ``2`` usage, ``3`` unknown
(the slot stays occupied; reconcile before any retry).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, FIRST_EXPORT_SCHEMA, FIRST_IMPORT_DRIVER_VERSION, Stop,
    common_file_digest, default_bridge_dir, fetch_verified, git, inside, load_bridge,
    load_json, manifest_digest, read_key, remote_has_credentials,
    resolve_bridge_file, result_failure, safe_name, sha256_file,
    verify_first_job, write_json_atomic, now_iso,
)
from dell_first_import_verify import (
    BRANCH_RE, DIGEST_RE, JOB_RE, import_once, project_of,
    verify_artifacts, verify_payload,
)

SCHEMA = "dell-first-import/1"

NEXT_ACTION = {
    "imported": "start local work from the imported copy",
    "base-job-failed": "read the carried exit code and stderr, fix on the Lenovo side, then a new job id",
    "artifact-mismatch": "keep the slot, re-fetch the same job id; never import bytes that do not match the sealed digest",
    "artifact-missing": "keep the slot, re-fetch the same job id; the shared path may be unavailable",
    "manifest-mismatch": "keep the slot and re-read the sealed result; a manifest that does not match its digest is not evidence",
    "return-file-mismatch": "keep the slot and re-read the sealed result; do not import on a failed cross-check",
    "repo-exists": "choose a new empty path; one first copy is imported once, never replaced",
    "source-mismatch": "check the remote URL and branch spelling; never import a copy that names another source",
    "credential-refused": "resubmit with a credential-free remote URL; secrets never travel the queue, the share, or a receipt",
    "driver-stale": "redeploy the current verified drivers on both machines and resubmit with a fresh token; a stale or unverified driver never yields a copy",
    "destination-is-file": "choose a new empty directory path; a file at the destination is left untouched",
    "destination-protected": "choose a destination outside the anchor checkout, the queue, and the shared folder",
    "receipt-protected": "write the receipt outside the anchor checkout, the queue, the shared folder, and the imported copy",
    "outcome-unknown": "keep the slot occupied, re-fetch the same job id, do not start a second import",
    "key-refused": "fix the key location or content outside the anchor checkout, the queue, and the shared folder",
    "request-malformed": "re-read the sealed result; a non-first-export payload proves nothing",
    "git-failed": "repair Git access and retry the import into a new empty path",
    "queue-job-mismatch": "re-read the sealed job; only the exact first-export command proves this import",
}

def parse(argv=None):
    parser = argparse.ArgumentParser(prog="dell_first_import.py", description=__doc__)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--shared", required=True,
                        help="absolute LAN share that received the bundle")
    parser.add_argument("--repo", required=True,
                        help="existing Dell checkout used only as the key-custody anchor")
    parser.add_argument("--repo-out", required=True, help="new empty path for the one first copy")
    parser.add_argument("--remote-url", required=True, help="explicit remote URL or path, spelled as in the request")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--expected-project", help="expected GitLab project path of the remote")
    parser.add_argument("--fetch-timeout", type=float, default=660.0)
    parser.add_argument("--poll", type=float, default=10.0)
    parser.add_argument("--provenance", default="fixture", choices=("fixture", "live"))
    parser.add_argument("--expected-lenovo-driver-sha256", required=True,
                        help="pinned SHA-256 of the trusted Lenovo driver copy; "
                             "the sealed driver digest must match it")
    parser.add_argument("--expected-dell-driver-sha256", required=True,
                        help="pinned SHA-256 of the trusted Dell driver file set; "
                             "the executing dell_first_import.py file set digest must match it")
    parser.add_argument("--expected-lenovo-common-sha256", required=True,
                        help="pinned SHA-256 of the trusted dell_lan_common.py copy; "
                             "the sealed Lenovo dependency digest must match it")
    parser.add_argument("--expected-dell-common-sha256", required=True,
                        help="pinned SHA-256 of the trusted dell_lan_common.py copy; "
                             "the executing Dell dependency digest must match it")
    parser.add_argument("--expected-bridge-sha256", required=True,
                        help="pinned SHA-256 of the trusted lan_bridge.py copy; "
                             "the configured bridge file digest must match it "
                             "before it is executed")
    parser.add_argument("--bridge-dir", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)

def usage(message: str) -> int:
    print(f"dell first import usage: {message}", file=sys.stderr)
    return 2

def blank(args) -> dict:
    return {
        "schema": SCHEMA,
        "provenance": args.provenance,
        "verdict": None,
        "reason": None,
        "detail": "",
        "jobId": args.job_id,
        "remoteUrl": args.remote_url,
        "refFull": None,
        "baseSha": None,
        "bundle": {"name": None, "sha256": None, "bytes": None, "verified": False},
        "manifest": {"digest": None, "match": False, "returnFileObserved": None},
        "driver": {"version": None, "sha256": None, "commonSha256": None,
                   "freshness": "unverified"},
        "dellDriver": {"version": None, "sha256": None, "commonSha256": None,
                       "freshness": "unverified"},
        "bridge": {"sha256": None, "freshness": "unverified"},
        "repo": {"path": str(args.repo_out), "created": False, "head": None},
        "origin": {"url": args.remote_url, "project": project_of(args.remote_url),
                   "projectMatches": None},
        "trustBoundary": (
            "Pinned digests compare the sealed Lenovo claim and the locally "
            "executing Dell bytes against operator-supplied identities. A "
            "driver patched to forge its own digest cannot be detected from "
            "inside itself: execution integrity rests on running both sides "
            "from a read-only verified checkout deployed over a trusted "
            "channel. Any missing or mismatched pin fails closed; no "
            "'unverified' success path exists."),
        "nextAction": None,
        "checkedAt": now_iso(),
    }

def verify_dell_driver(args, receipt: dict, bridge_dir=None) -> None:
    """Bind the actually configured Dell files to the operator's trusted pins.

    The pins are out-of-band trust inputs: SHA-256 of the current verified
    ``dell_first_import.py`` file set (the driver plus
    ``dell_first_import_verify.py``), ``dell_lan_common.py`` file set, and
    ``lan_bridge.py`` copies, computed by the operator from a trusted
    checkout. Every digest
    is taken from the resolved configured file *before* the bridge is
    executed: the bridge file is hashed at its resolved configured path and
    compared to the pin first, so a mismatched bridge never runs; the
    common digest comes from the import-system spec origin, never the
    mutable ``module.__file__`` attribute a module could rewrite to forge
    the check. Only an exact match on every pin reads as
    ``verified-pinned``. A missing, malformed, or mismatched pin, or an
    unreadable file, fails closed with ``driver-stale`` before any queue
    read or copy. A self-reported version string alone is never freshness.

    Trust boundary: this is still an in-process self-measurement. A driver
    patched to forge the digest it reports cannot be detected from inside
    itself, and the already-imported ``dell_lan_common`` executed at import
    load time before this check ran. Execution integrity therefore rests on
    running from a read-only verified checkout deployed over a trusted
    channel; the pins only fail closed against stale, mismatched, or
    unreadable deployments. There is no ``unverified`` success path. A file
    replaced between this hash and the later bridge import is likewise not
    detected from inside the process.
    """

    try:
        base = Path(__file__).resolve().parent
        names = ("dell_first_import.py", "dell_first_import_verify.py")
        combined = hashlib.sha256()
        for name in names:
            candidate = base / name
            if not candidate.is_file():
                raise OSError(f"missing driver file: {name}")
            combined.update(candidate.read_bytes())
        actual = combined.hexdigest().lower()
    except OSError:
        actual = ""
    if actual and not DIGEST_RE.match(actual):
        actual = ""
    actual_common = common_file_digest().lower()
    if actual_common and not DIGEST_RE.match(actual_common):
        actual_common = ""
    if bridge_dir is not None:
        try:
            bridge_path = resolve_bridge_file(Path(bridge_dir))
            actual_bridge = sha256_file(bridge_path).lower() if bridge_path.is_file() else ""
        except (OSError, ValueError):
            actual_bridge = ""
    else:
        actual_bridge = ""
    if actual_bridge and not DIGEST_RE.match(actual_bridge):
        actual_bridge = ""
    receipt["dellDriver"] = {"version": FIRST_IMPORT_DRIVER_VERSION,
                             "sha256": actual or None,
                             "commonSha256": actual_common or None,
                             "freshness": "unverified"}
    receipt["bridge"] = {"sha256": actual_bridge or None, "freshness": "unverified"}
    for label, pinned_raw in (
            ("--expected-dell-driver-sha256", args.expected_dell_driver_sha256),
            ("--expected-dell-common-sha256", args.expected_dell_common_sha256),
            ("--expected-bridge-sha256", args.expected_bridge_sha256)):
        pinned = (pinned_raw or "").strip().lower() or None
        if pinned is None or not DIGEST_RE.match(pinned):
            raise Stop("driver-stale",
                       detail=f"missing or malformed {label}; no trusted Dell identity")
    if not actual or actual != args.expected_dell_driver_sha256.strip().lower():
        raise Stop("driver-stale",
                   detail="executing Dell driver digest does not match the pinned digest")
    if not actual_common or actual_common != args.expected_dell_common_sha256.strip().lower():
        raise Stop("driver-stale",
                   detail="executing Dell dependency digest does not match the pinned digest")
    if not actual_bridge or actual_bridge != args.expected_bridge_sha256.strip().lower():
        raise Stop("driver-stale",
                   detail="configured bridge file digest does not match the pinned digest")
    receipt["dellDriver"]["freshness"] = "verified-pinned"
    # Bridge freshness stays "unverified" until the caller successfully
    # imports the already-verified file: the pin match above only proves
    # the bytes on disk, not that the import worked.

def main(argv=None) -> int:
    args = parse(argv)
    if not JOB_RE.match(args.job_id or ""):
        return usage("--job-id must be 32 lowercase hex")
    if not BRANCH_RE.match(args.branch or "") or args.branch.startswith("-") or ".." in args.branch:
        return usage("--branch is not a safe name")
    if args.fetch_timeout <= 0 or args.poll <= 0:
        return usage("--fetch-timeout and --poll must be positive")
    for label in ("expected_lenovo_driver_sha256", "expected_dell_driver_sha256",
                  "expected_lenovo_common_sha256", "expected_dell_common_sha256",
                  "expected_bridge_sha256"):
        value = (getattr(args, label) or "").strip().lower()
        if not DIGEST_RE.match(value):
            return usage(f"--{label.replace('_', '-')} must be 64 hex")
    if remote_has_credentials(args.remote_url):
        return usage("--remote-url must not carry userinfo credentials")
    for name in ("queue", "repo", "shared", "repo_out", "receipt"):
        if not Path(getattr(args, name)).is_absolute():
            return usage(f"--{name.replace('_', '-')} must be an absolute path")
    repo_out = Path(args.repo_out)
    receipt_path = Path(args.receipt)
    anchor = Path(args.repo)
    queue = Path(args.queue)
    shared = Path(args.shared)
    # No output may mutate the trust anchors: the receipt must live outside
    # the imported copy, the anchor checkout, the queue, and the share, and
    # the imported copy must live outside the anchor, the queue, and the
    # share (in both directions, so neither root nests inside the other).
    protected = (("anchor checkout", anchor), ("queue", queue),
                 ("shared folder", shared))
    if inside(receipt_path, repo_out) or inside(repo_out, receipt_path):
        return usage("--receipt must stay outside the imported copy")
    for label, root in protected:
        if inside(receipt_path, root) or inside(root, receipt_path):
            return usage(f"--receipt must stay outside the {label}")
        if inside(repo_out, root) or inside(root, repo_out):
            return usage(f"--repo-out must stay outside the {label}")
    if not anchor.is_dir():
        return usage("--repo must be an existing Dell checkout (custody anchor only)")

    receipt = blank(args)
    code = 0
    try:
        bridge_dir = args.bridge_dir or default_bridge_dir()
        verify_dell_driver(args, receipt, bridge_dir)
        bridge = load_bridge(bridge_dir)
        receipt["bridge"]["freshness"] = "verified-pinned"
        repos = [repo_out, anchor]
        key = read_key(bridge, queue, Path(args.key_file), args.key_format,
                       repos=repos, shared=shared)
        job = verify_first_job(bridge, queue, key, args.job_id)
        payload = fetch_verified(bridge, queue, key, args.job_id,
                                 args.fetch_timeout, args.poll)
        exported = verify_payload(payload, args, receipt, job)
        bundle = verify_artifacts(exported, args, receipt, payload)
        if args.expected_project:
            receipt["origin"]["projectMatches"] = receipt["origin"]["project"] == args.expected_project
            if not receipt["origin"]["projectMatches"]:
                raise Stop("artifact-mismatch",
                           detail=f"no remote for project {args.expected_project}")
        import_once(bundle, exported, args, receipt)
        receipt["verdict"] = "imported"
        receipt["reason"] = "imported"
    except Stop as stop:
        receipt["verdict"] = stop.verdict
        receipt["reason"] = stop.reason
        receipt["detail"] = stop.detail
        code = stop.code
    except OSError as error:
        receipt["verdict"] = "mismatch"
        receipt["reason"] = "git-failed"
        receipt["detail"] = str(error)[:DETAIL_MAX]
        code = 1

    receipt["nextAction"] = NEXT_ACTION.get(receipt["reason"], "stop and reconcile before any retry")
    text = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    try:
        write_json_atomic(args.receipt, receipt)
    except OSError as error:
        print(f"dell first import {receipt['verdict']} : receipt unwritable: {error}", file=sys.stderr)
        print(text)
        return 3
    print(text)
    print(f"dell first import {receipt['verdict']} : {receipt['reason']}", file=sys.stderr)
    return code

if __name__ == "__main__":
    raise SystemExit(main())
