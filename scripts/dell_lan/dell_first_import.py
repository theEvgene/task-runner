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
import base64
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, FIRST_EXPORT_SCHEMA, FIRST_IMPORT_DRIVER_VERSION, Stop,
    default_bridge_dir, fetch_verified, git, inside, load_bridge, load_json,
    manifest_digest, read_key, result_failure, safe_name, sha256_file,
    verify_first_job, write_json_atomic, now_iso,
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
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
    "driver-stale": "redeploy the current verified drivers on Lenovo and resubmit with a fresh token; a stale driver never yields a copy",
    "destination-is-file": "choose a new empty directory path; a file at the destination is left untouched",
    "outcome-unknown": "keep the slot occupied, re-fetch the same job id, do not start a second import",
    "key-refused": "fix the key location or content outside the anchor checkout, the queue, and the shared folder",
    "request-malformed": "re-read the sealed result; a non-first-export payload proves nothing",
    "git-failed": "repair Git access and retry the import into a new empty path",
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
    parser.add_argument("--expected-lenovo-driver-sha256", default=None,
                        help="pinned SHA-256 of the trusted Lenovo driver copy; "
                             "when named, the sealed driver digest must match it")
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
        "driver": {"version": None, "sha256": None, "freshness": "unverified"},
        "repo": {"path": str(args.repo_out), "created": False, "head": None},
        "origin": {"url": args.remote_url, "project": project_of(args.remote_url),
                   "projectMatches": None},
        "nextAction": None,
        "checkedAt": now_iso(),
    }


def project_of(url: str | None) -> str | None:
    """The GitLab project path named by one remote URL, or None.

    A local filesystem path names no GitLab project and returns None.
    """

    if not url:
        return None
    candidate = str(url).strip()
    if candidate.startswith("git@"):
        host, _, path = candidate[4:].partition(":")
        if not host or "/" not in path:
            return None
        return path[:-4] if path.endswith(".git") else path
    if "://" not in candidate:
        return None
    parsed = urllib.parse.urlsplit(candidate)
    path = parsed.path.strip("/")
    if not parsed.hostname or "/" not in path:
        return None
    return path[:-4] if path.endswith(".git") else path


def verify_payload(payload: dict, args, receipt: dict) -> dict:
    failure = result_failure(payload)
    if failure is not None:
        raise Stop("base-job-failed", verdict="rejected",
                   detail=json.dumps(failure)[:DETAIL_MAX])
    try:
        exported = json.loads(str(payload.get("stdout") or ""))
    except json.JSONDecodeError as error:
        raise Stop("request-malformed", detail=str(error)[:DETAIL_MAX])
    if not isinstance(exported, dict) or exported.get("schema") != FIRST_EXPORT_SCHEMA:
        raise Stop("request-malformed", detail=f"expected {FIRST_EXPORT_SCHEMA}")
    if exported.get("outcome") not in (None, "ok"):
        raise Stop("base-job-failed", verdict="rejected",
                   detail=str(exported.get("reason"))[:DETAIL_MAX])
    token = str(exported.get("token") or "")
    tip = str(exported.get("tipSha") or "").lower()
    digest = str(exported.get("sha256") or "").lower()
    size = exported.get("bytes")
    if not TOKEN_RE.match(token):
        raise Stop("request-malformed", detail="token is not factual")
    if not SHA_RE.match(tip) or not DIGEST_RE.match(digest) or isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise Stop("request-malformed", detail="tipSha/sha256/bytes are not factual")
    for name in ("bundleName", "manifestName"):
        if not safe_name(str(exported.get(name) or "")):
            raise Stop("request-malformed", detail=f"{name} must be one contained file name")
    if (exported.get("bundleName") != f"dell-first-{token}.bundle"
            or exported.get("manifestName") != f"dell-first-{token}.manifest.json"):
        raise Stop("request-malformed", detail="sealed artifacts do not match the token")
    if str(exported.get("refFull") or "") != f"refs/heads/{args.branch}":
        raise Stop("source-mismatch", detail=f"sealed branch {exported.get('refFull')} != refs/heads/{args.branch}")
    if str(exported.get("remoteUrl") or "") != args.remote_url:
        raise Stop("source-mismatch", detail="sealed remote URL differs from --remote-url")
    if exported.get("driverVersion") != FIRST_IMPORT_DRIVER_VERSION:
        raise Stop("driver-stale", detail=(
            f"sealed driver version {exported.get('driverVersion')} "
            f"!= {FIRST_IMPORT_DRIVER_VERSION}"))
    driver_sha = str(exported.get("driverSha256") or "").lower()
    if driver_sha and not DIGEST_RE.match(driver_sha):
        raise Stop("request-malformed", detail="sealed driverSha256 is not 64 hex")
    receipt["driver"] = {"version": exported["driverVersion"], "sha256": driver_sha or None,
                         "freshness": "unverified"}
    pinned = (args.expected_lenovo_driver_sha256 or "").strip().lower() or None
    if pinned is not None and not DIGEST_RE.match(pinned):
        raise Stop("request-malformed", detail="--expected-lenovo-driver-sha256 must be 64 hex")
    if pinned is not None:
        if not driver_sha or driver_sha != pinned:
            raise Stop("driver-stale", detail="sealed driver digest does not match the pinned digest")
        receipt["driver"]["freshness"] = "verified-pinned"
    receipt["refFull"] = exported["refFull"]
    receipt["baseSha"] = tip
    receipt["bundle"] = {"name": exported["bundleName"], "sha256": digest,
                         "bytes": size, "verified": False}
    return exported


def verify_artifacts(exported: dict, args, receipt: dict, payload: dict) -> Path:
    shared = Path(args.shared)
    bundle = shared / exported["bundleName"]
    if not bundle.is_file():
        raise Stop("artifact-missing", detail=str(bundle)[:DETAIL_MAX])
    if bundle.stat().st_size != exported["bytes"]:
        raise Stop("artifact-mismatch", detail=f"{bundle.stat().st_size} != {exported['bytes']}")
    if sha256_file(bundle) != exported["sha256"]:
        raise Stop("artifact-mismatch", detail="bundle SHA-256 does not match the sealed result")
    receipt["bundle"]["verified"] = True

    manifest_path = shared / exported["manifestName"]
    manifest = load_json(manifest_path, "manifest-mismatch")
    if manifest.get("version") != "git-bundle-manifest/1":
        raise Stop("manifest-mismatch", detail="not a git-bundle-manifest/1")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise Stop("manifest-mismatch", detail="expected exactly one bundle entry")
    entry = files[0] if isinstance(files[0], dict) else {}
    if (entry.get("path") != exported["bundleName"]
            or str(entry.get("sha256") or "").lower() != exported["sha256"]
            or entry.get("bytes") != exported["bytes"]):
        raise Stop("manifest-mismatch", detail="manifest entry does not match the sealed result")
    if manifest_digest(files) != manifest.get("manifestDigest"):
        raise Stop("manifest-mismatch", detail="manifestDigest does not cover the entry")
    if manifest.get("manifestDigest") != exported.get("manifestDigest"):
        raise Stop("manifest-mismatch", detail="sealed manifestDigest differs from the share")
    receipt["manifest"]["digest"] = manifest["manifestDigest"]
    receipt["manifest"]["match"] = True

    receipt["manifest"]["returnFileObserved"] = check_return_file(
        payload, exported["manifestName"], manifest)
    return bundle


def check_return_file(payload: dict, manifest_name: str, manifest: dict) -> bool | None:
    """Cross-check the declared return file when the worker shape is recognisable.

    The worker owns the ``files`` mapping shape and this repository does not
    define it, so an absent or unrecognisable entry is reported as ``None``
    instead of being treated as a failure. A recognisable entry that disagrees
    with the sealed manifest is a hard stop.
    """

    files = payload.get("files")
    if not isinstance(files, dict):
        return None
    for key, value in files.items():
        if Path(str(key)).name != manifest_name:
            continue
        try:
            data = base64.b64decode(str(value), validate=True)
            returned = json.loads(data.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(returned, dict):
            return None
        if returned.get("manifestDigest") != manifest.get("manifestDigest"):
            raise Stop("return-file-mismatch", detail="returned manifest digest differs")
        return True
    return None


def import_once(bundle: Path, exported: dict, args, receipt: dict) -> None:
    target = Path(args.repo_out)
    if os.path.lexists(str(target)) and not target.is_dir():
        raise Stop("destination-is-file", detail=f"destination exists and is not a directory: {target}"[:DETAIL_MAX])
    if target.is_symlink():
        raise Stop("destination-is-file", detail=f"destination is a link: {target}"[:DETAIL_MAX])
    if target.is_dir() and any(target.iterdir()):
        raise Stop("repo-exists", detail=str(target)[:DETAIL_MAX])
    target.mkdir(parents=True, exist_ok=True)
    receipt["repo"]["created"] = True
    git(target, "init", "-q")
    git(target, "bundle", "verify", str(bundle))
    branch = args.branch
    git(target, "fetch", "-q", str(bundle), f"{exported['refFull']}:refs/heads/{branch}")
    head = git(target, "rev-parse", f"refs/heads/{branch}").lower()
    if head != exported["tipSha"]:
        raise Stop("artifact-mismatch", detail=f"imported tip {head} != {exported['tipSha']}")
    git(target, "checkout", "-q", "-B", branch, head)
    observed = git(target, "rev-parse", "--verify", "HEAD").lower()
    if observed != exported["tipSha"]:
        raise Stop("artifact-mismatch", detail=f"HEAD {observed} != {exported['tipSha']}")
    receipt["repo"]["head"] = observed


def main(argv=None) -> int:
    args = parse(argv)
    if not JOB_RE.match(args.job_id or ""):
        return usage("--job-id must be 32 lowercase hex")
    if not BRANCH_RE.match(args.branch or "") or args.branch.startswith("-") or ".." in args.branch:
        return usage("--branch is not a safe name")
    if args.fetch_timeout <= 0 or args.poll <= 0:
        return usage("--fetch-timeout and --poll must be positive")
    if args.expected_lenovo_driver_sha256 is not None and not DIGEST_RE.match(
            args.expected_lenovo_driver_sha256.strip().lower()):
        return usage("--expected-lenovo-driver-sha256 must be 64 hex")
    repo_out = Path(args.repo_out)
    if inside(args.receipt, repo_out):
        return usage("--receipt must stay outside the imported copy")
    for name in ("queue", "repo", "shared"):
        if not Path(getattr(args, name)).is_absolute():
            return usage(f"--{name} must be an absolute path: the key is proven "
                         "outside it")
    if not Path(args.repo).is_dir():
        return usage("--repo must be an existing Dell checkout (custody anchor only)")

    receipt = blank(args)
    code = 0
    try:
        bridge = load_bridge(args.bridge_dir or default_bridge_dir())
        queue = Path(args.queue)
        repos = [repo_out, Path(args.repo)]
        key = read_key(bridge, queue, Path(args.key_file), args.key_format,
                       repos=repos, shared=Path(args.shared))
        verify_first_job(bridge, queue, key, args.job_id)
        payload = fetch_verified(bridge, queue, key, args.job_id,
                                 args.fetch_timeout, args.poll)
        exported = verify_payload(payload, args, receipt)
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
