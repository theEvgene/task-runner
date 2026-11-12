#!/usr/bin/env python3
"""Dell side: receive, verify, and import the Lenovo base copy exactly once.

The base bundle travels over the owner-provided LAN share that already hosts
the signed queue. Its SHA-256, byte count, and tip commit travel inside the
MAC-verified sealed result, so a bundle swapped on the share cannot be imported
as this job's base. The copy is created once at ``--repo-out`` and never moved,
replaced, or cleaned afterwards.

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
    BASE_EXPORT_SCHEMA, DETAIL_MAX, Stop, default_bridge_dir, fetch_verified, git,
    inside, load_bridge, load_json, manifest_digest, read_key,
    result_failure, safe_name, sha256_file, verify_job,
    write_json_atomic, now_iso,
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
SCHEMA = "dell-base-import/1"
NEXT_ACTION = {
    "imported": "cut the task worktree from the imported base and start local work",
    "base-job-failed": "read the carried exit code and stderr, fix on the Lenovo side, then a new job id",
    "artifact-mismatch": "keep the slot, re-fetch the same job id; never import bytes that do not match the sealed digest",
    "artifact-missing": "keep the slot, re-fetch the same job id; the shared path may be unavailable",
    "manifest-mismatch": "keep the slot and re-read the sealed result; a manifest that does not match its digest is not evidence",
    "return-file-mismatch": "keep the slot and re-read the sealed result; do not import on a failed cross-check",
    "repo-exists": "choose a new empty path; one source copy is imported once, never replaced",
    "outcome-unknown": "keep the slot occupied, re-fetch the same job id, do not start a second import",
    "key-refused": "fix the key location or content outside the checkout, the queue, and the shared folder",
    "request-malformed": "re-read the sealed result; a non-base-export payload proves nothing",
    "git-failed": "repair Git access and retry the import into a new empty path",
}


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="dell_base_import.py", description=__doc__)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--shared", required=True,
                        help="absolute LAN share that received the bundle")
    parser.add_argument("--repo", required=True,
                        help="absolute Dell checkout that must not hold the key")
    parser.add_argument("--repo-out", required=True, help="new empty path for the one source copy")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--expected-project", help="expected GitLab project path of the Lenovo origin")
    parser.add_argument("--manifest-out", type=Path, help="keep the verified manifest outside the copy")
    parser.add_argument("--fetch-timeout", type=float, default=660.0)
    parser.add_argument("--poll", type=float, default=10.0)
    parser.add_argument("--provenance", default="fixture", choices=("fixture", "live"))
    parser.add_argument("--bridge-dir", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def usage(message: str) -> int:
    print(f"dell base import usage: {message}", file=sys.stderr)
    return 2


def blank(args) -> dict:
    return {
        "schema": SCHEMA,
        "provenance": args.provenance,
        "verdict": None,
        "reason": None,
        "detail": "",
        "jobId": args.job_id,
        "baseSha": None,
        "refFull": None,
        "bundle": {"name": None, "sha256": None, "bytes": None, "verified": False},
        "manifest": {"digest": None, "match": False, "returnFileObserved": None},
        "repo": {"path": str(args.repo_out), "created": False, "head": None},
        "origin": {"url": None, "projects": [], "projectMatches": None},
        "nextAction": None,
        "checkedAt": now_iso(),
    }


def verify_payload(payload: dict, args, receipt: dict) -> dict:
    failure = result_failure(payload)
    if failure is not None:
        raise Stop("base-job-failed", verdict="rejected",
                   detail=json.dumps(failure)[:DETAIL_MAX])
    try:
        exported = json.loads(str(payload.get("stdout") or ""))
    except json.JSONDecodeError as error:
        raise Stop("request-malformed", detail=str(error)[:DETAIL_MAX])
    if not isinstance(exported, dict) or exported.get("schema") != BASE_EXPORT_SCHEMA:
        raise Stop("request-malformed", detail=f"expected {BASE_EXPORT_SCHEMA}")
    if exported.get("outcome") not in (None, "ok"):
        raise Stop("base-job-failed", verdict="rejected",
                   detail=str(exported.get("reason"))[:DETAIL_MAX])
    tip = str(exported.get("tipSha") or "").lower()
    digest = str(exported.get("sha256") or "").lower()
    size = exported.get("bytes")
    if not SHA_RE.match(tip) or not DIGEST_RE.match(digest) or isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise Stop("request-malformed", detail="tipSha/sha256/bytes are not factual")
    for name in ("bundleName", "manifestName"):
        if not safe_name(str(exported.get(name) or "")):
            raise Stop("request-malformed", detail=f"{name} must be one contained file name")
    if not str(exported.get("refFull") or "").startswith("refs/heads/"):
        raise Stop("request-malformed", detail="refFull must be refs/heads/<branch>")
    receipt["baseSha"] = tip
    receipt["refFull"] = exported["refFull"]
    receipt["bundle"] = {"name": exported["bundleName"], "sha256": digest,
                         "bytes": size, "verified": False}
    remotes = exported.get("remotes") if isinstance(exported.get("remotes"), dict) else {}
    receipt["origin"]["url"] = remotes.get("origin")
    receipt["origin"]["projects"] = sorted({
        project for project in (project_of(str(url)) for url in remotes.values())
        if project
    })
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
    """Cross-check the declared return file when the worker's shape is recognisable.

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
    """Create the one base copy exclusively; never overwrite or follow links.

    ANY pre-existing destination -- including an empty real directory -- is
    refused (``repo-exists``) and left untouched. The final path component is
    claimed with an exclusive ``mkdir`` so a concurrent winner loses with
    ``repo-exists`` instead of replacing foreign bytes.
    """

    target = Path(args.repo_out)
    if os.path.lexists(str(target)):
        raise Stop("repo-exists", detail=str(target)[:DETAIL_MAX])
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Stop("git-failed", detail=str(error)[:DETAIL_MAX])
    try:
        os.mkdir(str(target))
    except FileExistsError:
        raise Stop("repo-exists", detail=str(target)[:DETAIL_MAX])
    except OSError as error:
        raise Stop("git-failed", detail=str(error)[:DETAIL_MAX])
    if target.is_symlink() or os.path.islink(str(target)) or not target.is_dir():
        raise Stop("git-failed", detail=f"destination claim failed: {target}"[:DETAIL_MAX])
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


def project_of(url: str | None) -> str | None:
    """The GitLab project path named by one remote URL, or None.

    A local filesystem path is not a GitLab project and returns None.
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


def main(argv=None) -> int:
    args = parse(argv)
    if not JOB_RE.match(args.job_id or ""):
        return usage("--job-id must be 32 lowercase hex")
    if args.fetch_timeout <= 0 or args.poll <= 0:
        return usage("--fetch-timeout and --poll must be positive")
    repo_out = Path(args.repo_out)
    if inside(args.receipt, repo_out):
        return usage("--receipt must stay outside the imported copy")
    for name in ("queue", "repo", "shared"):
        if not Path(getattr(args, name)).is_absolute():
            return usage(f"--{name} must be an absolute path: the key is proven "
                         "outside it")

    receipt = blank(args)
    code = 0
    try:
        bridge = load_bridge(args.bridge_dir or default_bridge_dir())
        queue = Path(args.queue)
        repos = [repo_out, Path(args.repo)]
        key = read_key(bridge, queue, Path(args.key_file), args.key_format,
                       repos=repos, shared=Path(args.shared))
        verify_job(bridge, queue, key, args.job_id, "lenovo_base_export.py")
        payload = fetch_verified(bridge, queue, key, args.job_id,
                                 args.fetch_timeout, args.poll)
        exported = verify_payload(payload, args, receipt)
        bundle = verify_artifacts(exported, args, receipt, payload)
        if args.expected_project:
            receipt["origin"]["projectMatches"] = args.expected_project in receipt["origin"]["projects"]
            if not receipt["origin"]["projectMatches"]:
                raise Stop("artifact-mismatch",
                           detail=f"no remote for project {args.expected_project}")
        import_once(bundle, exported, args, receipt)
        if args.manifest_out:
            manifest_out = Path(args.manifest_out)
            if inside(manifest_out, repo_out):
                raise Stop("repo-exists", detail="manifest copy must stay outside the copy")
            source = Path(args.shared) / exported["manifestName"]
            manifest_out.parent.mkdir(parents=True, exist_ok=True)
            manifest_out.write_bytes(source.read_bytes())
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
        print(f"dell base import {receipt['verdict']} : receipt unwritable: {error}", file=sys.stderr)
        print(text)
        return 3
    print(text)
    print(f"dell base import {receipt['verdict']} : {receipt['reason']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
