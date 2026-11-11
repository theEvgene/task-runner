"""Windowed wait, state classification and timings for the LAN bridge.

Builds only on the signed ``lan_bridge`` client: no protocol, MAC, queue or
key change. All waits re-read the SAME job id; this module never publishes,
so recovery after an observer restart cannot duplicate a job. Absence of a
result is reported as ``unknown`` -- never as "running".
"""

from __future__ import annotations

import time

from lan_bridge import (FetchTimeout, JobNotFoundError, _require_queue,
                         _validate_job_id, _validate_key, fetch_result,
                         open_envelope, read)

CONFIRMED_SUCCESS_STATUSES = ("completed", "ok")

UNKNOWN = "unknown"


def peek_result(queue_dir: str, key: bytes, job_id: str) -> dict | None:
    """One verified, non-blocking read: payload, or None when not yet there.

    Tamper or id mismatch raises ProtocolError immediately; absence returns
    None (unknown), never a "running" claim. Missing queue raises BridgeError.
    """
    _validate_job_id(job_id)
    _validate_key(key)
    results_dir = _require_queue(queue_dir)
    try:
        raw = read(results_dir, job_id)
    except JobNotFoundError:
        return None
    return open_envelope(raw, key, "result", job_id)


def classify_result(payload: dict) -> dict:
    """Classify a *verified* result envelope into a confirmed state.

    Returns ``{"outcome", "confirmed", "detail", "action"}``. ``confirmed``
    is always True here (MAC + kind/id already verified); ``outcome`` is one
    of ``success``, ``worker_failure``, ``worker_timeout``, ``truncated`` or
    ``unknown_status``. There is no ``running`` outcome: a verified envelope
    is terminal evidence, anything else is unknown.
    """
    status = payload.get("status")
    exit_code = payload.get("exit_code")
    timed_out = bool(payload.get("timed_out"))
    truncated = bool(payload.get("output_truncated"))
    base = {"confirmed": True, "status": status, "exit_code": exit_code,
            "timed_out": timed_out, "output_truncated": truncated}
    if timed_out:
        return {**base, "outcome": "worker_timeout",
                "detail": "worker reports its own timeout (terminal)",
                "action": "inspect worker limits; re-run as a NEW job id"}
    if truncated:
        return {**base, "outcome": "truncated",
                "detail": "verified envelope, output incomplete (terminal)",
                "action": "re-run as a NEW job id with larger max_output"}
    if status not in CONFIRMED_SUCCESS_STATUSES:
        return {**base, "outcome": "unknown_status",
                "detail": f"verified envelope, unrecognized status={status!r}",
                "action": "inspect worker payload; do not treat as success"}
    if isinstance(exit_code, bool) or exit_code != 0:
        return {**base, "outcome": "worker_failure",
                "detail": f"worker terminal failure exit={exit_code!r}",
                "action": "fix the remote command; re-run as a NEW job id"}
    return {**base, "outcome": "success",
            "detail": "worker terminal success (verified)",
            "action": "consume stdout/files"}


def classify_absence(job_id: str) -> dict:
    """Describe the no-result-yet state: unknown, with the resume action."""
    return {"outcome": UNKNOWN, "confirmed": False, "status": None,
            "detail": "no verified result yet; not evidence the job "
                      "is running, done, or failed",
            "action": f"re-read the SAME id {job_id}; "
                      "never resubmit while unknown"}


def summarize_result(payload: dict) -> dict:
    """Minimal confirmed info sufficient for the agent's next action.

    Contains the verified id/status/exit/flags only. Never includes key
    material (the payload never carries it) and never claims "running".
    """
    info = classify_result(payload)
    return {"id": payload.get("id"), "outcome": info["outcome"],
            "confirmed": True, "status": info["status"],
            "exit_code": info["exit_code"], "timed_out": info["timed_out"],
            "output_truncated": info["output_truncated"],
            "action": info["action"]}


def stage_timings(payload: dict) -> dict:
    """Split queue/exec/delivery only from confirmed numeric marks.

    Worker marks (``started_at``/``finished_at``) are opaque strings in the
    protocol; unless both are finite numbers with finished >= started they
    are reported as ``UNKNOWN`` rather than invented.
    """
    started = payload.get("started_at")
    finished = payload.get("finished_at")
    if (isinstance(started, (int, float)) and not isinstance(started, bool)
            and isinstance(finished, (int, float))
            and not isinstance(finished, bool)
            and finished >= started):
        return {"exec_s": finished - started, "queue_s": UNKNOWN,
                "delivery_s": UNKNOWN,
                "note": "exec from worker marks; "
                        "queue/delivery have no confirmed marks"}
    return {"exec_s": UNKNOWN, "queue_s": UNKNOWN, "delivery_s": UNKNOWN,
            "note": "no confirmed numeric worker marks"}


def wait_many_windows(queue_dir: str, key: bytes, job_id: str,
                      total_timeout: float = 240.0,
                      window_timeout: float = 60.0,
                      poll_seconds: float = 30.0,
                      on_window=None) -> tuple:
    """Wait across bounded windows; resume-safe, never resubmits.

    Each window calls :func:`fetch_result` for the SAME id. Between windows
    (and across observer restarts -- there is no in-memory state, only queue
    files) the job is untouched. ``on_window`` gets at most one small dict
    per window (no per-poll chatter). Returns ``(payload_or_None, report)``;
    ``None`` means ``unknown`` with ``report["action"]`` telling the caller
    to re-read the same id.
    """
    started = time.monotonic()
    windows = 0
    polls = 0
    deadline = started + float(total_timeout)
    while True:
        windows += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        window = min(float(window_timeout), remaining)
        window_started = time.monotonic()
        try:
            payload = fetch_result(queue_dir, key, job_id,
                                   timeout_seconds=window,
                                   poll_seconds=poll_seconds)
        except FetchTimeout:
            payload = None
        polls += int(window // float(poll_seconds)) + 2  # upper bound
        elapsed = time.monotonic() - started
        event = {"window": windows, "window_s": round(
            time.monotonic() - window_started, 1),
            "elapsed_s": round(elapsed, 1),
            "found": payload is not None}
        if on_window is not None:
            on_window(event)
        if payload is not None:
            return payload, {
                "job_id": job_id, "outcome": "found", "windows_used": windows,
                "polls_bound": polls, "elapsed_s": round(elapsed, 1),
                "stages": stage_timings(payload),
                "summary": summarize_result(payload)}
        if time.monotonic() >= deadline:
            break
    elapsed = time.monotonic() - started
    absence = classify_absence(job_id)
    return None, {"job_id": job_id, "outcome": absence["outcome"],
                  "confirmed": False, "windows_used": windows,
                  "polls_bound": polls, "elapsed_s": round(elapsed, 1),
                  "detail": absence["detail"], "action": absence["action"]}


__all__ = ["CONFIRMED_SUCCESS_STATUSES", "UNKNOWN", "classify_absence",
           "classify_result", "peek_result", "stage_timings",
           "summarize_result", "wait_many_windows"]
