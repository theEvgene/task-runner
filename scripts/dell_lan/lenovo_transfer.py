#!/usr/bin/env python3
"""Lenovo side: standalone transfer of one task branch, no publication.

Reuses the bundle intake (``lenovo_bundle``): request validation, manifest
verification and the one-time compare-and-swap import (``import_once``). This
driver never pushes and never creates a merge request: it stops after the
import and returns the worktree path, branch name and verified SHA. A repeat
names the explicit prior SHA; a dirty or foreign state is refused or reported
as unknown through the same mechanism, never by overwriting foreign state.

Exit codes: ``0`` transferred/observed, ``1`` refused, ``2`` usage,
``3`` unknown. Supports ``--reconcile``: read-only probe, never writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dell_lan_common import DETAIL_MAX, Stop, git_ok, now_iso
from dell_standalone_common import TRANSFER_REQUEST_SCHEMA, TRANSFER_RESULT_SCHEMA
from lenovo_bundle import bundle_tip, import_once, validated, verify_bundle
from lenovo_remote import require


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="lenovo_transfer.py", description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--reconcile", action="store_true",
                        help="read-only probe: observe the branch/worktree, never write")
    return parser.parse_args(argv)


def _observe(request: dict, result: dict) -> None:
    """Read-only probe of an already transferred branch (never writes)."""

    from pathlib import Path as _P

    checkout = _P(request["lenovoCheckout"])
    worktree = _P(request["lenovoWorktree"])
    expected = request["expectedCommit"]
    ok, current = git_ok(checkout, "rev-parse", "--verify", f"refs/heads/{request['branch']}")
    observed = current.lower() if ok else None
    head = None
    owned_branch = None
    clean = None
    if worktree.is_dir():
        ok2, out = git_ok(worktree, "rev-parse", "--verify", "HEAD")
        head = out.lower() if ok2 else None
        ok3, out = git_ok(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        owned_branch = out.strip() if ok3 else None
        ok4, out = git_ok(worktree, "status", "--porcelain")
        clean = not out.strip() if ok4 else None
    matches = (observed == expected and head == expected
               and owned_branch == request["branch"] and clean is True)
    result["import"] = {"branch": request["branch"], "commit": expected,
                        "worktree": str(worktree), "observedBranchHead": observed,
                        "observedWorktreeHead": head,
                        "observedWorktreeBranch": owned_branch,
                        "clean": clean, "matches": matches}
    if not matches:
        raise Stop("import-unknown", verdict="unknown", code=3, detail=(
            "reconciliation did not prove the requested branch owns a clean worktree "
            f"at {expected}"))
    result["outcome"] = "observed"


def main(argv=None) -> int:
    args = parse(argv)
    result = {"schema": TRANSFER_RESULT_SCHEMA, "outcome": "refused", "stage": "request",
              "token": None, "authorized": [], "bundle": None, "import": None,
              "push": None, "mr": None,
              "pipeline": {"waited": False, "polled": False},
              "error": None, "finishedAt": now_iso()}
    code = 0
    try:
        request = validated(Path(args.request), args.request_sha256, args.reconcile)
        schema = request.get("schema")
        if schema != TRANSFER_REQUEST_SCHEMA:
            raise Stop("request-malformed", detail=f"expected transfer request, got {schema}")
        result["token"] = request["token"]
        result["authorized"] = request["authorized"]
        if request["reconcileOnly"]:
            require(request, "reconcile")
            result["stage"] = "reconcile"
            _observe(request, result)
        else:
            require(request, "import")
            result["stage"] = "bundle"
            bundle = verify_bundle(request, result)
            tip = bundle_tip(Path(request["lenovoCheckout"]), bundle)
            result["stage"] = "import"
            worktree = import_once(request, bundle, tip, result)
            result["import"]["worktree"] = str(worktree)
            result["outcome"] = "ok"
    except Stop as stop:
        result["outcome"] = stop.verdict if stop.verdict in ("unknown", "refused") else "refused"
        result["error"] = {"reason": stop.reason, "detail": stop.detail}
        code = stop.code
    except OSError as error:
        result["outcome"] = "unknown"
        result["error"] = {"reason": "io-error", "detail": str(error)[:DETAIL_MAX]}
        code = 3
    except Exception as error:  # noqa: BLE001 - an unattended driver always answers
        result["outcome"] = "unknown"
        result["error"] = {"reason": "driver-error",
                           "detail": f"{type(error).__name__}: {error}"[:DETAIL_MAX]}
        code = 3
    result["finishedAt"] = now_iso()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(f"lenovo transfer {result['outcome']} : {result['stage']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
