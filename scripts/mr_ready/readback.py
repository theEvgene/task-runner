#!/usr/bin/env python3
"""Dell side: verify one Lenovo ``mr_ready_run.py`` signed result."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from pipeline_observer.gate import (  # noqa: E402
    GateInputError,
    canonical_url,
)
from pipeline_observer.readback import load_bridge  # noqa: E402

SCHEMA = "mr-ready-readback/1"
RUN_SCHEMA = "mr-ready-run/1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _result(job_id: str, verdict: str, reason: str, *,
            receipt: dict[str, Any] | None = None,
            detail: str | None = None,
            next_action: str | None = None) -> dict[str, Any]:
    return {"schema": SCHEMA, "job_id": job_id, "verdict": verdict,
            "reason": reason, "detail": detail,
            "next_action": next_action, "run": receipt}


def _identity_problem(receipt: dict[str, Any], expected_url: str) -> str | None:
    try:
        observed = canonical_url(str(receipt.get("mr_url") or ""))
    except GateInputError:
        return "runner receipt has no canonical MR URL"
    if observed != canonical_url(expected_url):
        return "runner receipt belongs to another MR URL"
    for name in ("head_sha", "target_sha"):
        if not SHA_RE.fullmatch(str(receipt.get(name) or "")):
            return f"runner receipt has no valid {name}"
    if not isinstance(receipt.get("iid"), int) or receipt["iid"] < 1:
        return "runner receipt has no valid MR IID"
    for name in ("source_branch", "target_branch"):
        if not isinstance(receipt.get(name), str) or not receipt[name]:
            return f"runner receipt has no {name}"
    return None


def verify(payload: dict[str, Any], job_id: str,
           expected_mr_url: str) -> dict[str, Any]:
    if payload.get("kind") != "result" or payload.get("id") != job_id:
        return _result(job_id, "mismatch", "result_identity_mismatch")
    if payload.get("timed_out") or payload.get("output_truncated"):
        return _result(
            job_id, "unknown", "incomplete_worker_result",
            detail="the Lenovo result timed out or was truncated",
            next_action="re-fetch the same job id and reconcile it before submitting another run")
    exit_code = payload.get("exit_code")
    worker_status = payload.get("status")
    if (worker_status not in ("ok", "completed", "error") or
            isinstance(exit_code, bool) or exit_code not in (0, 1)):
        return _result(
            job_id, "unknown", "runner_error",
            detail=str(payload.get("stderr") or "mr_ready_run.py returned no usable receipt"),
            next_action="fix the reported Lenovo runner input or environment, then submit a new run")
    try:
        receipt = json.loads(str(payload.get("stdout") or ""))
    except (TypeError, ValueError):
        return _result(job_id, "unknown", "unreadable_receipt",
                       next_action="inspect the sealed stdout and reconcile this job id")
    if not isinstance(receipt, dict) or receipt.get("schema") != RUN_SCHEMA:
        return _result(job_id, "mismatch", "wrong_receipt_schema")
    problem = _identity_problem(receipt, expected_mr_url)
    if problem:
        return _result(job_id, "mismatch", "identity_mismatch",
                       receipt=receipt, detail=problem,
                       next_action="discard this result and run again for the intended MR")

    status = receipt.get("status")
    gate = receipt.get("gate")
    if gate is not None:
        if not isinstance(gate, dict) or gate.get("sha") != receipt["head_sha"]:
            return _result(
                job_id, "mismatch", "stale_gate_sha", receipt=receipt,
                detail="pipeline evidence is not bound to the receipt head SHA",
                next_action="run the mechanism again on the current MR head")

    if status == "ready":
        publish = receipt.get("publish")
        valid = (
            exit_code == 0 and worker_status in ("ok", "completed") and
            receipt.get("blockers") == [] and
            isinstance(gate, dict) and gate.get("outcome") == "passed" and
            isinstance(receipt.get("phase_gate"), dict) and
            isinstance(publish, dict) and publish.get("draft") is False and
            publish.get("mr_url") == receipt.get("mr_url") and
            publish.get("target_sha") == receipt.get("target_sha")
        )
        if not valid:
            return _result(
                job_id, "mismatch", "ready_unproven", receipt=receipt,
                detail="the runner claims Ready without complete bound readback evidence",
                next_action="inspect the MR and run the mechanism again; do not treat it as confirmed Ready")
        return _result(job_id, "ready", "ready_confirmed", receipt=receipt,
                       next_action=str(receipt.get("next_action") or "review the Ready MR"))

    blockers = receipt.get("blockers")
    first = blockers[0] if isinstance(blockers, list) and blockers else None
    if (status != "draft" or exit_code != 1 or worker_status != "error" or
            not isinstance(first, dict)):
        return _result(job_id, "mismatch", "invalid_terminal_receipt",
                       receipt=receipt,
                       next_action="inspect the MR state manually before another write")
    kind = str(first.get("kind") or "blocked")
    detail = str(first.get("detail") or "the Ready run stopped")
    next_action = str(first.get("next_step") or receipt.get("next_action") or
                      "resolve the blocker, then run again")
    publish = receipt.get("publish")
    landed_unconfirmed = (
        kind == "publish_landed_unconfirmed" or
        (isinstance(publish, dict) and
         (publish.get("readback") == "ready" or publish.get("draft") is False))
    )
    if landed_unconfirmed:
        return _result(job_id, "unknown", "write_unconfirmed",
                       receipt=receipt, detail=detail,
                       next_action=next_action)
    return _result(job_id, "blocked", kind, receipt=receipt,
                   detail=detail, next_action=next_action)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="readback.py", description=__doc__)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", choices=("raw", "base64"), default="raw")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--expected-mr-url", required=True)
    parser.add_argument("--bridge-dir", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll", type=float, default=0.25)
    args = parser.parse_args(argv)
    try:
        canonical_url(args.expected_mr_url)
        bridge = load_bridge(args.bridge_dir)
        key = bridge.load_key(args.key_file, args.key_format, args.queue)
        payload = bridge.fetch_result(
            args.queue, key, args.job_id,
            timeout_seconds=args.timeout, poll_seconds=args.poll)
        result = verify(payload, args.job_id, args.expected_mr_url)
    except Exception as error:  # bridge-specific timeout/error classes are dynamic
        result = _result(
            args.job_id, "unknown", "readback_unavailable", detail=str(error),
            next_action="re-fetch the same job id; do not submit a duplicate write while its outcome is unknown")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.receipt_out:
        args.receipt_out.parent.mkdir(parents=True, exist_ok=True)
        args.receipt_out.write_text(text + "\n", encoding="utf-8")
    return 0 if result["verdict"] == "ready" else (
        3 if result["verdict"] == "unknown" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
