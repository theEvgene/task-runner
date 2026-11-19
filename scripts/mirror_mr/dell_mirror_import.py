#!/usr/bin/env python3
"""Dell side: import an open GitLab MR into a private GitHub PR (issue #142).

Fetches the MAC-verified export result for one signed LAN job (an unknown
outcome keeps the slot and resumes the SAME id), verifies the bundle bytes,
digests, manifest, and Git object/ref identities, publishes the two refs to
the explicitly configured private GitHub mirror, creates or reconciles the
GitHub PR with the original direction, title, and source link, then reads
back visibility, refs, PR state/base/head, and the resulting change. Reuses
the signed bridge (`scripts/lan_bridge`) and the `dell_lan_common` trust
rules; GitHub goes through the `github_ops` seam (`--github-dir` names a
fixture double, otherwise the real `gh` CLI).

Re-running the same request (same `--state`) finds and verifies the
existing PR instead of duplicating it; an interrupted run resumes from the
persisted non-secret state. A conflicting same-name ref or PR stops the run.
No GitLab mutation, no merge, no force-push. Exit 0 mirrored, 1 refused, 2
usage, 3 unknown.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dell_lan"))

from mirror_mr_common import (DETAIL_MAX, EXPORT_SCHEMA, JOB_RE, RECEIPT_SCHEMA,
                              SHA_RE, STATE_SCHEMA, Stop, TOKEN_RE,
                              check_branch, check_export_identity,
                              load_mapping, parse_mr_url, resolve_mirror)
from dell_mirror_publish import (adopt_or_create, push_refs,
                                 read_remote_refs, reconcile_refs)  # noqa: E402
from dell_lan_common import (default_bridge_dir, fetch_verified, git_ok,
                             load_bridge, load_json, manifest_digest,
                             now_iso, read_key, result_failure, safe_name,
                             sha256_file,
                             write_json_atomic)  # noqa: E402

MIRROR_REQUEST_NAME_RE = re.compile(r"^mirror-request-([0-9a-f]{16})\.json$")

NEXT_ACTION = {
    "mirrored": "share the PR URL and receipt; no further action",
    "reconciled": "share the PR URL and receipt; no further action",
    "outcome-unknown": "re-run the same command: it resumes the same job id",
    "pr-conflict": "resolve the conflicting PR or ref by hand, then a new request",
    "target-not-fast-forward": "re-read the mirror target ref; a new request names current SHAs",
    "source-ref-conflict": "resolve the conflicting source ref by hand, then a new request",
    "destination-not-private": "make the mirror private, or remap to a private mirror",
    "evidence-gap": "do not treat the PR as equivalent; inspect the reported gap",
    "queue-job-mismatch": "re-read the sealed job; only the exact mirror-export command proves this import",
    "request-malformed": "re-read the sealed result; a payload whose token or artifacts ignore the job proves nothing",
    "worktree-checked-out": "check out another ref in the mirror clone before retrying",
}


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="dell_mirror_import.py", description=__doc__)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--shared", required=True)
    parser.add_argument("--mirror-dir", required=True,
                        help="local clone whose origin is the private GitHub mirror")
    parser.add_argument("--mapping-file", required=True)
    parser.add_argument("--mr-url", required=True)
    parser.add_argument("--state", required=True,
                        help="non-secret operation state for resume (outside the mirror)")
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--fetch-timeout", type=float, default=60.0)
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--provenance", default="fixture", choices=("fixture", "live"))
    parser.add_argument("--bridge-dir", type=Path, default=None)
    parser.add_argument("--github-dir", type=Path, default=None,
                        help="dir holding a github_ops.py double (default: real gh)")
    return parser.parse_args(argv)


def load_github(github_dir: Path | None):
    if github_dir is not None:
        path = Path(github_dir) / "github_ops.py"
        if not path.is_file():
            raise Stop("github-unavailable", detail=f"no github_ops.py in {github_dir}")
        sys.path.insert(0, str(Path(github_dir)))
    try:
        return importlib.import_module("github_ops")
    except Exception as error:
        raise Stop("github-unavailable", detail=str(error)[:DETAIL_MAX])


def blank_state(args) -> dict:
    return {"schema": STATE_SCHEMA, "mrUrl": args.mr_url,
            "mirrorRepo": None, "jobId": args.job_id, "token": None,
            "stage": "requested", "sourceSha": None, "targetSha": None,
            "sourceBranch": None, "targetBranch": None, "title": None,
            "description": None, "prUrl": None, "prNumber": None,
            "refsPushed": False, "updatedAt": now_iso()}


def save_state(path: Path, state: dict) -> None:
    state["updatedAt"] = now_iso()
    write_json_atomic(path, state)


def verify_mirror_job(bridge, queue: Path, key: bytes, job_id: str) -> dict:
    """Bind the sealed job to the exact mirror-export command (fail closed).

    Mirrors ``verify_first_job``: a substring marker is never enough
    (``echo lenovo_mr_export.py`` would pass it while executing nothing).
    The sealed command must parse to exactly ``<python> <driver> --request
    <request> --request-sha256 <digest>`` with an optional ``--flow-dir
    <dir>`` pair, where the driver basename is ``lenovo_mr_export.py``,
    every path is absolute, the request basename is
    ``mirror-request-<16hex>.json`` and the digest is 64 hex. The parsed
    request file token is stashed as ``_requestFileToken`` so the sealed
    payload token can be bound to the exact file the job named.
    """

    try:
        raw = bridge.read(str(queue / "jobs"), job_id)
    except bridge.JobNotFoundError:
        raise Stop("queue-job-missing", detail=job_id)
    except (bridge.BridgeError, OSError) as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    try:
        job = bridge.open_envelope(raw, key, "job", job_id)
    except bridge.ProtocolError as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    command = str(job.get("command") or "")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as error:
        raise Stop("queue-job-mismatch", detail=str(error)[:DETAIL_MAX])
    if len(argv) not in (6, 8):
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    python, driver, flag_request, request_path, flag_digest, digest = argv[:6]
    if not python or Path(driver).name != "lenovo_mr_export.py":
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if not Path(driver).is_absolute() or not Path(request_path).is_absolute():
        raise Stop("queue-job-mismatch", detail="driver and request paths must be absolute")
    if flag_request != "--request" or flag_digest != "--request-sha256":
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    match = MIRROR_REQUEST_NAME_RE.fullmatch(Path(request_path).name)
    if match is None:
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    if len(argv) == 8:
        if argv[6] != "--flow-dir" or not Path(argv[7]).is_absolute():
            raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])
    job["_requestFileToken"] = match.group(1)
    job["_requestDigest"] = digest.lower()
    job["_requestPath"] = request_path
    return job


def verify_payload(payload: dict, job: dict | None = None) -> dict:
    failure = result_failure(payload)
    if failure is not None:
        raise Stop("export-job-failed", detail=json.dumps(failure)[:DETAIL_MAX])
    try:
        exported = json.loads(str(payload.get("stdout") or ""))
    except json.JSONDecodeError as error:
        raise Stop("export-malformed", detail=str(error)[:DETAIL_MAX])
    if not isinstance(exported, dict) or exported.get("schema") != EXPORT_SCHEMA:
        raise Stop("export-malformed", detail=f"expected {EXPORT_SCHEMA}")
    if exported.get("outcome") not in (None, "ok"):
        raise Stop("export-job-failed", detail=str(exported.get("reason"))[:DETAIL_MAX])
    if str(exported.get("status") or "") != "opened":
        raise Stop("mr-not-open", detail=str(exported.get("status"))[:DETAIL_MAX])
    for name in ("sourceSha", "targetSha"):
        if not SHA_RE.match(str(exported.get(name) or "").lower()):
            raise Stop("export-malformed", detail=f"{name} is not a commit SHA")
    check_branch(str(exported.get("sourceBranch") or ""), "source branch")
    check_branch(str(exported.get("targetBranch") or ""), "target branch")
    for name in ("bundleName", "manifestName"):
        if not safe_name(str(exported.get(name) or "")):
            raise Stop("export-malformed", detail=f"{name} must be one contained file")
    digest = str(exported.get("sha256") or "").lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise Stop("export-malformed", detail="sha256 is not a digest")
    if not isinstance(exported.get("bytes"), int) or exported["bytes"] < 1:
        raise Stop("export-malformed", detail="bytes is not factual")
    token = str(exported.get("token") or "").lower()
    if not TOKEN_RE.match(token):
        raise Stop("request-malformed", detail="token is not factual")
    if exported.get("bundleName") != f"mirror-mr-{token}.bundle" \
            or exported.get("manifestName") != f"mirror-mr-{token}.manifest.json":
        raise Stop("request-malformed", detail="sealed artifacts do not match the token")
    if job is not None:
        request_token = job.get("_requestFileToken")
        if request_token is not None and request_token != token:
            raise Stop("request-malformed",
                       detail="sealed token does not match the request file the job named")
    return exported


def verify_artifacts(exported: dict, args, payload: dict) -> Path:
    shared = Path(args.shared)
    bundle = shared / exported["bundleName"]
    if not bundle.is_file():
        raise Stop("artifact-missing", detail=str(bundle)[:DETAIL_MAX])
    if bundle.stat().st_size != exported["bytes"]:
        raise Stop("artifact-mismatch", detail="bundle bytes differ from sealed result")
    if sha256_file(bundle) != exported["sha256"]:
        raise Stop("artifact-mismatch", detail="bundle SHA-256 differs from sealed result")
    manifest = load_json(shared / exported["manifestName"], "manifest-mismatch")
    if manifest.get("version") != "git-bundle-manifest/1":
        raise Stop("manifest-mismatch", detail="not a git-bundle-manifest/1")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise Stop("manifest-mismatch", detail="expected exactly one bundle entry")
    entry = files[0]
    if entry.get("path") != exported["bundleName"] \
            or str(entry.get("sha256") or "").lower() != exported["sha256"] \
            or entry.get("bytes") != exported["bytes"]:
        raise Stop("manifest-mismatch", detail="manifest entry differs from sealed result")
    if manifest_digest(files) != manifest.get("manifestDigest") \
            or manifest.get("manifestDigest") != exported.get("manifestDigest"):
        raise Stop("manifest-mismatch", detail="manifestDigest does not cover the entry")
    return bundle


def fetch_into_mirror(mirror: Path, bundle: Path, exported: dict) -> None:
    ok, _ = git_ok(mirror, "bundle", "verify", str(bundle))
    if not ok:
        raise Stop("bundle-unreadable", detail="bundle does not verify in the mirror")
    ok, listed = git_ok(mirror, "bundle", "list-heads", str(bundle))
    heads = [line.split()[0].lower() for line in listed.splitlines()
             if line.strip()] if ok else []
    wants = [exported["sourceSha"].lower(), exported["targetSha"].lower()]
    for want in wants:
        if want not in heads:
            raise Stop("bundle-tip-mismatch", detail=f"{want[:12]} not in bundle")
    tmp_base = "refs/tmp/mirror-mr/verify"
    try:
        for index, want in enumerate(wants):
            tmp = f"{tmp_base}-{index}"
            ok, failure = git_ok(mirror, "fetch", "-q", str(bundle),
                                 f"{want}:{tmp}")
            if not ok:
                raise Stop("bundle-unreadable",
                           detail=failure[:DETAIL_MAX] or f"cannot fetch {want[:12]}")
            ok, resolved = git_ok(mirror, "rev-parse", "--verify", f"{want}^{{commit}}")
            if not ok or resolved.strip().lower() != want:
                raise Stop("bundle-tip-mismatch",
                           detail=f"{want[:12]} is not a commit in the mirror")
    finally:
        git_ok(mirror, "update-ref", "-d", f"{tmp_base}-0")
        git_ok(mirror, "update-ref", "-d", f"{tmp_base}-1")


def main(argv=None) -> int:
    args = parse(argv)
    if not JOB_RE.match(args.job_id or ""):
        print("dell mirror import usage: --job-id must be 32 lowercase hex",
              file=sys.stderr)
        return 2
    mirror = Path(args.mirror_dir)
    state_path = Path(args.state)
    receipt_path = Path(args.receipt)
    if not mirror.is_absolute() or not state_path.is_absolute() \
            or not receipt_path.is_absolute():
        print("dell mirror import usage: --mirror-dir/--state/--receipt "
              "must be absolute", file=sys.stderr)
        return 2
    for path in (state_path, receipt_path):
        if path == mirror or mirror in path.parents:
            print("dell mirror import usage: --state/--receipt must stay "
                  "outside the mirror", file=sys.stderr)
            return 2
    receipt: dict = {"schema": RECEIPT_SCHEMA, "provenance": args.provenance,
                     "verdict": None, "reason": None, "detail": "",
                     "mrUrl": args.mr_url, "mirrorRepo": None,
                     "prUrl": None, "prNumber": None,
                     "sourceSha": None, "targetSha": None,
                     "comparison": None, "ancestry": None,
                     "checkedAt": now_iso()}
    state = blank_state(args)
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            receipt["verdict"] = "refused"
            receipt["reason"] = "state-malformed"
            receipt["detail"] = str(error)[:DETAIL_MAX]
            receipt["nextAction"] = NEXT_ACTION.get("state-malformed",
                                                   "stop and reconcile before any retry")
            return finish(receipt, receipt_path, 1)
        if not isinstance(loaded, dict) or loaded.get("schema") != STATE_SCHEMA \
                or loaded.get("jobId") != args.job_id \
                or loaded.get("mrUrl") != args.mr_url:
            receipt["verdict"] = "refused"
            receipt["reason"] = "state-mismatch"
            receipt["detail"] = "state names another job or MR"
            receipt["nextAction"] = NEXT_ACTION.get("state-mismatch",
                                                   "stop and reconcile before any retry")
            return finish(receipt, receipt_path, 1)
        state = loaded
    code = 0
    try:
        meta = parse_mr_url(args.mr_url)
        mapping = load_mapping(Path(args.mapping_file))
        mirror_repo = resolve_mirror(mapping, meta["host"], meta["project"])
        receipt["mirrorRepo"] = mirror_repo
        state["mirrorRepo"] = mirror_repo
        if not mirror.is_dir():
            raise Stop("mirror-unavailable", detail=f"no mirror checkout: {mirror}")
        github = load_github(args.github_dir)
        bridge = load_bridge(args.bridge_dir or default_bridge_dir())
        queue = Path(args.queue)
        key = read_key(bridge, queue, Path(args.key_file), args.key_format,
                       repos=[mirror], shared=Path(args.shared))
        job = verify_mirror_job(bridge, queue, key, args.job_id)
        payload = fetch_verified(bridge, queue, key, args.job_id,
                                 args.fetch_timeout, args.poll)
        exported = verify_payload(payload, job)
        check_export_identity(exported, args.mr_url, meta["iid"])
        if state.get("sourceSha") not in (None, exported["sourceSha"].lower()) \
                or state.get("targetSha") not in (None, exported["targetSha"].lower()):
            raise Stop("state-mismatch", detail="sealed SHAs moved under one job id")
        state.update({"token": exported.get("token"),
                      "sourceSha": exported["sourceSha"].lower(),
                      "targetSha": exported["targetSha"].lower(),
                      "sourceBranch": exported["sourceBranch"],
                      "targetBranch": exported["targetBranch"],
                      "title": exported.get("title") or "",
                      "description": exported.get("description") or ""})
        state["stage"] = "fetched"
        save_state(state_path, state)
        bundle = verify_artifacts(exported, args, payload)
        fetch_into_mirror(mirror, bundle, exported)
        state["stage"] = "verified"
        save_state(state_path, state)
        receipt.update({"sourceSha": state["sourceSha"],
                        "targetSha": state["targetSha"],
                        "ancestry": exported.get("ancestry")})
        if exported.get("ancestry") == "disjoint":
            raise Stop("evidence-gap", verdict="mismatch", detail=(
                "source and target share no merge-base; the change cannot be "
                "proven equivalent"))
        if github.repo_visibility(mirror_repo) != "private":
            raise Stop("destination-not-private",
                       detail=f"{mirror_repo} is not private")
        branches = {state["sourceBranch"], state["targetBranch"]}
        if not state.get("refsPushed"):
            reconcile_refs(mirror, state)
            push_refs(mirror, state)
            state["refsPushed"] = True
            state["stage"] = "refs-published"
            save_state(state_path, state)
        else:
            observed = read_remote_refs(mirror, branches)
            for branch, key_name in ((state["targetBranch"], "targetSha"),
                                     (state["sourceBranch"], "sourceSha")):
                if observed.get(branch) != state[key_name]:
                    reconcile_refs(mirror, state)
                    push_refs(mirror, state)
                    break
        if github.repo_visibility(mirror_repo) != "private":
            raise Stop("destination-not-private",
                       detail=f"{mirror_repo} is not private")
        pr = adopt_or_create(github, mirror, mirror_repo, state)
        receipt.update({"prUrl": pr["url"], "prNumber": pr.get("number"),
                        "comparison": pr["comparison"]})
        state.update({"prUrl": pr["url"], "prNumber": pr.get("number"),
                      "stage": "pr-created"})
        save_state(state_path, state)
        if pr["comparison"] != "exact":
            raise Stop("evidence-gap", verdict="mismatch", detail=pr["comparison"])
        receipt["verdict"] = "reconciled" if pr.get("reconciled") else "mirrored"
        receipt["reason"] = receipt["verdict"]
    except Stop as stop:
        if stop.verdict == "unknown" and not state.get("prUrl"):
            try:
                save_state(state_path, state)
            except OSError:
                pass
        receipt["verdict"] = stop.verdict
        receipt["reason"] = stop.reason
        receipt["detail"] = stop.detail
        code = stop.code
    except OSError as error:
        receipt["verdict"] = "mismatch"
        receipt["reason"] = "io-error"
        receipt["detail"] = str(error)[:DETAIL_MAX]
        code = 1
    receipt["nextAction"] = NEXT_ACTION.get(
        receipt["reason"], "stop and reconcile before any retry")
    if state.get("prUrl"):
        receipt["prUrl"] = state["prUrl"]
        receipt["prNumber"] = state.get("prNumber")
    return finish(receipt, receipt_path, code)


def finish(receipt: dict, receipt_path: Path, code: int) -> int:
    """Write the receipt and return the exit code."""

    text = json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{receipt_path.name}.", suffix=".tmp",
            dir=str(receipt_path.parent))
        os.close(handle)
        try:
            Path(temp_name).write_text(text, encoding="utf-8")
            os.replace(temp_name, receipt_path)
        finally:
            if Path(temp_name).exists():
                Path(temp_name).unlink()
    except OSError as error:
        print(text, end="")
        print(f"dell mirror import receipt unwritable: {error}", file=sys.stderr)
        return 3
    print(text, end="")
    print(f"dell mirror import {receipt['verdict']} : {receipt['reason']}",
          file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
