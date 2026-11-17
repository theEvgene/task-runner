"""Windowed wait, state classification and timings for the LAN bridge.

Builds only on the signed ``lan_bridge`` client: no protocol, MAC, queue or
key change. All waits re-read the SAME job id; this module never publishes,
so recovery after an observer restart cannot duplicate a job. Absence of a
result is reported as ``waiting`` only when a signed lease holds the job,
otherwise ``unknown`` -- never as "running".

One verified readback for one job id lives here
(``job_status``/``status_from_result``/``status_for_absence``): the same
``lan-job-status/1`` shape for ``get.py`` and for readiness, with an owner,
a deadline or an occupancy flag only when the queue holds signed evidence
for it.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import job_claim
import lan_bridge as _bridge  # Module handle: attribute access keeps a
# monkeypatched lan_bridge.read/_require_queue visible to get_result.
from lan_bridge import (FetchTimeout, JobNotFoundError, ProtocolError,
                         _require_queue, _validate_job_id, _validate_key,
                         fetch_result, open_envelope, read)

CONFIRMED_SUCCESS_STATUSES = ("completed", "ok")

UNKNOWN = "unknown"

STATUS_SCHEMA = "lan-job-status/1"
LEASE_SCHEMA = job_claim.CLAIM_SCHEMA

FINISHED = "finished"
WAITING = "waiting"
REFUSED = "refused"

SIGNED = "signed"
ABSENT = "absent"
UNVERIFIABLE = "unverifiable"
NOT_READ = "not read"


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
    """Describe the evidence-free no-result state: unknown, with the action.

    This is the baseline without a queue read; :func:`job_status` is the one
    verified readback and its outcome may be ``waiting`` when a signed lease
    holds the job.
    """
    return {"outcome": UNKNOWN, "confirmed": False, "status": None,
            "detail": "no verified result yet; not evidence the job "
                      "is running, done, or failed",
            "action": f"re-read the SAME id {job_id}; "
                      "never resubmit while unknown"}


def _status(job_id: str, outcome: str, next_action: str,
            result: str, job: str = NOT_READ, lease: str = NOT_READ,
            owner: dict | None = None, deadline: dict | None = None,
            terminal: dict | None = None) -> dict:
    """Build the one status shape; optional fields only on observed proof."""
    status = {"schema": STATUS_SCHEMA, "jobId": job_id, "outcome": outcome,
              "nextAction": next_action,
              "proof": {"result": result, "job": job, "lease": lease}}
    if owner is not None:
        status["observedOwner"] = {"instance": owner["instance"],
                                   "pid": owner["pid"]}
        status["occupied"] = True
    if deadline is not None:
        status["deadline"] = dict(deadline)
    if terminal is not None:
        status["terminal"] = dict(terminal)
    return status


def _status_from_info(job_id: str, info: dict) -> dict:
    """The status for a verified result, from its one classification."""
    return _status(job_id, FINISHED, info["action"], SIGNED,
                   terminal={"outcome": info["outcome"],
                             "status": info["status"],
                             "exitCode": info["exit_code"]})


# One wording per not-finished outcome, so a waiter report and its embedded
# status can never state two different job states.
_ABSENCE_DETAIL = {
    WAITING: "a signed lease holds this job; no verified result yet "
             "(ownership, not liveness)",
    UNKNOWN: "no verified result and no signed lease for this id; not "
             "evidence the job is running, done, or failed",
    REFUSED: "the signed request for this id cannot be verified on this "
             "host; the present file is not evidence",
}


def _signed_job(queue_dir: str, key: bytes, job_id: str) -> tuple:
    """The MAC-verified request bound to this id: (payload, digest, proof)."""
    try:
        raw = _bridge.read(str(Path(queue_dir) / "jobs"), job_id)
    except JobNotFoundError:
        return None, None, ABSENT
    try:
        payload = open_envelope(raw, key, "job", job_id)
    except ProtocolError:
        return None, None, UNVERIFIABLE
    return payload, hashlib.sha256(raw).hexdigest(), SIGNED


def _observed_claim(queue_dir: str, key: bytes, job_id: str,
                    job_digest: str | None) -> tuple:
    """The lease owner from a signed claim bound to this exact job.

    Returns ``(owner_or_None, proof)``. The claim must pass the same
    ``job_claim.claim_matches`` predicate the owning worker applies, so this
    reader cannot accept a claim the worker refuses. The worker-derived
    resource set is not re-derivable from the signed job on this host, so the
    observer does not pass one; that last check stays with the owner.
    """
    try:
        raw = _bridge.read(str(Path(queue_dir) / "claims"), job_id)
    except JobNotFoundError:
        return None, ABSENT
    try:
        payload = open_envelope(raw, key, "claim", job_id)
    except ProtocolError:
        return None, UNVERIFIABLE
    if not job_claim.claim_matches(payload, job_id=job_id,
                                   job_envelope_sha256=job_digest):
        return None, UNVERIFIABLE
    owner = payload["owner"]
    return {"instance": owner["instance"], "pid": owner["pid"]}, SIGNED


def _signed_deadline(job: dict | None) -> dict | None:
    """Deadline basis from the signed request; never an invented clock."""
    if job is None:
        return None
    seconds = job.get("timeout_seconds")
    if (isinstance(seconds, int) and not isinstance(seconds, bool)
            and 1 <= seconds <= 3600):
        return {"basis": "signed job timeout_seconds (worker execution limit)",
                "seconds": seconds}
    return None


def status_from_result(payload: dict) -> dict:
    """The finished answer for a MAC-verified result bound to its own id."""
    return _status_from_info(payload.get("id"), classify_result(payload))


def status_for_absence(queue_dir: str, key: bytes, job_id: str) -> dict:
    """The not-finished answer: OWNER/DEADLINE only from signed queue data.

    A signed claim that passes the worker's own claim predicate is occupancy
    evidence, not liveness: it keeps the job owned (``waiting``) and the
    single next action re-reads the SAME id. A request file that fails its
    MAC or id binding is ``refused`` -- an untrusted request is not the
    absent-id ``unknown`` and re-reading it cannot repair it. Nothing is
    published, released or resubmitted here.
    """
    _validate_job_id(job_id)
    _validate_key(key)
    _require_queue(queue_dir)
    if not (Path(queue_dir) / "jobs").is_dir():
        raise _bridge.BridgeError("queue jobs unavailable: check path/mount")
    job, digest, job_proof = _signed_job(queue_dir, key, job_id)
    owner, lease_proof = _observed_claim(queue_dir, key, job_id, digest)
    deadline = _signed_deadline(job)
    if job_proof == UNVERIFIABLE:
        return _status(
            job_id, REFUSED,
            "ask the queue owner for the sealed request of this exact id; "
            "never treat the present file as evidence",
            ABSENT, job_proof, lease_proof)
    if owner is not None:
        return _status(
            job_id, WAITING,
            f"re-read the SAME id {job_id}; never resubmit or release its lease",
            "absent", job_proof, SIGNED, owner=owner, deadline=deadline)
    if job_proof == ABSENT:
        action = (f"check the id {job_id} against the signed request it was "
                  "submitted as; never resubmit or reuse another id")
    else:
        action = (f"re-read the SAME id {job_id}; never resubmit while no "
                  "signed result exists")
    return _status(job_id, UNKNOWN, action, "absent", job_proof, lease_proof,
                   deadline=deadline)


def job_status(queue_dir: str, key: bytes, job_id: str) -> dict:
    """One non-blocking, verified readback for one job id.

    Returns ``lan-job-status/1`` with outcome ``finished``, ``waiting``,
    ``unknown`` or ``refused``. A result file that is present but fails its
    MAC/id/format check is a refusal, never a terminal state; an unavailable
    queue still raises (fail fast, no spin).
    """
    _validate_job_id(job_id)
    _validate_key(key)
    results_dir = _require_queue(queue_dir)
    try:
        raw = _bridge.read(results_dir, job_id)
    except JobNotFoundError:
        return status_for_absence(queue_dir, key, job_id)
    try:
        payload = open_envelope(raw, key, "result", job_id)
    except ProtocolError:
        return _status(
            job_id, REFUSED,
            "ask the queue owner for the sealed result of this exact id; "
            "never treat the present file as evidence",
            UNVERIFIABLE)
    return status_from_result(payload)


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
    ``None`` means not finished: the report's ``outcome`` mirrors its
    ``status.outcome`` (``waiting``, ``unknown`` or ``refused``) and
    ``report["action"]`` is that status's single next action.
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
                "status": status_from_result(payload)}
        if time.monotonic() >= deadline:
            break
    elapsed = time.monotonic() - started
    status = status_for_absence(queue_dir, key, job_id)
    return None, {"job_id": job_id, "outcome": status["outcome"],
                  "confirmed": False, "windows_used": windows,
                  "polls_bound": polls, "elapsed_s": round(elapsed, 1),
                  "detail": _ABSENCE_DETAIL[status["outcome"]],
                  "action": status["nextAction"], "status": status}


def get_result(queue_dir: str, key: bytes, job_id: str,
               window_timeout: float = 60.0,
               poll_seconds: float = 30.0,
               max_windows: int = 0,
               on_window=None) -> tuple:
    """Observe the SAME id across short windows until a confirmed terminal
    result or a concrete blocker.

    Agent-facing get-result: submit (via ``lan_bridge``) returns an id; this
    call keeps reading that id, one short window at a time, until a
    MAC-verified result bound to the id is found or a blocker proves further
    waiting useless. It never publishes, so recovery is stateless (queue
    files only): re-calling with the same id after any interruption resumes
    without duplicating the job. Absence of a result is reported as
    ``waiting`` when a signed lease holds the job, otherwise ``unknown`` --
    never as "running".

    ``max_windows=0`` waits without a window bound; otherwise expiry returns
    ``(None, report)`` whose ``outcome`` mirrors the embedded
    ``status.outcome`` and carries one same-id resume action.
    A present-but-unverifiable result raises immediately (concrete blocker),
    as does an unavailable queue -- validated once, before any waiting, so a
    fail-fast path can never spin. The single queue resolve below (versus one
    per window when re-invoking ``fetch_result``) is the measured
    constant-factor saving; queue reads are counted in ``probes``.

    The queue is immutable, so the first verified result for the id is final:
    every returned payload has ``confirmed`` True, including an unrecognized
    ``status`` (verified envelope, unknown meaning -- never success).
    """
    _validate_job_id(job_id)
    _validate_key(key)
    for name, value in (("window", window_timeout),
                        ("poll", poll_seconds)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _bridge.BridgeError(f"{name} must be a number of seconds")
        if not (value > 0) or value != value or value in (float("inf"),
                                                          float("-inf")):
            raise _bridge.BridgeError(f"{name} must be positive and finite")
    if (isinstance(max_windows, bool) or not isinstance(max_windows, int)
            or max_windows < 0):
        raise _bridge.BridgeError("max_windows must be a non-negative int")
    results_dir = _bridge._require_queue(queue_dir)
    started = time.monotonic()
    windows = 0
    probes = 0
    per_window = int(float(window_timeout) // float(poll_seconds)) + 2
    while True:
        windows += 1
        window_started = time.monotonic()
        window_deadline = window_started + float(window_timeout)
        payload = None
        while True:
            try:
                raw = _bridge.read(results_dir, job_id)
            except JobNotFoundError:
                raw = None
            probes += 1
            if raw is not None:
                # Verifiable now: tamper/id mismatch raises at once (a
                # concrete blocker), never a silent extra window.
                payload = open_envelope(raw, key, "result", job_id)
                break
            remaining = window_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(float(poll_seconds), remaining))
        elapsed = time.monotonic() - started
        event = {"window": windows,
                 "window_s": round(time.monotonic() - window_started, 1),
                 "elapsed_s": round(elapsed, 1),
                 "found": payload is not None,
                 "outcome": "found" if payload is not None else UNKNOWN}
        if on_window is not None:
            on_window(event)
        if payload is not None:
            info = classify_result(payload)
            return payload, {
                "job_id": job_id, "outcome": "found", "confirmed": True,
                "terminal_outcome": info["outcome"],
                "windows_used": windows, "probes": probes,
                "polls_bound": windows * per_window,
                "elapsed_s": round(elapsed, 1),
                "stages": stage_timings(payload),
                "status": status_from_result(payload)}
        if max_windows and windows >= max_windows:
            status = status_for_absence(queue_dir, key, job_id)
            return None, {
                "job_id": job_id, "outcome": status["outcome"],
                "confirmed": False, "windows_used": windows,
                "probes": probes, "polls_bound": windows * per_window,
                "elapsed_s": round(elapsed, 1),
                "detail": _ABSENCE_DETAIL[status["outcome"]],
                "action": status["nextAction"], "status": status}


__all__ = ["ABSENT", "CONFIRMED_SUCCESS_STATUSES", "FINISHED", "LEASE_SCHEMA",
           "NOT_READ", "REFUSED", "SIGNED", "STATUS_SCHEMA", "UNKNOWN",
           "UNVERIFIABLE", "WAITING", "classify_absence", "classify_result",
           "get_result", "job_status", "peek_result", "stage_timings",
           "status_for_absence", "status_from_result", "wait_many_windows"]
