#!/usr/bin/env python3
"""Lenovo side: export the published base ref as one hashed git bundle.

Runs as the single command of a signed LAN job. It reads a request file from
the shared LAN path, re-hashes it against the SHA-256 that travelled inside the
MAC-bound job payload, writes ``<shared>/dell-base-<token>.bundle`` plus its
``git-bundle-manifest/1`` manifest, and prints the observed facts as one JSON
line. The manifest is also declared as a return file, so a small MAC-bound
artefact comes back in-band while the bundle itself stays on the share.

Nothing here publishes, pushes, edits GitLab, or writes into the checkout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dell_lan_common import (
    BASE_EXPORT_SCHEMA, BASE_REQUEST_SCHEMA, DETAIL_MAX, Stop, git, git_ok,
    load_json, manifest_digest, now_iso, safe_name, sha256_bytes, write_json_atomic,
)


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="lenovo_base_export.py", description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    return parser.parse_args(argv)


def validated(request_path: Path, expected_digest: str) -> dict:
    raw = request_path.read_bytes()
    actual = sha256_bytes(raw)
    if actual != (expected_digest or "").lower():
        raise Stop("request-digest-mismatch", detail=f"{actual} != {expected_digest}")
    request = load_json(request_path, "request-malformed")
    if request.get("schema") != BASE_REQUEST_SCHEMA:
        raise Stop("request-malformed", detail=f"expected {BASE_REQUEST_SCHEMA}")
    for name in ("bundleName", "manifestName"):
        if not safe_name(str(request.get(name) or "")):
            raise Stop("request-malformed", detail=f"{name} must be one contained file name")
    ref_full = str(request.get("refFull") or "")
    if not ref_full.startswith("refs/heads/"):
        raise Stop("request-malformed", detail="refFull must be refs/heads/<branch>")
    shared = Path(str(request.get("shared") or ""))
    if not shared.is_dir():
        raise Stop("shared-unavailable", detail=str(shared)[:DETAIL_MAX])
    return request


def export(request: dict) -> dict:
    shared = Path(request["shared"])
    checkout = Path(request["lenovoCheckout"])
    if not checkout.is_dir():
        raise Stop("checkout-unavailable", detail=str(checkout)[:DETAIL_MAX])
    ref_full = request["refFull"]
    tip = git(checkout, "rev-parse", "--verify", f"{ref_full}^{{commit}}").lower()
    if len(tip) != 40:
        raise Stop("git-failed", detail="rev-parse did not return a commit")
    bundle = shared / request["bundleName"]
    if bundle.exists():
        raise Stop("artifact-exists", detail=f"refusing to replace {bundle.name}")
    git(checkout, "bundle", "create", str(bundle), ref_full)
    ok, listed = git_ok(checkout, "bundle", "list-heads", str(bundle))
    if not ok:
        raise Stop("bundle-unreadable", detail=listed[:DETAIL_MAX])
    heads = [line.split()[0].lower() for line in listed.splitlines() if line.strip()]
    if tip not in heads:
        raise Stop("bundle-tip-mismatch", detail=", ".join(heads)[:DETAIL_MAX])
    data = bundle.read_bytes()
    files = [{"path": bundle.name, "sha256": sha256_bytes(data), "bytes": len(data)}]
    manifest = {
        "version": "git-bundle-manifest/1",
        "revs": ref_full,
        "bundle": bundle.name,
        "files": files,
        "manifestDigest": manifest_digest(files),
    }
    manifest_path = shared / request["manifestName"]
    write_json_atomic(manifest_path, manifest)
    remotes = {}
    for name in git(checkout, "remote").splitlines():
        name = name.strip()
        if name:
            remotes[name] = git(checkout, "remote", "get-url", name)
    return {
        "schema": BASE_EXPORT_SCHEMA,
        "token": request["token"],
        "refFull": ref_full,
        "tipSha": tip,
        "bundleName": bundle.name,
        "manifestName": manifest_path.name,
        "sha256": files[0]["sha256"],
        "bytes": files[0]["bytes"],
        "manifestDigest": manifest["manifestDigest"],
        "remotes": remotes,
        "exportedAt": now_iso(),
    }


def main(argv=None) -> int:
    args = parse(argv)
    try:
        request = validated(Path(args.request), args.request_sha256)
        result = export(request)
    except Stop as stop:
        print(json.dumps({"schema": BASE_EXPORT_SCHEMA, "outcome": "refused",
                          "reason": stop.reason, "detail": stop.detail},
                         ensure_ascii=False, separators=(",", ":")))
        print(f"base export refused: {stop.reason}", file=sys.stderr)
        return stop.code
    except OSError as error:
        print(json.dumps({"schema": BASE_EXPORT_SCHEMA, "outcome": "refused",
                          "reason": "io-error", "detail": str(error)[:DETAIL_MAX]},
                         ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
