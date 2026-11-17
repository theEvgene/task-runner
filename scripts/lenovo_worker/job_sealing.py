"""Seal one terminal worker result into the signed queue (stdlib only).

The worker module decides *what* happened to a job; this module owns the one
sealed shape that reports it. A refused or oversized result is still sealed
once, in the safe direction: an unsealed job would be re-executed, and a
failure to seal is reported as ``failed`` rather than retried.
"""

from __future__ import annotations

import sys
from pathlib import Path

import job_results


def _result_payload(job_id: str, now, **fields) -> dict:
    payload = {
        "kind": "result", "id": job_id,
        "status": fields.get("status", "error"),
        "exit_code": fields.get("exit_code", 2),
        "stdout": fields.get("stdout", "") or "",
        "stderr": fields.get("stderr", "") or "",
        "started_at": fields.get("started_at", "") or "",
        "finished_at": fields.get("finished_at") or now(),
        "timed_out": bool(fields.get("timed_out", False)),
        "output_truncated": bool(fields.get("output_truncated", False)),
        "files": fields.get("files", {}) or {},
    }
    for name in ("termination", "signal", "process_exit_code"):
        if fields.get(name) is not None:
            payload[name] = fields[name]
    if fields.get("reconciliation") is not None:
        payload["reconciliation"] = fields["reconciliation"]
    return payload


def seal_result(queue: Path, key: bytes, job_id: str, bridge: object,
                now, **fields) -> str:
    """Seal a result. Returns "sealed", "skipped", or "failed"."""

    payload = _result_payload(job_id, now, **fields)
    job_results.fit_payload(payload)
    try:
        bridge.publish(str(queue / "results"), job_id, bridge.seal(payload, key))
        return "sealed"
    except bridge.JobExistsError:
        return "skipped"  # another worker sealed it first: safe direction
    except Exception as error:
        message = str(error)
        if "too large" in message or "exceeds" in message or "limit" in message.lower():
            # Size refusal must not leave the job unsealed to re-execute:
            # seal one minimal terminal result instead.
            fallback = {
                "kind": "result", "id": job_id, "status": "error",
                "exit_code": 2, "stdout": "",
                "stderr": job_results.MIN_FALLBACK_STDERR,
                "started_at": payload.get("started_at", "") or "",
                "finished_at": now(), "timed_out": False,
                "output_truncated": True, "files": {},
            }
            try:
                bridge.publish(str(queue / "results"), job_id,
                               bridge.seal(fallback, key))
                return "sealed"
            except bridge.JobExistsError:
                return "skipped"
            except Exception as fallback_error:
                print(f"worker: cannot seal result for {job_id} ({fallback_error})",
                      file=sys.stderr)
                return "failed"
        print(f"worker: cannot seal result for {job_id} ({error})", file=sys.stderr)
        return "failed"
