#!/usr/bin/env python3
"""Lenovo-side read-only pipeline probe for one MR and one expected SHA.

Runs on the GitLab-reachable side as the single command of a signed job.
It reuses the strict :mod:`gate` read (current-SHA gate plus trace
classification) and binds the result to the canonical MR URL and the one
expected source SHA. Nothing here mutates anything: no retry, no rebase,
no MR change. Every GitLab read is a GET through the injected callable.

Exit codes: 0 means verified ``passed`` for the expected SHA, 1 means
any other receipt, 2 means usage or a bad profile.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from gate import GateInputError, canonical_url, load_profile, observe
except ImportError:  # package-style invocation from the repo root
    from pipeline_observer.gate import (  # type: ignore[no-redef]
        GateInputError, canonical_url, load_profile, observe,
    )

SCHEMA = "pipeline-observer-probe/1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def classify_trace(trace: str | None, job_name: str) -> dict[str, Any]:
    """Classify one failed-job trace without storing log text."""
    if not isinstance(trace, str) or not trace.strip():
        return {"log_available": False, "kind": "unknown-log",
                "test": None, "error": None, "built_in_repeats": False}
    lines = trace.strip().splitlines()
    first_fail = next((ln.strip()[:200] for ln in lines
                       if "FAIL" in ln or "Error" in ln or "error" in ln),
                      lines[0].strip()[:200])
    lowered = trace.lower()
    infra_markers = ("connection refused", "timeout", "tls", "dns",
                     "no space left", "econnreset", "502", "503")
    kind = "undetermined"
    if any(mark in lowered for mark in infra_markers):
        kind = "infrastructure"
    elif "fail" in lowered or "assert" in lowered or "expected" in lowered:
        kind = "test_failure"
    repeats = sum(1 for ln in lines if "FAIL" in ln) > 1
    return {"log_available": True, "kind": kind,
            "test": job_name or None, "error": first_fail,
            "built_in_repeats": bool(repeats)}


def probe(url: str, expected_sha: str, profile: dict[str, Any] | None,
          get: Callable[[str, str], Any],
          read_log: Callable[..., str | None]) -> dict[str, Any]:
    """Run the strict gate read and bind it to the expected MR + SHA."""
    want = (expected_sha or "").lower()
    if not SHA_RE.fullmatch(want):
        raise GateInputError("expected-sha must be 40 lowercase hex")
    canonical = canonical_url(url)
    status = observe(canonical, profile, get)
    observed = status.get("sha")
    base: dict[str, Any] = {
        "schema": SCHEMA, "read_only": True, "mr_url": canonical,
        "expected_sha": want, "observed_sha": observed,
        "status_outcome": status.get("outcome"), "status": status,
        "pipeline_id": status.get("pipeline_id"),
        "pipeline_url": status.get("pipeline_url"),
    }
    outcome = status.get("outcome")
    if not isinstance(observed, str):
        base["verdict"] = outcome if isinstance(outcome, str) else "unavailable"
        base["reason"] = status.get("reason") or f"status_{base['verdict']}"
        for key in ("missing_required_checks", "error"):
            if status.get(key) is not None:
                base[key] = status.get(key)
        return base
    if observed.lower() != want:
        base["verdict"] = "mismatch"
        base["reason"] = "sha_mismatch"
        base["error"] = ("the MR head does not equal the expected SHA; "
                         "prior pipeline evidence is invalidated")
        return base
    if outcome == "passed":
        base["verdict"] = "passed"
        base["reason"] = "gate_passed"
        return base
    base["verdict"] = outcome if isinstance(outcome, str) else "unavailable"
    base["reason"] = status.get("reason") or f"status_{base['verdict']}"
    for key in ("failed_job", "missing_required_checks", "error"):
        if status.get(key) is not None:
            base[key] = status.get(key)
    failed = status.get("failed_job") or {}
    trace = None
    try:
        if failed.get("id") is not None:
            host = canonical.split("/")[2]
            trace = read_log(host, None, failed.get("id"))
    except Exception:
        trace = None
    signature = classify_trace(
        trace if isinstance(trace, str) else None, str(failed.get("name", "")))
    base["diagnosis"] = {
        "log_available": signature["log_available"],
        "cause": signature["kind"],
        "test": signature["test"],
        "error": signature["error"],
        "built_in_repeats": signature["built_in_repeats"],
        "decision": "stop",
        "decision_reason": ("log_unavailable"
                            if not signature["log_available"]
                            else "diagnose_first"),
    }
    if base["verdict"] == "passed":  # defensive: only the gate passes
        base["verdict"] = "unavailable"
        base["reason"] = "unexpected_gate_outcome"
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="probe.py", description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--receipt-out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        from gitlab_get import api_get, live_read_log
    except ImportError as error:
        parser.error(f"live GitLab client unavailable: {error}")
    try:
        profile = load_profile(args.profile)
    except GateInputError as error:
        parser.error(str(error))
    try:
        receipt = probe(args.url, args.expected_sha, profile,
                        get=api_get, read_log=live_read_log)
    except GateInputError as error:
        parser.error(str(error))
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    print(text)
    if args.receipt_out is not None:
        args.receipt_out.parent.mkdir(parents=True, exist_ok=True)
        args.receipt_out.write_text(text + "\n", encoding="utf-8")
    return 0 if receipt.get("verdict") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
