#!/usr/bin/env python3
"""Run exactly one bounded, host-bound selected check and print one receipt.

The ``lan-selected-check/1`` descriptor (outside every checkout) names one
host, one exact argv, one cwd, one expected checkout revision and a declared
read-only effect.  Host ``dell`` runs one local child after a local preflight.
Host ``lenovo`` first requires the existing MAC-verified readiness evidence
for the same python/cwd/tool, then publishes exactly ONE signed job through
the existing queue.  That job's fixed preamble re-verifies cwd, tool and
revision on the target host and refuses before running the check; its real
child exit is the result.  A bounded wait that expires is UNKNOWN and is
never resubmitted.

This is not a second runner: it reuses ``readiness.check`` for the Lenovo
environment model and ``lan_bridge`` for the only transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parent / "lan_bridge"
sys.path[:0] = [str(item) for item in (BRIDGE_DIR, HERE)
                if str(item) not in sys.path]

import lan_bridge as bridge  # noqa: E402
import syntax  # noqa: E402
from selected_hosts import (execute_dell, execute_lenovo, preflight_dell,  # noqa: E402
                            preflight_lenovo, resume_lenovo)
from selected_model import (NEXT_ACTIONS, RECEIPT_SCHEMA, Refusal,  # noqa: E402
                            inside, read_object, validate_descriptor)


def _digest(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decide(requested: str, preflight: dict, execution: dict | None) -> str:
    if preflight["state"] != "confirmed" or execution is None:
        return "NOT_RUN"
    if (not execution["verified"] or execution["observedHost"] != requested
            or execution.get("hostEvidence") is None):
        return "UNKNOWN"
    if execution["state"] == "unknown":
        return "UNKNOWN"
    if execution["revisionState"] != "confirmed":
        return "NOT_RUN" if execution["state"] == "not_run" else "FAIL"
    if execution["state"] == "not_run":
        return "NOT_RUN"
    if execution["timedOut"] or execution["truncated"]:
        return "FAIL"
    if execution["state"] == "fail":
        return "FAIL"
    if (execution["state"] == "run"
            and type(execution["exitCode"]) is int
            and execution["exitCode"] == 0):
        return "PASS"
    return "UNKNOWN"


def _next_action(outcome: str, preflight: dict, execution: dict | None) -> str:
    if outcome == "PASS":
        return "none"
    if outcome == "FAIL":
        return ("inspect the selected check; a nonzero or incomplete exit "
                "is not a pass")
    if outcome == "UNKNOWN":
        job_id = execution["jobId"] if execution else None
        if job_id and execution["reason"] in ("worker-refused", "invalid-result"):
            return (f"ask the queue owner to inspect the result for id {job_id}; "
                    "never resubmit while unknown")
        if job_id:
            return (f"re-read the SAME id {job_id} with "
                    "scripts/readiness/selected.py --job-id; never resubmit "
                    "while unknown")
        return "stop and report the unknown selected-check state to the owner"
    reason = (execution["reason"] if execution
              and execution["state"] == "not_run" else preflight["reason"])
    return NEXT_ACTIONS.get(reason, "fix the refused preflight condition; "
                                     "nothing was executed")


def _revision_source(host: str, preflight: dict,
                     execution: dict | None) -> str:
    if host == "dell":
        return "local-git"
    if execution is not None and execution.get("observedRevision") is not None:
        return "signed-target-preamble"
    if preflight.get("reason") in ("revision-stale", "revision-unconfirmed",
                                   "evidence-untrusted"):
        return "readiness-evidence"
    return "signed-target-preamble"


def build_receipt(desc: dict, preflight: dict,
                  execution: dict | None) -> dict:
    check = desc["check"]
    host = check["host"]
    outcome = decide(host, preflight, execution)
    observed_host = execution["observedHost"] if execution else None
    host_evidence = execution["hostEvidence"] if execution else None
    if execution is not None and execution["revisionState"]:
        revision_state = execution["revisionState"]
        observed_revision = execution["observedRevision"]
    elif preflight["reason"] == "revision-stale":
        revision_state = "stale"
        observed_revision = preflight.get("observed")
    elif preflight["state"] == "confirmed" and preflight.get("observed"):
        revision_state = "confirmed"
        observed_revision = preflight.get("observed")
    else:
        revision_state = "unconfirmed"
        observed_revision = preflight.get("observed")
    return {
        "schema": RECEIPT_SCHEMA,
        "check": {"name": check["name"], "effect": check["effect"],
                  "commandDigest": _digest(check["argv"]),
                  "argumentCount": len(check["argv"]),
                  "cwdDigest": _digest(check["cwd"])},
        "requestedHost": host,
        "observedHost": observed_host,
        "hostBound": (bool(execution and execution["verified"])
                      and observed_host == host and host_evidence is not None),
        "hostEvidence": host_evidence,
        "outcome": outcome,
        "revision": {"expected": check["expectedRevision"],
                     "observed": observed_revision,
                     "state": revision_state,
                     "source": _revision_source(host, preflight, execution)},
        "preflight": {"state": preflight["state"],
                      "reason": preflight["reason"]},
        "execution": {
            "kind": (execution["kind"] if execution else
                     "local-child" if host == "dell" else "signed-queue"),
            "jobId": execution["jobId"] if execution else None,
            "verified": bool(execution and execution["verified"]),
            "exitCode": execution["exitCode"] if execution else None,
            "timedOut": bool(execution and execution["timedOut"]),
            "outputTruncated": bool(execution and execution["truncated"]),
            "reason": execution["reason"] if execution else "preflight-refused",
        },
        "nextAction": _next_action(outcome, preflight, execution),
    }


def run(args: argparse.Namespace) -> int:
    descriptor_path = Path(args.descriptor).resolve()
    repo_root = bridge.find_repo_root()
    if repo_root is not None and inside(descriptor_path, repo_root):
        raise Refusal("descriptor must stay outside the checkout")
    out = None
    if args.receipt_out:
        out = Path(args.receipt_out).resolve()
        if repo_root is not None and inside(out, repo_root):
            raise Refusal("receipt must stay outside the checkout")
    desc = validate_descriptor(read_object(descriptor_path))
    if desc["check"]["host"] == "dell":
        if args.wait is not None or args.poll is not None:
            raise Refusal("--wait/--poll apply only to a lenovo descriptor")
        if args.job_id is not None:
            raise Refusal("--job-id applies only to a lenovo descriptor")
        preflight = preflight_dell(desc)
        execution = execute_dell(desc) if preflight["state"] == "confirmed" else None
    else:
        if args.wait is not None:
            desc["lenovo"]["waitSeconds"] = float(args.wait)
        if args.poll is not None:
            desc["lenovo"]["pollSeconds"] = float(args.poll)
        for name in ("waitSeconds", "pollSeconds"):
            value = desc["lenovo"][name]
            if value <= 0 or not math.isfinite(value):
                raise Refusal(f"{name} override must be a positive finite number of seconds")
        if desc["lenovo"]["pollSeconds"] > desc["lenovo"]["waitSeconds"]:
            raise Refusal("poll override must not exceed the wait")
        if args.job_id is not None:
            if (len(args.job_id) != 32
                    or any(char not in "0123456789abcdef"
                           for char in args.job_id)):
                raise Refusal("--job-id must be 32 lowercase hex characters")
            preflight, key = resume_lenovo(desc, args.job_id)
        else:
            preflight, key = preflight_lenovo(desc)
        execution = (execute_lenovo(desc, key, args.job_id)
                     if preflight["state"] == "confirmed" else None)
    receipt = build_receipt(desc, preflight, execution)
    text = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    if receipt["outcome"] == "PASS":
        return 0
    return 3 if receipt["outcome"] == "UNKNOWN" else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run one bounded host-bound selected check")
    result.add_argument("--descriptor", required=True,
                        help="owner-provided JSON outside the checkout")
    result.add_argument("--receipt-out", default=None,
                        help="optional receipt path outside the checkout")
    result.add_argument("--wait", type=float, default=None,
                        help="override the lenovo result wait in seconds")
    result.add_argument("--poll", type=float, default=None,
                        help="override the lenovo poll interval in seconds")
    result.add_argument("--job-id", default=None,
                        help="re-read this existing lenovo job id; never submit another")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (Refusal, bridge.BridgeError, syntax.SyntaxGateError,
            ValueError, TypeError) as error:
        print(f"selected check refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
