#!/usr/bin/env python3
"""Lenovo side: observe one open GitLab MR and export its revisions (issue #142).

Runs as the single command of a signed LAN job. It reads a request file from
the shared LAN path, re-hashes it against the SHA-256 that travelled inside
the MAC-bound job payload, observes the MR through the reader seam
(``gitlab_mr_reader.read_open_mr`` from ``--flow-dir``, else the ``glab``
CLI), resolves the exact source/target revisions against the Lenovo remote,
exports both as one hashed git bundle plus its ``git-bundle-manifest/1``
manifest, re-observes the MR, and refuses a moved MR instead of snapshotting
it. The manifest is declared as a return file so a small MAC-bound artefact
also comes back in-band while the bundle itself stays on the share.

Nothing here publishes, pushes, merges, or edits GitLab. A refusal deletes
the bundle and manifest this run staged, so a failed observation leaves no
partial artefact behind.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dell_lan"))

from mirror_mr_common import (DETAIL_MAX, EXPORT_SCHEMA, REQUEST_SCHEMA, SHA_RE,
                              Stop, check_branch)
from dell_lan_common import (git, git_ok, load_json, manifest_digest, now_iso,
                             safe_name, sha256_bytes, sha256_file,
                             write_json_atomic)  # noqa: E402


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="lenovo_mr_export.py", description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--flow-dir", default=None,
                        help="reader seam dir holding gitlab_mr_reader.py "
                             "(default: the glab CLI fallback)")
    return parser.parse_args(argv)


def validated(request_path: Path, expected_digest: str) -> dict:
    raw = request_path.read_bytes()
    if sha256_bytes(raw) != (expected_digest or "").lower():
        raise Stop("request-digest-mismatch",
                   detail=f"{sha256_bytes(raw)} != {expected_digest}")
    request = load_json(request_path, "request-malformed")
    if request.get("schema") != REQUEST_SCHEMA:
        raise Stop("request-malformed", detail=f"expected {REQUEST_SCHEMA}")
    for name in ("bundleName", "manifestName"):
        if not safe_name(str(request.get(name) or "")):
            raise Stop("request-malformed", detail=f"{name} must be one contained file")
    if not str(request.get("mrUrl") or "").startswith("https://"):
        raise Stop("request-malformed", detail="mrUrl must be https")
    shared = Path(str(request.get("shared") or ""))
    if not shared.is_dir():
        raise Stop("shared-unavailable", detail=str(shared)[:DETAIL_MAX])
    return request


def read_mr(request: dict, flow_dir: str | None) -> dict:
    """The current MR identity: status, branches, and revisions."""

    host = request["gitlabHost"]
    project = request["gitlabProject"]
    iid = request["mrIid"]
    if flow_dir:
        sys.path.insert(0, flow_dir)
        try:
            reader = importlib.import_module("gitlab_mr_reader")
        except Exception as error:
            raise Stop("reader-unavailable",
                       detail=f"{flow_dir}: {error}"[:DETAIL_MAX])
        try:
            observed = reader.read_open_mr(host, project, iid)
        except getattr(reader, "ReaderError", Exception) as error:
            raise Stop("mr-unreadable", detail=str(error)[:DETAIL_MAX])
        if not isinstance(observed, dict):
            raise Stop("mr-unreadable", detail="reader returned no MR record")
        return normalise(observed)
    return via_glab(request)


def normalise(record: dict) -> dict:
    status = str(record.get("status") or record.get("state") or "").lower()
    if status not in ("opened", "open"):
        raise Stop("mr-not-open", detail=f"MR state is {status or 'unknown'}")
    source = check_branch(str(record.get("sourceBranch")
                              or record.get("source_branch") or ""), "source branch")
    target = check_branch(str(record.get("targetBranch")
                              or record.get("target_branch") or ""), "target branch")
    source_sha = str(record.get("sourceSha") or record.get("source_sha")
                     or record.get("sha") or record.get("head_sha") or "").lower()
    target_sha = str(record.get("targetSha") or record.get("target_sha") or "").lower()
    if not SHA_RE.match(source_sha) or not SHA_RE.match(target_sha):
        raise Stop("mr-unreadable", detail="reader returned no exact revisions")
    return {"status": "opened",
            "title": str(record.get("title") or ""),
            "description": str(record.get("description") or ""),
            "sourceBranch": source, "targetBranch": target,
            "sourceSha": source_sha, "targetSha": target_sha}


def via_glab(request: dict) -> dict:
    proc = subprocess.run(
        ["glab", "mr", "view", request["mrUrl"], "--output", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)
    if proc.returncode != 0:
        raise Stop("mr-unreadable",
                   detail=(proc.stderr or proc.stdout).strip()[:DETAIL_MAX])
    try:
        return normalise(json.loads(proc.stdout or "{}"))
    except json.JSONDecodeError as error:
        raise Stop("mr-unreadable", detail=str(error)[:DETAIL_MAX])


def resolve(request: dict, observed: dict) -> dict:
    """Bind the observed branches to exact remote revisions."""

    checkout = Path(request["lenovoCheckout"])
    if not checkout.is_dir():
        raise Stop("checkout-unavailable", detail=str(checkout)[:DETAIL_MAX])
    refs: dict[str, str] = {}
    for branch in (observed["sourceBranch"], observed["targetBranch"]):
        ok, output = git_ok(checkout, "ls-remote", "origin", f"refs/heads/{branch}")
        if not ok:
            raise Stop("remote-unreadable", detail=output[:DETAIL_MAX])
        lines = [line.split()[0].lower() for line in output.splitlines() if line.strip()]
        if not lines:
            raise Stop("branch-missing", detail=f"origin has no refs/heads/{branch}")
        refs[branch] = lines[0]
    if refs[observed["sourceBranch"]] != observed["sourceSha"] \
            or refs[observed["targetBranch"]] != observed["targetSha"]:
        raise Stop("mr-moved", detail=(
            f"metadata {observed['sourceSha'][:12]}/{observed['targetSha'][:12]} "
            f"!= remote {refs[observed['sourceBranch']][:12]}/"
            f"{refs[observed['targetBranch']][:12]}; re-read before any retry"))
    return refs


def export(request: dict, observed: dict) -> dict:
    shared = Path(request["shared"])
    checkout = Path(request["lenovoCheckout"])
    token = request["token"]
    tmp = f"refs/tmp/mirror-mr/{token}"
    src_ref, tgt_ref = f"{tmp}/source", f"{tmp}/target"
    try:
        ok, failure = git_ok(checkout, "fetch", "-q", "origin",
                             f"refs/heads/{observed['sourceBranch']}:{src_ref}",
                             f"refs/heads/{observed['targetBranch']}:{tgt_ref}")
        if not ok:
            raise Stop("remote-unreadable", detail=failure[:DETAIL_MAX])
        for ref, want in ((src_ref, observed["sourceSha"]),
                          (tgt_ref, observed["targetSha"])):
            if git(checkout, "rev-parse", "--verify", f"{ref}^{{commit}}").lower() != want:
                raise Stop("mr-moved", detail=f"{ref} no longer resolves to {want[:12]}")
        bundle = shared / request["bundleName"]
        if bundle.exists():
            raise Stop("artifact-exists", detail=f"refusing to replace {bundle.name}")
        git(checkout, "bundle", "create", str(bundle), src_ref, tgt_ref)
        ok, listed = git_ok(checkout, "bundle", "list-heads", str(bundle))
        if not ok:
            raise Stop("bundle-unreadable", detail=listed[:DETAIL_MAX])
        heads = [line.split()[0].lower() for line in listed.splitlines() if line.strip()]
        for want in (observed["sourceSha"], observed["targetSha"]):
            if want not in heads:
                raise Stop("bundle-tip-mismatch", detail=f"{want[:12]} not in bundle")
        data = bundle.read_bytes()
        files = [{"path": bundle.name, "sha256": sha256_bytes(data), "bytes": len(data)}]
        manifest = {"version": "git-bundle-manifest/1",
                    "revs": f"{observed['sourceSha']} {observed['targetSha']}",
                    "bundle": bundle.name, "files": files,
                    "manifestDigest": manifest_digest(files)}
        manifest_path = shared / request["manifestName"]
        if manifest_path.exists():
            raise Stop("artifact-exists", detail=f"refusing to replace {manifest_path.name}")
        write_json_atomic(manifest_path, manifest)
        ok, base = git_ok(checkout, "merge-base",
                           observed["targetSha"], observed["sourceSha"])
        ancestry = "connected" if ok else "disjoint"
        return {"bundle": bundle, "manifestPath": manifest_path,
                "sha256": files[0]["sha256"], "bytes": len(data),
                "manifestDigest": manifest["manifestDigest"],
                "mergeBase": base.strip().lower() if ok else None,
                "ancestry": ancestry}
    finally:
        git_ok(checkout, "update-ref", "-d", src_ref)
        git_ok(checkout, "update-ref", "-d", tgt_ref)


def main(argv=None) -> int:
    args = parse(argv)
    staged: list[Path] = []
    try:
        request = validated(Path(args.request), args.request_sha256)
        observed = read_mr(request, args.flow_dir)
        resolve(request, observed)
        exported = export(request, observed)
        staged = [exported["bundle"], exported["manifestPath"]]
        rechecked = read_mr(request, args.flow_dir)
        if rechecked["sourceSha"] != observed["sourceSha"] \
                or rechecked["targetSha"] != observed["targetSha"]:
            raise Stop("mr-moved", detail=(
                "MR moved between observation and export; "
                "re-read before any retry"))
        result = {"schema": EXPORT_SCHEMA, "token": request["token"],
                  "mrUrl": request["mrUrl"], "mrIid": request["mrIid"],
                  "status": "opened", "title": observed["title"],
                  "description": observed["description"],
                  "sourceBranch": observed["sourceBranch"],
                  "targetBranch": observed["targetBranch"],
                  "sourceSha": observed["sourceSha"],
                  "targetSha": observed["targetSha"],
                  "mergeBase": exported["mergeBase"],
                  "ancestry": exported["ancestry"],
                  "bundleName": request["bundleName"],
                  "manifestName": request["manifestName"],
                  "sha256": exported["sha256"], "bytes": exported["bytes"],
                  "manifestDigest": exported["manifestDigest"],
                  "observedAt": now_iso()}
    except Stop as stop:
        for path in staged:
            try:
                path.unlink()
            except OSError:
                pass
        print(json.dumps({"schema": EXPORT_SCHEMA, "outcome": "refused",
                          "reason": stop.reason, "detail": stop.detail},
                         ensure_ascii=False, separators=(",", ":")))
        print(f"mr export refused: {stop.reason}", file=sys.stderr)
        return stop.code
    except OSError as error:
        print(json.dumps({"schema": EXPORT_SCHEMA, "outcome": "refused",
                          "reason": "io-error",
                          "detail": str(error)[:DETAIL_MAX]},
                         ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
