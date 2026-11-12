#!/usr/bin/env python3
"""Lenovo side: export the first verified copy of an explicit remote branch.

Runs as the single command of a signed LAN job. It reads a request file from
the shared LAN path, re-hashes it against the SHA-256 that travelled inside the
MAC-bound job payload, observes the actual remote tip into an isolated temp
bare repo (no Lenovo checkout is read or written), writes
``<shared>/dell-first-<token>.bundle`` plus its ``git-bundle-manifest/1``
manifest, and prints the observed facts as one JSON line. The manifest is also
declared as a return file, so a small MAC-bound artefact comes back in-band
while the bundle itself stays on the share.

Nothing here publishes, pushes, edits GitLab, or writes into any checkout.
Git credentials stay in the Lenovo user context and never enter the result.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, FIRST_EXPORT_SCHEMA, FIRST_REQUEST_SCHEMA, Stop, git, git_ok,
    load_json, manifest_digest, now_iso, safe_name, sha256_bytes, sha256_file,
    write_json_atomic,
)

TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="lenovo_first_export.py", description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    return parser.parse_args(argv)


def validated(request_path: Path, expected_digest: str) -> dict:
    raw = request_path.read_bytes()
    actual = sha256_bytes(raw)
    if actual != (expected_digest or "").lower():
        raise Stop("request-digest-mismatch", detail=f"{actual} != {expected_digest}")
    request = load_json(request_path, "request-malformed")
    if request.get("schema") != FIRST_REQUEST_SCHEMA:
        raise Stop("request-malformed", detail=f"expected {FIRST_REQUEST_SCHEMA}")
    token = str(request.get("token") or "")
    if not TOKEN_RE.match(token):
        raise Stop("request-malformed", detail="token must be 16 lowercase hex")
    remote = str(request.get("remoteUrl") or "")
    if not remote.strip() or any(char in remote for char in ("\n", "\r", "\0")):
        raise Stop("request-malformed", detail="remoteUrl must be one non-empty line")
    for name in ("bundleName", "manifestName"):
        if not safe_name(str(request.get(name) or "")):
            raise Stop("request-malformed", detail=f"{name} must be one contained file name")
    if (request.get("bundleName") != f"dell-first-{token}.bundle"
            or request.get("manifestName") != f"dell-first-{token}.manifest.json"):
        raise Stop("request-malformed", detail="artifacts do not match the token")
    ref_full = str(request.get("refFull") or "")
    branch = ref_full[len("refs/heads/"):] if ref_full.startswith("refs/heads/") else ""
    if not branch or not BRANCH_RE.match(branch):
        raise Stop("request-malformed", detail="refFull must be refs/heads/<branch>")
    shared = Path(str(request.get("shared") or ""))
    if not shared.is_dir():
        raise Stop("shared-unavailable", detail=str(shared)[:DETAIL_MAX])
    return request


def observe_remote_tip(remote: str, ref_full: str, token: str) -> tuple[str, Path]:
    """Fetch the actual remote ref into an isolated temp bare repo.

    Returns the verified tip SHA plus the temp repo dir. The caller owns the
    dir and removes it. Any drift between ls-remote and fetch fails closed.
    """

    proc_remote = git_ok(Path.cwd(), "ls-remote", remote, ref_full)
    ok_lines = proc_remote[1] if proc_remote[0] else ""
    if not proc_remote[0]:
        raise Stop("remote-unavailable", detail=ok_lines[:DETAIL_MAX] or "ls-remote failed")
    lines = [line.strip() for line in ok_lines.splitlines() if line.strip()]
    if len(lines) != 1:
        raise Stop("remote-unavailable", detail="ambiguous ref")
    tip, _, got_ref = lines[0].partition("\t")
    if not got_ref:
        tip, _, got_ref = lines[0].partition(" ")
    tip = tip.strip().lower()
    if got_ref.strip() != ref_full or len(tip) != 40 or any(c not in "0123456789abcdef" for c in tip):
        raise Stop("remote-unavailable", detail="no factual tip")
    workdir = Path(tempfile.mkdtemp(prefix="dell-first-fetch-"))
    try:
        git(workdir, "init", "--bare", "-q")
        ok, output = git_ok(workdir, "fetch", "--no-write-fetch-head", "--quiet",
                            remote, f"{ref_full}:refs/dell-first/{token}")
        if not ok:
            raise Stop("remote-unavailable", detail=output[:DETAIL_MAX] or "fetch failed")
        fetched = git(workdir, "rev-parse", "--verify", f"refs/dell-first/{token}^{{commit}}").lower()
        if fetched != tip:
            raise Stop("bundle-tip-mismatch", detail="fetch drifted from ls-remote")
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return tip, workdir


def export(request: dict) -> dict:
    shared = Path(request["shared"])
    remote = request["remoteUrl"]
    ref_full = request["refFull"]
    tip, workdir = observe_remote_tip(remote, ref_full, request["token"])
    branch = ref_full[len("refs/heads/"):]
    try:
        bundle = shared / request["bundleName"]
        if bundle.exists():
            raise Stop("artifact-exists", detail=f"refusing to replace {bundle.name}")
        # Advertise the requested branch name in the bundle: the temp repo is
        # disposable, so naming refs/heads/<branch> there cannot race another
        # job, and the Dell importer fetches exactly that ref.
        git(workdir, "update-ref", f"refs/heads/{branch}", tip)
        git(workdir, "bundle", "create", str(bundle), ref_full)
        ok, listed = git_ok(workdir, "bundle", "list-heads", str(bundle))
        if not ok:
            raise Stop("bundle-unreadable", detail=listed[:DETAIL_MAX])
        heads = [line.split()[0].lower() for line in listed.splitlines() if line.strip()]
        if tip not in heads:
            raise Stop("bundle-tip-mismatch", detail=", ".join(heads)[:DETAIL_MAX])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    data = bundle.read_bytes()
    digest = sha256_file(bundle)
    files = [{"path": bundle.name, "sha256": digest, "bytes": len(data)}]
    manifest = {
        "version": "git-bundle-manifest/1",
        "revs": ref_full,
        "bundle": bundle.name,
        "files": files,
        "manifestDigest": manifest_digest(files),
    }
    manifest_path = shared / request["manifestName"]
    write_json_atomic(manifest_path, manifest)
    return {
        "schema": FIRST_EXPORT_SCHEMA,
        "token": request["token"],
        "remoteUrl": remote,
        "refFull": ref_full,
        "tipSha": tip,
        "bundleName": bundle.name,
        "manifestName": manifest_path.name,
        "sha256": digest,
        "bytes": len(data),
        "manifestDigest": manifest["manifestDigest"],
        "exportedAt": now_iso(),
    }


def main(argv=None) -> int:
    args = parse(argv)
    try:
        request = validated(Path(args.request), args.request_sha256)
        result = export(request)
    except Stop as stop:
        print(json.dumps({"schema": FIRST_EXPORT_SCHEMA, "outcome": "refused",
                          "reason": stop.reason, "detail": stop.detail},
                         ensure_ascii=False, separators=(",", ":")))
        print(f"first export refused: {stop.reason}", file=sys.stderr)
        return stop.code
    except OSError as error:
        print(json.dumps({"schema": FIRST_EXPORT_SCHEMA, "outcome": "refused",
                          "reason": "io-error", "detail": str(error)[:DETAIL_MAX]},
                         ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
