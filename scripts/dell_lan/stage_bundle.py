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

Every artifact is created exclusively: a pre-existing bundle, manifest, or
receipt is refused instead of replaced, and the same holds for a concurrent
writer that wins the name between the pre-check and the create. Names have no
fixed defaults, so two attempts cannot silently target one file. Work is done
in caller-owned temporary files beside the destination; a handled failure
removes both the temporary files and any final artifact this run created, so
no orphan is left behind. The receipt must be absolute and outside the task
worktree, the shared LAN folder, and any Git checkout.

Exit ``0`` staged with the receipt, ``1`` refused, ``2`` usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, Stop, containment, git_ok, load_json, manifest_digest, now_iso,
    safe_name, sha256_bytes,
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
    parser.add_argument("--bundle-name", required=True,
                        help="one contained, unused bundle file name")
    parser.add_argument("--manifest-name", required=True,
                        help="one contained, unused manifest file name")
    parser.add_argument("--expected-tip", default=None,
                        help="40-hex commit the bundle tip must equal")
    parser.add_argument("--receipt", type=Path, required=True,
                        help="unused receipt path outside every checkout")
    return parser.parse_args(argv)


def _lexists(path: Path) -> bool:
    """True for any directory entry, including a dangling link."""

    return os.path.lexists(str(path))


def _inside_checkout(value: Path) -> bool:
    """True when some ancestor of ``value`` carries a ``.git`` entry."""

    current = Path(value).parent
    while True:
        if (current / ".git").exists():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _refuse_receipt(receipt: Path, worktree: Path, shared: Path,
                    artifacts: tuple[Path, ...]) -> None:
    """Prove the receipt is a new file outside every protected root."""

    if not receipt.is_absolute():
        raise Stop("usage", code=2, detail="--receipt must be an absolute path")
    if _lexists(receipt):
        raise Stop("receipt-exists", detail=f"refusing to replace {receipt.name}")
    receipt_id = os.path.normcase(os.path.abspath(str(receipt)))
    for artifact in artifacts:
        if receipt_id == os.path.normcase(os.path.abspath(str(artifact))):
            raise Stop("usage", code=2,
                       detail="--receipt must differ from the staged artifact paths")
    for label, root in (("task worktree", worktree), ("shared LAN folder", shared)):
        verdict = containment(receipt, root)
        if verdict is None:
            raise Stop("receipt-refused",
                       detail=f"cannot prove the receipt is outside the {label}")
        if verdict:
            raise Stop("receipt-refused", detail=f"receipt inside the {label}")
    if _inside_checkout(receipt):
        raise Stop("receipt-refused", detail="receipt inside a Git checkout")


def validated(args) -> tuple[Path, Path, Path, Path, Path]:
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
    if args.bundle_name == args.manifest_name:
        raise Stop("usage", code=2,
                   detail="--bundle-name and --manifest-name must differ")
    expected = (args.expected_tip or "").strip().lower() or None
    if expected is not None and not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise Stop("usage", code=2, detail="--expected-tip must be 40 hex")
    bundle = shared / args.bundle_name
    manifest = shared / args.manifest_name
    for path in (bundle, manifest):
        if _lexists(path):
            raise Stop("artifact-exists", detail=f"refusing to replace {path.name}")
    receipt = Path(args.receipt)
    _refuse_receipt(receipt, worktree, shared, (bundle, manifest))
    return worktree, shared, bundle, manifest, receipt


def _temp_file(directory: Path, label: str) -> Path:
    """One caller-owned temporary file in the destination directory."""

    handle, name = tempfile.mkstemp(prefix=f".{label}.", suffix=".tmp",
                                    dir=str(directory))
    os.close(handle)
    return Path(name)


def _write_json_temp(path: Path, data: dict) -> None:
    path.write_bytes(
        (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
        .encode("utf-8")
    )


def _commit_new(temp: Path, final: Path, reason: str) -> None:
    """Publish ``temp`` at ``final`` only if ``final`` does not exist.

    The create is atomic: a hard link (or, where links are unavailable, an
    exclusive reservation followed by a replace) loses to a concurrent writer
    with ``artifact-exists`` instead of overwriting its file.
    """

    try:
        os.link(temp, final)
    except FileExistsError:
        raise Stop(reason, detail=f"refusing to replace {final.name}")
    except OSError:
        try:
            handle = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            raise Stop(reason, detail=f"refusing to replace {final.name}")
        os.close(handle)
        try:
            os.replace(temp, final)
        except OSError:
            try:
                final.unlink()
            except OSError:
                pass
            raise
    else:
        try:
            temp.unlink()
        except OSError:
            pass


def _cleanup(paths) -> None:
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass


def stage(args) -> dict:
    worktree, shared, bundle, manifest_path, receipt = validated(args)
    ok, _ = git_ok(worktree, "rev-parse", "--verify", "HEAD")
    if not ok:
        raise Stop("worktree-unreadable", detail=f"cannot read {worktree} HEAD")
    temp_bundle = _temp_file(shared, "bundle")
    temp_manifest = _temp_file(shared, "manifest")
    owned: list[Path] = []
    try:
        proc_ok, created = git_ok(worktree, "bundle", "create",
                                  str(temp_bundle), args.ref)
        if not proc_ok:
            raise Stop("bundle-unreadable", detail=created[:DETAIL_MAX])
        ok, listed = git_ok(worktree, "bundle", "verify", str(temp_bundle))
        if not ok:
            raise Stop("bundle-unreadable", detail=listed[:DETAIL_MAX])
        ok, heads = git_ok(worktree, "bundle", "list-heads", str(temp_bundle))
        tips = [line.split()[0].lower() for line in heads.splitlines() if line.strip()] if ok else []
        if not ok or not tips:
            raise Stop("bundle-unreadable", detail="bundle advertises no commit")
        tip = tips[0]
        expected = (args.expected_tip or "").strip().lower() or None
        if expected is not None and tip != expected:
            raise Stop("bundle-tip-mismatch", detail=f"bundle tip {tip} != {expected}")
        data = temp_bundle.read_bytes()
        files = [{"path": bundle.name, "sha256": sha256_bytes(data), "bytes": len(data)}]
        manifest = {"version": MANIFEST_SCHEMA, "revs": args.ref,
                    "bundle": bundle.name, "files": files,
                    "manifestDigest": manifest_digest(files)}
        _write_json_temp(temp_manifest, manifest)
        _commit_new(temp_bundle, bundle, "artifact-exists")
        owned.append(bundle)
        _commit_new(temp_manifest, manifest_path, "artifact-exists")
        owned.append(manifest_path)
        check = load_json(manifest_path, "manifest-unreadable")
        if manifest_digest(check.get("files", [])) != check.get("manifestDigest"):
            raise Stop("manifest-unreadable", detail="staged manifest does not re-read")
        result = {
            "schema": SCHEMA, "outcome": "staged",
            "bundle": bundle.name, "manifest": manifest_path.name,
            "ref": args.ref, "tip": tip, "sha256": files[0]["sha256"],
            "bytes": files[0]["bytes"], "manifestDigest": manifest["manifestDigest"],
            "stagedAt": now_iso(),
        }
        temp_receipt = None
        try:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            temp_receipt = _temp_file(receipt.parent, "receipt")
            _write_json_temp(temp_receipt, result)
            _commit_new(temp_receipt, receipt, "receipt-exists")
        except Stop:
            raise
        except OSError as error:
            raise Stop("receipt-unwritable", detail=str(error)[:DETAIL_MAX])
        finally:
            if temp_receipt is not None:
                _cleanup([temp_receipt])
        owned.append(receipt)
        return result
    except Exception:
        _cleanup(reversed(owned))
        raise
    finally:
        _cleanup([temp_bundle, temp_manifest])


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
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
