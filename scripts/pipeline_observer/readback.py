#!/usr/bin/env python3
"""Dell side: fetch and verify the Lenovo pipeline probe result.

Reads the sealed result for one signed-queue job id through the
existing signed bridge client (MAC plus kind/id binding happen inside
``fetch_result``: a valid envelope for another id never verifies as
this one), then binds the carried probe receipt to the expected
canonical MR URL and source SHA.

Fail-closed:

- an unknown, timed-out, or truncated job is ``unknown`` (exit 3):
  keep the slot, re-fetch the same job id, and reconcile before any
  new job id; the queue never reuses an id;
- a MAC or kind/id mismatch (a forged or swapped result) is
  ``mismatch`` (exit 1);
- a probe receipt whose MR URL or SHA is not exactly the expected one
  is ``mismatch`` (exit 1);
- a carried failure, an unavailable probe read, or any non-``passed``
  probe verdict is reported with that verdict (exit 1): a green parent
  with a missing required check is never ``passed``;
- only a well-formed ``pipeline-observer-probe/1`` receipt with
  verdict ``passed`` for the expected SHA verifies (exit 0).

The queue key travels through ``--key-file`` and is never printed or
stored in a receipt.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "pipeline-observer-readback/1"
PROBE_SCHEMA = "pipeline-observer-probe/1"


def load_bridge(bridge_dir: Path):
    path = Path(bridge_dir) / "lan_bridge.py"
    if not path.is_file():
        raise ValueError(f"bridge not found: {path}")
    spec = importlib.util.spec_from_file_location(
        "pipeline_observer_bridge", str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ValueError(f"cannot load bridge: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pipeline_observer_bridge"] = module
    spec.loader.exec_module(module)
    return module


def verify(payload: dict[str, Any], job_id: str,
           expected_mr_url: str, expected_sha: str) -> dict[str, Any]:
    """Bind one fetched result payload to the expected MR + SHA.

    Canonical form is used for the MR comparison, so a non-canonical
    but valid expected URL (trailing slash, /diffs suffix) still binds.
    The pipeline id is required only for a ``passed`` verdict; other
    verdicts (e.g. pending with no pipeline yet) are carried as-is.
    A receipt that carries no SHA and no ``passed`` verdict (e.g. a
    Lenovo-side outage) is ``unknown`` -- keep the slot and reconcile --
    not ``mismatch``.
    """
    want = (expected_sha or "").lower()
    try:
        from gate import canonical_url
    except ImportError:  # package-style invocation from the repo root
        from pipeline_observer.gate import (  # type: ignore[no-redef]
            canonical_url)
    try:
        want_mr = canonical_url(expected_mr_url)
    except Exception:
        want_mr = expected_mr_url
    base: dict[str, Any] = {
        "schema": SCHEMA, "read_only": True, "job_id": job_id,
        "mr_url": expected_mr_url, "expected_sha": want,
    }
    if payload.get("kind") != "result" or payload.get("id") != job_id:
        base.update(verdict="mismatch", reason="result-id-mismatch",
                    error="the sealed result is not bound to this job id")
        return base
    if payload.get("timed_out"):
        base.update(verdict="unknown", reason="result-timed-out",
                    error="the Lenovo job timed out; keep the slot "
                          "and reconcile")
        return base
    if payload.get("output_truncated"):
        base.update(verdict="unknown", reason="result-truncated",
                    error="the Lenovo result was truncated; keep the slot "
                          "and reconcile")
        return base
    exit_code = payload.get("exit_code")
    status = payload.get("status")
    if (status not in ("ok", "completed", "error")
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)):
        base.update(
            verdict="unknown", reason="job-not-successful",
            error="the Lenovo job did not report a trusted worker status "
                  f"(status={status!r}, exit={exit_code!r})",
            stderr=str(payload.get("stderr") or "")[:400])
        return base
    try:
        receipt = json.loads(payload.get("stdout") or "")
    except (TypeError, json.JSONDecodeError):
        if exit_code == 0:
            base.update(verdict="mismatch", reason="probe-unreadable",
                        error="the Lenovo result carries no probe JSON")
        else:
            base.update(verdict="unknown", reason="probe-unreadable",
                        error="the Lenovo job reported failure without "
                              "probe JSON; keep the slot and reconcile",
                        stderr=str(payload.get("stderr") or "")[:400])
        return base
    if not isinstance(receipt, dict) or receipt.get("schema") != PROBE_SCHEMA:
        if exit_code == 0:
            base.update(verdict="mismatch", reason="probe-unreadable",
                        error=f"expected a {PROBE_SCHEMA} receipt")
        else:
            base.update(verdict="unknown", reason="probe-unreadable",
                        error=f"expected a {PROBE_SCHEMA} receipt; keep "
                              "the slot and reconcile",
                        stderr=str(payload.get("stderr") or "")[:400])
        return base
    if receipt.get("mr_url") != want_mr:
        base.update(verdict="mismatch", reason="mr-mismatch",
                    error="the probe receipt names another merge request",
                    probe=receipt)
        return base
    verdict = receipt.get("verdict")
    observed = (receipt.get("observed_sha") or "")
    expected = (receipt.get("expected_sha") or "")
    if not observed or not expected:
        # No SHA bound (e.g. Lenovo-side read outage): never a mismatch.
        # A claimed pass without a SHA is inconsistent; anything else is
        # carried as unknown so the slot is kept and reconciled.
        if verdict == "passed":
            base.update(verdict="mismatch", reason="probe-inconsistent",
                        error="the probe claims passed without a bound "
                              "SHA",
                        probe=receipt)
        else:
            base.update(
                verdict="unknown",
                reason=receipt.get("reason") or "probe_unavailable",
                error="the probe receipt carries no SHA; keep the slot "
                      "and reconcile",
                probe=receipt)
        return base
    if expected.lower() != want or observed.lower() != want:
        base.update(verdict="mismatch", reason="sha-mismatch",
                    error="the probe receipt is not bound to the "
                          "expected SHA",
                    probe=receipt)
        return base
    if verdict == "passed" and receipt.get("pipeline_id") is None:
        base.update(verdict="unknown", reason="probe-inconsistent",
                    error="the probe claims passed but names no "
                          "pipeline; keep the slot and reconcile",
                    probe=receipt)
        return base
    if verdict == "passed":
        if exit_code != 0 or status not in ("ok", "completed"):
            base.update(
                verdict="unknown", reason="probe-inconsistent",
                error="the probe claims passed but the worker "
                      "status/exit is inconsistent; keep the slot "
                      "and reconcile",
                probe=receipt)
            return base
        base.update(verdict="passed", reason="probe_passed", probe=receipt)
        return base
    base.update(verdict=verdict if isinstance(verdict, str) else "unavailable",
                reason=receipt.get("reason") or "probe_not_passed",
                probe=receipt)
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="readback.py", description=__doc__)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=["raw", "base64"])
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--expected-mr-url", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--bridge-dir", type=Path, default=None)
    parser.add_argument("--receipt-out", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=660)
    parser.add_argument("--poll", type=float, default=10)
    args = parser.parse_args(argv)
    if args.bridge_dir is None:
        parser.error("--bridge-dir is required: path to the existing "
                     "signed bridge client directory")
    bridge = load_bridge(args.bridge_dir)
    try:
        key = bridge.load_key(args.key_file, args.key_format, args.queue)
    except Exception as error:
        parser.error(f"cannot load queue key: {error}")
    try:
        payload = bridge.fetch_result(
            args.queue, key, args.job_id,
            timeout_seconds=args.timeout, poll_seconds=args.poll)
    except bridge.FetchTimeout:
        receipt = {"schema": SCHEMA, "read_only": True,
                   "job_id": args.job_id, "verdict": "unknown",
                   "reason": "fetch-timeout",
                   "error": "no sealed result yet; keep the slot "
                            "and reconcile"}
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 3
    except bridge.ProtocolError as error:
        receipt = {"schema": SCHEMA, "read_only": True,
                   "job_id": args.job_id, "verdict": "mismatch",
                   "reason": "result-unverifiable",
                   "error": str(error)[:400]}
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 1
    except Exception as error:
        receipt = {"schema": SCHEMA, "read_only": True,
                   "job_id": args.job_id, "verdict": "unavailable",
                   "reason": "queue-result-unreadable",
                   "error": str(error)[:400]}
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 1
    receipt = verify(payload, args.job_id, args.expected_mr_url,
                     args.expected_sha)
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    print(text)
    if args.receipt_out is not None:
        args.receipt_out.parent.mkdir(parents=True, exist_ok=True)
        args.receipt_out.write_text(text + "\n", encoding="utf-8")
    verdict = receipt.get("verdict")
    if verdict == "passed":
        return 0
    if verdict == "unknown":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
