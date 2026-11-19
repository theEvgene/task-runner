#!/usr/bin/env python3
"""Dell side: import a merged GitLab MR as a historical review PR (#143).

Fetches the MAC-verified history export for one signed LAN job (unknown
outcomes resume the SAME id), verifies the bundle and the authoritative
diff, publishes uniquely named review-only refs, and creates or
reconciles one GitHub PR with readback. Same `--state` resume and
`github_ops` seam as the open route. Exit 0 mirrored, 1 refused,
2 usage, 3 unknown.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dell_lan"))

from mirror_mr_common import (DETAIL_MAX, JOB_RE, Stop, TOKEN_RE, check_branch,
                              check_export_identity, load_mapping,
                              parse_mr_url, resolve_mirror,
                              verify_export_job)
from dell_mirror_publish import read_remote_refs  # noqa: E402
from dell_mr_history_publish import (adopt_or_create_history,
                                      fetch_into_mirror, history_refs,
                                      push_history_refs,
                                      reconcile_history_refs,
                                      select_representation)  # noqa: E402
from dell_lan_common import (default_bridge_dir, fetch_verified,
                             load_bridge, load_json, manifest_digest,
                             now_iso, read_key, result_failure, safe_name,
                             sha256_file,
                             write_json_atomic)  # noqa: E402

HISTORY_EXPORT_SCHEMA = "mirror-mr-history-export/1"
HISTORY_STATE_SCHEMA = "mirror-mr-history-state/1"
HISTORY_RECEIPT_SCHEMA = "mirror-mr-history-receipt/1"

NEXT_ACTION = {
    "mirrored": "share the PR URL and receipt",
    "reconciled": "share the PR URL and receipt",
    "outcome-unknown": "re-run the same command with the same job id",
    "pr-conflict": "resolve the conflicting PR or ref by hand",
    "history-ref-conflict": "resolve the conflicting history ref by hand",
    "destination-not-private": "make the mirror private, or remap it",
    "evidence-gap": "do not treat the PR as equivalent",
    "history-unavailable": "the historical objects are gone",
    "queue-job-mismatch": "only the exact history-export command proves this import",
    "request-malformed": "the sealed payload does not match the job it names",
    "worktree-checked-out": "check out another ref before retrying",
}


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="dell_mr_history_import.py",
                                     description=__doc__)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--shared", required=True)
    parser.add_argument("--mirror-dir", required=True)
    parser.add_argument("--mapping-file", required=True)
    parser.add_argument("--mr-url", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--fetch-timeout", type=float, default=60.0)
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--provenance", default="fixture", choices=("fixture", "live"))
    parser.add_argument("--bridge-dir", type=Path, default=None)
    parser.add_argument("--github-dir", type=Path, default=None)
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
    return {"schema": HISTORY_STATE_SCHEMA, "mrUrl": args.mr_url,
            "mirrorRepo": None, "jobId": args.job_id, "token": None,
            "stage": "requested", "baseSha": None, "headSha": None,
            "squashSha": None, "chosenSha": None, "headRef": None,
            "baseRef": None, "representation": None, "title": None,
            "description": None, "prUrl": None, "prNumber": None,
            "refsPushed": False, "updatedAt": now_iso()}


def save_state(path: Path, state: dict) -> None:
    state["updatedAt"] = now_iso()
    write_json_atomic(path, state)


def verify_history_job(bridge, queue: Path, key: bytes, job_id: str) -> dict:
    """Bind the sealed job to the exact history-route export command.

    Shared contract in ``mirror_mr_common.verify_export_job``: the
    canonical PowerShell wrapper (legacy direct form still verifies
    for in-flight same-ID resumes) carrying exactly the history
    driver, the ``mirror-request-<16hex>`` basename, and the 64-hex
    digest.
    """

    return verify_export_job(
        bridge, queue, key, job_id, "lenovo_mr_history_export.py")


def _need_sha(value: object, label: str) -> str:
    text = str(value or "").lower()
    if len(text) != 40 or any(c not in "0123456789abcdef" for c in text):
        raise Stop("export-malformed", detail=f"{label} is not a commit SHA")
    return text


def verify_payload(payload: dict, job: dict | None = None) -> dict:
    failure = result_failure(payload)
    if failure is not None:
        raise Stop("export-job-failed", detail=json.dumps(failure)[:DETAIL_MAX])
    try:
        exported = json.loads(str(payload.get("stdout") or ""))
    except json.JSONDecodeError as error:
        raise Stop("export-malformed", detail=str(error)[:DETAIL_MAX])
    if not isinstance(exported, dict) or exported.get("schema") != HISTORY_EXPORT_SCHEMA:
        raise Stop("export-malformed", detail=f"expected {HISTORY_EXPORT_SCHEMA}")
    if exported.get("outcome") not in (None, "ok"):
        raise Stop("export-job-failed", detail=str(exported.get("reason"))[:DETAIL_MAX])
    if str(exported.get("status") or "") != "merged":
        raise Stop("mr-not-merged", detail=str(exported.get("status"))[:DETAIL_MAX])
    exported["baseSha"] = _need_sha(exported.get("baseSha"), "baseSha")
    exported["headSha"] = _need_sha(exported.get("headSha"), "headSha")
    squash = exported.get("squashSha")
    exported["squashSha"] = _need_sha(squash, "squashSha") if squash else None
    check_branch(str(exported.get("sourceBranch") or ""), "source branch")
    check_branch(str(exported.get("targetBranch") or ""), "target branch")
    for name in ("bundleName", "manifestName"):
        if not safe_name(str(exported.get(name) or "")):
            raise Stop("export-malformed", detail=f"{name} must be one contained file")
    digest = str(exported.get("sha256") or "").lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise Stop("export-malformed", detail="sha256 is not a digest")
    auth = str(exported.get("authoritativeDigest") or "").lower()
    if len(auth) != 64 or any(c not in "0123456789abcdef" for c in auth):
        raise Stop("export-malformed", detail="authoritativeDigest is not a digest")
    if not isinstance(exported.get("authoritativeFiles"), list) \
            or not exported["authoritativeFiles"]:
        raise Stop("export-malformed", detail="authoritativeFiles is not factual")
    if not isinstance(exported.get("bytes"), int) or exported["bytes"] < 1:
        raise Stop("export-malformed", detail="bytes is not factual")
    token = str(exported.get("token") or "").lower()
    if not TOKEN_RE.match(token):
        raise Stop("request-malformed", detail="token is not factual")
    if exported.get("bundleName") != f"mirror-mr-{token}.bundle" \
            or exported.get("manifestName") != f"mirror-mr-{token}.manifest.json":
        raise Stop("request-malformed", detail="sealed artifacts do not match the token")
    if job is not None and job.get("_requestFileToken") != token:
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


def main(argv=None) -> int:
    args = parse(argv)
    if not JOB_RE.match(args.job_id or ""):
        print("dell history import usage: --job-id must be 32 lowercase hex",
              file=sys.stderr)
        return 2
    mirror = Path(args.mirror_dir)
    state_path = Path(args.state)
    receipt_path = Path(args.receipt)
    if not mirror.is_absolute() or not state_path.is_absolute() \
            or not receipt_path.is_absolute():
        print("dell history import usage: --mirror-dir/--state/--receipt "
              "must be absolute", file=sys.stderr)
        return 2
    for path in (state_path, receipt_path):
        if path == mirror or mirror in path.parents:
            print("dell history import usage: --state/--receipt must stay "
                  "outside the mirror", file=sys.stderr)
            return 2
    receipt: dict = {"schema": HISTORY_RECEIPT_SCHEMA,
                     "provenance": args.provenance,
                     "verdict": None, "reason": None, "detail": "",
                     "mrUrl": args.mr_url, "mirrorRepo": None,
                     "status": "merged", "mrIid": None,
                     "prUrl": None, "prNumber": None,
                     "historicalBase": None, "historicalHead": None,
                     "representation": None,
                     "authoritativeDigest": None, "comparison": None,
                     "ancestry": None, "checkedAt": now_iso()}
    state = blank_state(args)
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            receipt["verdict"] = "refused"
            receipt["reason"] = "state-malformed"
            receipt["detail"] = str(error)[:DETAIL_MAX]
            receipt["nextAction"] = "stop and reconcile before any retry"
            return finish(receipt, receipt_path, 1)
        if not isinstance(loaded, dict) or loaded.get("schema") != HISTORY_STATE_SCHEMA \
                or loaded.get("jobId") != args.job_id \
                or loaded.get("mrUrl") != args.mr_url:
            receipt["verdict"] = "refused"
            receipt["reason"] = "state-mismatch"
            receipt["detail"] = "state names another job or MR"
            receipt["nextAction"] = "stop and reconcile before any retry"
            return finish(receipt, receipt_path, 1)
        state = loaded
    code = 0
    try:
        meta = parse_mr_url(args.mr_url)
        mapping = load_mapping(Path(args.mapping_file))
        mirror_repo = resolve_mirror(mapping, meta["host"], meta["project"])
        receipt["mirrorRepo"] = mirror_repo
        receipt["mrIid"] = meta["iid"]
        state["mirrorRepo"] = mirror_repo
        state["mrIid"] = meta["iid"]
        if not mirror.is_dir():
            raise Stop("mirror-unavailable", detail=f"no mirror checkout: {mirror}")
        github = load_github(args.github_dir)
        bridge = load_bridge(args.bridge_dir or default_bridge_dir())
        queue = Path(args.queue)
        key = read_key(bridge, queue, Path(args.key_file), args.key_format,
                       repos=[mirror], shared=Path(args.shared))
        job = verify_history_job(bridge, queue, key, args.job_id)
        payload = fetch_verified(bridge, queue, key, args.job_id,
                                 args.fetch_timeout, args.poll)
        exported = verify_payload(payload, job)
        check_export_identity(exported, args.mr_url, meta["iid"])
        if state.get("baseSha") not in (None, exported["baseSha"]) \
                or state.get("headSha") not in (None, exported["headSha"]):
            raise Stop("state-mismatch", detail="sealed SHAs moved under one job id")
        state.update({"token": exported.get("token"),
                      "baseSha": exported["baseSha"],
                      "headSha": exported["headSha"],
                      "squashSha": exported.get("squashSha"),
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
        chosen, representation = select_representation(mirror, exported)
        receipt.update({"historicalBase": state["baseSha"],
                        "historicalHead": chosen,
                        "representation": representation,
                        "authoritativeDigest": exported["authoritativeDigest"],
                        "ancestry": exported.get("ancestry")})
        state.update({"chosenSha": chosen, "representation": representation})
        base_ref, head_ref = history_refs(meta["iid"], state["baseSha"], chosen)
        state.update({"baseRef": base_ref, "headRef": head_ref})
        if exported.get("ancestry") == "disjoint":
            raise Stop("evidence-gap", verdict="mismatch", detail=(
                "historical base and head share no merge-base; the change "
                "cannot be proven equivalent"))
        if github.repo_visibility(mirror_repo) != "private":
            raise Stop("destination-not-private",
                       detail=f"{mirror_repo} is not private")
        if not state.get("refsPushed"):
            reconcile_history_refs(mirror, base_ref, head_ref,
                                   state["baseSha"], chosen)
            push_history_refs(mirror, base_ref, head_ref,
                              state["baseSha"], chosen)
            state["refsPushed"] = True
            state["stage"] = "refs-published"
            save_state(state_path, state)
        else:
            if state.get("headRef") != head_ref or state.get("baseRef") != base_ref \
                    or state.get("chosenSha") != chosen:
                raise Stop("state-mismatch",
                           detail="sealed history moved under one job id")
            observed = read_remote_refs(mirror, {base_ref, head_ref})
            if observed.get(base_ref) != state["baseSha"] \
                    or observed.get(head_ref) != chosen:
                reconcile_history_refs(mirror, base_ref, head_ref,
                                       state["baseSha"], chosen)
                push_history_refs(mirror, base_ref, head_ref,
                                  state["baseSha"], chosen)
        if github.repo_visibility(mirror_repo) != "private":
            raise Stop("destination-not-private",
                       detail=f"{mirror_repo} is not private")
        pr = adopt_or_create_history(github, mirror, mirror_repo, state,
                                     exported)
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
        print(f"dell history import receipt unwritable: {error}", file=sys.stderr)
        return 3
    print(text, end="")
    print(f"dell history import {receipt['verdict']} : {receipt['reason']}",
          file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
