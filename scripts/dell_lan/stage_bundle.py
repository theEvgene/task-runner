#!/usr/bin/env python3
"""Dell side: stage the task worktree as one hashed git bundle plus manifest.

Writes ``<shared>/<bundle>`` with ``git bundle create`` from ``--ref`` (a
branch name or ``HEAD``: a bundle needs an advertised ref, and a raw commit
SHA names none), verifies it with ``git bundle verify``, hashes the bytes,
and writes ``<shared>/<manifest>`` in the ``git-bundle-manifest/1`` shape the
Lenovo driver re-verifies (manifest digest over the file entries, per-file
SHA-256 and byte counts, bundle tip bound by ``git bundle list-heads``).
Nothing is pushed, published, or edited in the worktree; the shared folder is
transport, the sealed request digest stays trust.

An existing bundle file is never replaced. Exit ``0`` staged with the
receipt, ``1`` refused, ``2`` usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, Stop, git, git_ok, load_json, manifest_digest, now_iso,
    safe_name, sha256_bytes, sha256_file, write_json_atomic,
)

SCHEMA = "dell-bundle-stage/1"
MANIFEST_SCHEMA = "git-bundle-manifest/1"
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="stage_bundle.py", description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True,
                        help="task worktree to stage (read only)")
    parser.add_argument("--ref", default="HEAD",
                        help="ref to bundle: a branch name or HEAD "
                             "(a raw SHA advertises no ref and is refused)")
    parser.add_argument("--shared", type=Path, required=True,
                        help="LAN share that holds the queue")
    parser.add_argument("--bundle-name", default="transfer.bundle",
                        help="one contained bundle file name")
    parser.add_argument("--manifest-name", default="transfer.manifest.json",
                        help="one contained manifest file name")
    parser.add_argument("--expected-tip", default=None,
                        help="40-hex commit the bundle tip must equal")
    parser.add_argument("--receipt", type=Path, required=True,
                        help="receipt path outside every checkout")
    return parser.parse_args(argv)


def validated(args) -> tuple[Path, Path]:
    worktree = Path(args.worktree)
    shared = Path(args.shared)
    if not worktree.is_dir():
        raise Stop("usage", code=2, detail=f"worktree is not a directory: {worktree}")
    if not shared.is_dir():
        raise Stop("usage", code=2, detail=f"shared directory does not exist: {shared}")
    ref = (args.ref or "").strip()
    if not ref or ref.startswith("-"):
        raise Stop("usage", code=2, detail="--ref must name a branch or HEAD")
    if ref != "HEAD" and (not BRANCH_RE.match(ref) or ".." in ref):
        raise Stop("usage", code=2, detail="--ref must be a branch name or HEAD")
    for label, name in (("--bundle-name", args.bundle_name),
                        ("--manifest-name", args.manifest_name)):
        if not safe_name(name):
            raise Stop("usage", code=2, detail=f"{label} must be one contained file name")
    expected = (args.expected_tip or "").strip().lower() or None
    if expected is not None and not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise Stop("usage", code=2, detail="--expected-tip must be 40 hex")
    bundle = shared / args.bundle_name
    if bundle.exists():
        raise Stop("artifact-exists", detail=f"refusing to replace {bundle.name}")
    return bundle, shared / args.manifest_name


def stage(args) -> dict:
    bundle, manifest_path = validated(args)
    worktree = Path(args.worktree)
    ok, _ = git_ok(worktree, "rev-parse", "--verify", "HEAD")
    if not ok:
        raise Stop("worktree-unreadable", detail=f"cannot read {worktree} HEAD")
    proc_ok, created = git_ok(worktree, "bundle", "create", str(bundle), args.ref)
    if not proc_ok:
        raise Stop("bundle-unreadable", detail=created[:DETAIL_MAX])
    ok, listed = git_ok(worktree, "bundle", "verify", str(bundle))
    if not ok:
        try:
            bundle.unlink()
        except OSError:
            pass
        raise Stop("bundle-unreadable", detail=listed[:DETAIL_MAX])
    ok, heads = git_ok(worktree, "bundle", "list-heads", str(bundle))
    tips = [line.split()[0].lower() for line in heads.splitlines() if line.strip()] if ok else []
    if not ok or not tips:
        raise Stop("bundle-unreadable", detail="bundle advertises no commit")
    tip = tips[0]
    expected = (args.expected_tip or "").strip().lower() or None
    if expected is not None and tip != expected:
        raise Stop("bundle-tip-mismatch", detail=f"bundle tip {tip} != {expected}")
    data = bundle.read_bytes()
    files = [{"path": bundle.name, "sha256": sha256_bytes(data), "bytes": len(data)}]
    manifest = {"version": MANIFEST_SCHEMA, "revs": args.ref,
                "bundle": bundle.name, "files": files,
                "manifestDigest": manifest_digest(files)}
    try:
        write_json_atomic(manifest_path, manifest)
    except OSError as error:
        raise Stop("receipt-unwritable", detail=str(error)[:DETAIL_MAX])
    check = load_json(manifest_path, "manifest-unreadable")
    if manifest_digest(check.get("files", [])) != check.get("manifestDigest"):
        raise Stop("manifest-unreadable", detail="staged manifest does not re-read")
    return {
        "schema": SCHEMA, "outcome": "staged",
        "bundle": bundle.name, "manifest": manifest_path.name,
        "ref": args.ref, "tip": tip, "sha256": files[0]["sha256"],
        "bytes": files[0]["bytes"], "manifestDigest": manifest["manifestDigest"],
        "stagedAt": now_iso(),
    }


def main(argv=None) -> int:
    args = parse(argv)
    try:
        result = stage(args)
    except Stop as stop:
        print(json.dumps({"schema": SCHEMA, "outcome": "refused",
                          "reason": stop.reason, "detail": stop.detail},
                         ensure_ascii=False, separators=(",", ":")))
        print(f"bundle stage refused: {stop.reason}", file=sys.stderr)
        return stop.code
    except OSError as error:
        print(json.dumps({"schema": SCHEMA, "outcome": "refused",
                          "reason": "io-error", "detail": str(error)[:DETAIL_MAX]},
                         ensure_ascii=False, separators=(",", ":")))
        return 1
    receipt = Path(args.receipt)
    try:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(receipt, result)
    except OSError as error:
        print(f"bundle stage receipt unwritable at {receipt}: {error}", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
