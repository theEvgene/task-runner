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
    common_file_digest, default_bridge_dir, fetch_verified, git, inside, load_bridge,
    load_json, manifest_digest, read_key, remote_has_credentials,
    resolve_bridge_file, result_failure, safe_name, sha256_file,
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
                        help="pinned SHA-256 of the trusted Dell driver copy; "
                             "the executing dell_first_import.py digest must match it")
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


def verify_dell_driver(args, receipt: dict, bridge_dir=None) -> None:
    """Bind the actually configured Dell files to the operator's trusted pins.

    The pins are out-of-band trust inputs: SHA-256 of the current verified
    ``dell_first_import.py``, ``dell_lan_common.py``, and ``lan_bridge.py``
    copies, computed by the operator from a trusted checkout. Every digest
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
        actual = sha256_file(Path(__file__).resolve()).lower()
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


def verify_payload(payload: dict, args, receipt: dict, job: dict | None = None) -> dict:
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
    if job is not None:
        request_token = job.get("_requestFileToken")
        if request_token is not None and request_token != token:
            raise Stop("request-malformed",
                       detail="sealed token does not match the request file the job named")
    sealed_remote = str(exported.get("remoteUrl") or "")
    if remote_has_credentials(sealed_remote):
        raise Stop("credential-refused",
                   detail="sealed remote URL carries userinfo credentials; refusing before any copy")
    if str(exported.get("refFull") or "") != f"refs/heads/{args.branch}":
        raise Stop("source-mismatch", detail=f"sealed branch {exported.get('refFull')} != refs/heads/{args.branch}")
    if sealed_remote != args.remote_url:
        raise Stop("source-mismatch", detail="sealed remote URL differs from --remote-url")
    if exported.get("driverVersion") != FIRST_IMPORT_DRIVER_VERSION:
        raise Stop("driver-stale", detail=(
            f"sealed driver version {exported.get('driverVersion')} "
            f"!= {FIRST_IMPORT_DRIVER_VERSION}"))
    driver_sha = str(exported.get("driverSha256") or "").lower()
    if driver_sha and not DIGEST_RE.match(driver_sha):
        raise Stop("request-malformed", detail="sealed driverSha256 is not 64 hex")
    driver_common = str(exported.get("driverCommonSha256") or "").lower()
    if driver_common and not DIGEST_RE.match(driver_common):
        raise Stop("request-malformed", detail="sealed driverCommonSha256 is not 64 hex")
    receipt["driver"] = {"version": exported["driverVersion"], "sha256": driver_sha or None,
                         "commonSha256": driver_common or None,
                         "freshness": "unverified"}
    pinned = (args.expected_lenovo_driver_sha256 or "").strip().lower() or None
    if pinned is None or not DIGEST_RE.match(pinned):
        raise Stop("driver-stale",
                   detail="missing or malformed --expected-lenovo-driver-sha256; "
                          "no trusted Lenovo identity")
    if not driver_sha or driver_sha != pinned:
        raise Stop("driver-stale", detail="sealed driver digest does not match the pinned digest")
    pinned_common = (args.expected_lenovo_common_sha256 or "").strip().lower() or None
    if pinned_common is None or not DIGEST_RE.match(pinned_common):
        raise Stop("driver-stale",
                   detail="missing or malformed --expected-lenovo-common-sha256; "
                          "no trusted Lenovo dependency identity")
    if not driver_common or driver_common != pinned_common:
        raise Stop("driver-stale",
                   detail="sealed Lenovo dependency digest does not match the pinned digest")
    receipt["driver"]["freshness"] = "verified-pinned"
    receipt["refFull"] = exported["refFull"]
    receipt["baseSha"] = tip
    receipt["bundle"] = {"name": exported["bundleName"], "sha256": digest,
                         "bytes": size, "verified": False}
    return exported


def verify_artifacts(exported: dict, args, receipt: dict, payload: dict) -> Path:
    shared = Path(args.shared)
    bundle = shared / exported["bundleName"]
    if bundle.is_symlink() or os.path.islink(str(bundle)):
        raise Stop("artifact-mismatch", detail="bundle path is a link, not a file")
    if not bundle.is_file():
        raise Stop("artifact-missing", detail=str(bundle)[:DETAIL_MAX])
    if bundle.stat().st_size != exported["bytes"]:
        raise Stop("artifact-mismatch", detail=f"{bundle.stat().st_size} != {exported['bytes']}")
    if sha256_file(bundle) != exported["sha256"]:
        raise Stop("artifact-mismatch", detail="bundle SHA-256 does not match the sealed result")
    receipt["bundle"]["verified"] = True

    manifest_path = shared / exported["manifestName"]
    if manifest_path.is_symlink() or os.path.islink(str(manifest_path)):
        raise Stop("manifest-mismatch", detail="manifest path is a link, not a file")
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
    """Create the one first copy exclusively; never overwrite or follow links.

    The final path component is created with an exclusive ``mkdir``: a
    concurrent writer that wins the name loses with ``repo-exists`` instead
    of replacing foreign bytes, and a pre-existing file or link is refused
    as ``destination-is-file`` without being touched. Parents are created
    first, then the final component is claimed atomically; the claim is
    re-verified (not a link, an empty real directory) before any Git write.
    """

    target = Path(args.repo_out)
    if os.path.lexists(str(target)):
        if target.is_symlink() or os.path.islink(str(target)):
            raise Stop("destination-is-file",
                       detail=f"destination is a link: {target}"[:DETAIL_MAX])
        if not target.is_dir():
            raise Stop("destination-is-file",
                       detail=f"destination exists and is not a directory: {target}"[:DETAIL_MAX])
        if any(target.iterdir()):
            raise Stop("repo-exists", detail=str(target)[:DETAIL_MAX])
        # Pre-existing empty real directory: still re-verify it is not a
        # link before writing into it.
        if target.is_symlink() or os.path.islink(str(target)):
            raise Stop("destination-is-file",
                       detail=f"destination is a link: {target}"[:DETAIL_MAX])
        receipt["repo"]["created"] = False
    else:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise Stop("destination-is-file", detail=str(error)[:DETAIL_MAX])
        try:
            os.mkdir(str(target))
        except FileExistsError:
            # A concurrent writer won the name between the check and the
            # claim: re-read the winner instead of replacing it.
            if target.is_symlink() or os.path.islink(str(target)):
                raise Stop("destination-is-file",
                           detail=f"destination is a link: {target}"[:DETAIL_MAX])
            if not target.is_dir():
                raise Stop("destination-is-file",
                           detail=f"destination exists and is not a directory: {target}"[:DETAIL_MAX])
            raise Stop("repo-exists", detail=str(target)[:DETAIL_MAX])
        except OSError as error:
            raise Stop("destination-is-file", detail=str(error)[:DETAIL_MAX])
        if target.is_symlink() or os.path.islink(str(target)) or not target.is_dir():
            raise Stop("destination-is-file",
                       detail=f"destination claim failed: {target}"[:DETAIL_MAX])
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
