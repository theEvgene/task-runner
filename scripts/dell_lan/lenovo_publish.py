#!/usr/bin/env python3
"""Lenovo side: import the staged task bundle and publish it through GitLab.

Thin driver: request intake lives in ``lenovo_bundle``, remote steps in
``lenovo_remote``. A ``ready`` request adds one consented step after the
Draft publish: the external ``remove_draft.py`` marks the same MR Ready and
the driver proves title/description still match. No pipeline wait on any path.
Exit codes: ``0`` published/observed, ``1`` refused, ``2`` usage, ``3`` unknown.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, PUBLISH_RESULT_SCHEMA, Stop, default_flow_dir, now_iso,
)
from lenovo_bundle import ACTIONS, bundle_tip, import_once, validated, verify_bundle
from lenovo_publish_flow import load_description
from lenovo_remote import observe, preflight, publish, push, require


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="lenovo_publish.py", description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--reconcile", action="store_true",
                        help="read-only probe: observe the remote branch and MR, never write")
    parser.add_argument("--flow-dir", type=Path, help="gitlab-mr-flow scripts dir (default: sibling)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse(argv)
    result = {"schema": PUBLISH_RESULT_SCHEMA, "outcome": "refused", "stage": "request",
              "token": None, "authorized": [], "bundle": None, "import": None,
              "preflight": None, "push": None, "mr": None, "identity": None,
              "undraft": None, "pipeline": {"waited": False, "polled": False},
              "error": None, "finishedAt": now_iso()}
    code = 0
    try:
        request = validated(Path(args.request), args.request_sha256, args.reconcile)
        result["token"] = request["token"]
        result["authorized"] = request["authorized"]
        if request["reconcileOnly"]:
            require(request, "reconcile")
        else:
            # The push below is already a remote write, so undecodable input
            # must fail here, before any external write, never at the MR stage.
            request["descriptionText"] = load_description(request)
        result["stage"] = "bundle"
        bundle = verify_bundle(request, result)
        tip = bundle_tip(Path(request["lenovoCheckout"]), bundle)
        if request["reconcileOnly"]:
            result["stage"] = "reconcile"
            observe(request, Path(request["lenovoWorktree"]), args.flow_dir or default_flow_dir(), result)
            result["outcome"] = "observed"
        else:
            result["stage"] = "import"
            worktree = import_once(request, bundle, tip, result)
            result["stage"] = "preflight"
            preflight(request, worktree, args.flow_dir or default_flow_dir(), result)
            result["stage"] = "push"
            push(request, worktree, result)
            result["stage"] = "mr"
            receipt = publish(request, worktree, args.flow_dir or default_flow_dir(), result)
            result["stage"] = "readback"
            if str(receipt.get("head_sha") or "").lower() != request["expectedCommit"]:
                raise Stop("publication-head-mismatch",
                           detail=f"{receipt.get('head_sha')} != {request['expectedCommit']}")
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
    print(f"lenovo publish {result['outcome']} : {result['stage']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

# Re-exported for existing callers/tests: intake lives in lenovo_bundle.
__all__ = ["ACTIONS", "bundle_tip", "import_once", "parse", "main", "validated", "verify_bundle"]
