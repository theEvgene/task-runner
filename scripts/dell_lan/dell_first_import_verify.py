"""Verified-payload checks for the Dell first verified import.

Split from ``dell_first_import.py`` so the driver fits the file token
guard. ``dell_first_import.py`` keeps the CLI, the driver-pin check, and
``main()``.
"""

from __future__ import annotations

import base64
import json
import os
import re
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

    The final path component is created with an exclusive ``mkdir``: ANY
    pre-existing destination -- including an empty real directory -- is
    refused (``repo-exists`` for a directory, ``destination-is-file`` for a
    file or link) and left untouched. A concurrent writer that wins the name
    loses the same way instead of replacing foreign bytes. Parents are
    created first, then the final component is claimed atomically; the claim
    is re-verified (not a link, an empty real directory) before any Git write.
    """

    target = Path(args.repo_out)
    if os.path.lexists(str(target)):
        if target.is_symlink() or os.path.islink(str(target)):
            raise Stop("destination-is-file",
                       detail=f"destination is a link: {target}"[:DETAIL_MAX])
        if not target.is_dir():
            raise Stop("destination-is-file",
                       detail=f"destination exists and is not a directory: {target}"[:DETAIL_MAX])
        raise Stop("repo-exists", detail=str(target)[:DETAIL_MAX])
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
