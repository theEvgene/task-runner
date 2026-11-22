"""Read-only Lenovo queue/worker snapshot (stdlib only).

Builds one JSON-serializable screen model from the existing signed queue
(``jobs/``, ``results/``, ``claims/``, ``starts/``) and the launcher state
dir (``worker.pid``/``cleaner.pid``). Never publishes, seals, deletes, kills
or retries anything: every read is a plain file read plus MAC verification.

Truthfulness rules (same as the bridge ``wait`` reader):

- a signed claim is ownership evidence only, never liveness: active jobs are
  reported as ``waiting`` with ``confirmed: false``;
- no verified result and no signed lease is ``unknown``, never healthy;
- a present-but-unverifiable file is ``refused``, never a terminal state;
- a missing/unreadable queue is ``connection: lost``, never healthy;
- a worker pid counts as up only with a matching command line; an alive pid
  with an unprovable command line is ``unknown``;
- the snapshot carries the watched queue path, job ids, states, counts and
  timings only: no command, working directory, command output, return-file
  content, key, password or host name ever appears in output or logs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_SCHEMA = "lenovo-queue-status/1"
CONFIRMED_OK_STATUSES = ("completed", "ok")
_JOB_ID_CHARS = set("0123456789abcdef")


def _load_sibling(name: str, filename: str):
    path = Path(__file__).resolve().parent.parent / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_bridge = _load_sibling("lenovo_screen_bridge", "lan_bridge/lan_bridge.py")
_claim = _load_sibling("lenovo_screen_claim", "lan_bridge/job_claim.py")
_proc = _load_sibling("lenovo_screen_proc", "lenovo_worker/proc.py")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid_id(name: str) -> bool:
    return (len(name) == 32 and set(name) <= _JOB_ID_CHARS
            and name not in (".", ".."))


def _list_ids(directory: Path) -> tuple[list[str], str | None]:
    """Sorted job ids in a queue subdir, plus an error note when unreadable."""
    try:
        names = sorted(path.name for path in directory.iterdir()
                       if path.is_file() and path.name.endswith(".json"))
    except OSError as error:
        return [], f"unreadable ({error})"
    return [name[:-5] for name in names if _valid_id(name[:-5])], None


def _read_verified(subdir: Path, job_id: str, key: bytes,
                   kind: str) -> tuple:
    """(payload_or_None, proof) with proof in signed/absent/unverifiable."""
    try:
        raw = _bridge.read(str(subdir), job_id)
    except Exception:
        return None, "absent"
    try:
        return _bridge.open_envelope(raw, key, kind, job_id), "signed"
    except Exception:
        return None, "unverifiable"


def _deadline_s(job: dict | None) -> dict | None:
    """Enforced execution budget from the signed request (capped at 600 s)."""
    if job is None:
        return None
    seconds = job.get("timeout_seconds")
    if (isinstance(seconds, int) and not isinstance(seconds, bool)
            and 1 <= seconds <= 3600):
        effective = min(seconds, 600)
        return {"seconds": effective, "requested_seconds": seconds}
    return None


def _result_terminal(payload: dict) -> dict:
    timed_out = bool(payload.get("timed_out"))
    truncated = bool(payload.get("output_truncated"))
    status = payload.get("status")
    exit_code = payload.get("exit_code")
    if timed_out:
        outcome = "worker_timeout"
    elif truncated:
        outcome = "truncated"
    elif status not in CONFIRMED_OK_STATUSES:
        outcome = "unknown_status"
    elif isinstance(exit_code, bool) or exit_code != 0:
        outcome = "worker_failure"
    else:
        outcome = "success"
    return {"outcome": outcome, "status": status, "exitCode": exit_code,
            "timedOut": timed_out, "truncated": truncated}


def _age_s(claimed_at: object, now_epoch: float) -> float | None:
    if not isinstance(claimed_at, str) or not claimed_at:
        return None
    try:
        moment = datetime.fromisoformat(claimed_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return max(0.0, now_epoch - moment.timestamp())
    except (OSError, OverflowError, ValueError):
        return None


def _role_state(state_dir: Path | None, role: str,
                marker: str) -> dict:
    """One launcher role: up only with a matching command line.

    Anything else is stopped (no/stale record) or unknown (ambiguous), never
    a guess. Mirrors the launcher's own refusal vocabulary.
    """
    if state_dir is None:
        return {"role": role, "state": "unknown",
                "detail": "state dir not configured: role unproven"}
    if (state_dir / f"{role}.starting").exists():
        return {"role": role, "state": "unknown",
                "detail": f"incomplete start marker {role}.starting present: "
                          "verify before touching state"}
    try:
        existing = _proc.read_pid_file(state_dir / f"{role}.pid")
    except (ValueError, OSError):
        return {"role": role, "state": "unknown",
                "detail": f"ambiguous {role} record (corrupt pid file): "
                          f"verify, then remove stateDir/{role}.pid"}
    except Exception:
        return {"role": role, "state": "unknown",
                "detail": f"state unreadable for {role}: role unproven"}
    if existing is None:
        return {"role": role, "state": "stopped",
                "detail": "no pid record: role not started here"}
    pid = existing.get("pid")
    alive = _proc.is_alive(pid) if isinstance(pid, int) else False
    if not alive:
        return {"role": role, "state": "stopped", "pid": pid,
                "detail": f"stale record (pid {pid} dead): role not active"}
    line = _proc.cmdline(pid)
    if not line:
        return {"role": role, "state": "unknown", "pid": pid,
                "detail": f"pid {pid} alive but command line unprovable: "
                          "role unconfirmed"}
    if marker and marker not in line:
        return {"role": role, "state": "unknown", "pid": pid,
                "detail": f"pid {pid} runs a foreign command: refusing to "
                          "adopt it as this role"}
    return {"role": role, "state": "up", "pid": pid,
            "detail": f"pid {pid} alive with a matching command line"}


def _classify_job(queue: Path, job_id: str, key: bytes,
                  now_epoch: float) -> dict:
    """One job entry: finished/waiting/unknown/refused, never liveness."""
    job, job_proof = _read_verified(queue / "jobs", job_id, key, "job")
    result, result_proof = _read_verified(
        queue / "results", job_id, key, "result")
    if result_proof == "unverifiable":
        return {"id": job_id, "outcome": "refused", "confirmed": False,
                "detail": "result file present but unverifiable: not "
                          "evidence of any terminal state",
                "proof": {"result": "unverifiable", "job": job_proof,
                          "lease": "not read"}}
    if result is not None:
        terminal = _result_terminal(result)
        return {"id": job_id, "outcome": "finished", "confirmed": True,
                "terminal": terminal,
                "detail": f"verified terminal result ({terminal['outcome']})",
                "proof": {"result": "signed", "job": job_proof,
                          "lease": "not read"}}
    if job_proof == "unverifiable":
        return {"id": job_id, "outcome": "refused", "confirmed": False,
                "detail": "signed request unverifiable on this host: the "
                          "present file is not evidence",
                "proof": {"result": "absent", "job": "unverifiable",
                          "lease": "not read"}}
    if job is None:
        return {"id": job_id, "outcome": "unknown", "confirmed": False,
                "detail": "no verified result and no signed request for this "
                          "id: not evidence of any state",
                "proof": {"result": "absent", "job": "absent",
                          "lease": "not read"}}
    digest = hashlib.sha256(
        _bridge.read(str(queue / "jobs"), job_id)).hexdigest()
    claim, lease_proof = _read_verified(
        queue / "claims", job_id, key, "claim")
    owner = None
    waiting = (claim is not None and lease_proof == "signed"
               and _claim.claim_matches(claim, job_id=job_id,
                                        job_envelope_sha256=digest))
    if waiting:
        raw_owner = claim.get("owner") or {}
        owner = {"instance": raw_owner.get("instance"),
                 "pid": raw_owner.get("pid")}
    deadline = _deadline_s(job)
    entry = {"id": job_id, "confirmed": False,
             "proof": {"result": "absent", "job": "signed",
                       "lease": ("signed" if waiting
                                 else lease_proof)},
             "deadlineS": (deadline["seconds"] if deadline else None)}
    if not waiting:
        entry.update({
            "outcome": "unknown",
            "detail": "signed request without a signed lease and without a "
                      "verified result: not evidence the job is held, done "
                      "or failed; re-read the SAME id, never resubmit it",
        })
        return entry
    generation = claim.get("generation")
    start, _ = _read_verified(queue / "starts", job_id, key, "start")
    start_ok = (
        isinstance(start, dict)
        and start.get("generation") == generation
        and isinstance(start.get("owner"), dict)
        and start["owner"].get("instance") == owner["instance"]
        and start["owner"].get("pid") == owner["pid"]
        and isinstance(start.get("child_pid"), int)
        and not isinstance(start.get("child_pid"), bool)
        and start["child_pid"] > 0)
    alive = _proc.is_alive(owner["pid"])
    claimed_at = claim.get("claimed_at")
    entry.update({
        "outcome": "waiting",
        "observedOwner": owner,
        "ownerAlive": alive,
        "startConfirmed": start_ok,
        "claimedAt": claimed_at if isinstance(claimed_at, str) else None,
        "ageS": _age_s(claimed_at, now_epoch),
        "detail": "a signed lease holds this job (ownership, not liveness)"
                  + ("" if start_ok else "; start record unconfirmed")
                  + ("" if alive else "; owner process not observed alive"
                     + " (outcome still unknown, never success)"),
    })
    return entry


def build_snapshot(queue: Path | str, key: bytes,
                   state_dir: Path | str | None = None,
                   recent_limit: int = 10,
                   now_epoch: float | None = None) -> dict:
    """Assemble the whole screen model without touching the queue's state."""
    queue = Path(queue)
    state_path = Path(state_dir) if state_dir is not None else None
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    snapshot: dict = {"schema": SNAPSHOT_SCHEMA, "at": _now_iso(),
                      "queue": os.fspath(queue)}
    missing = [name for name in ("jobs", "results")
               if not (queue / name).is_dir()]
    if not queue.is_dir() or missing:
        snapshot["connection"] = {
            "state": "lost",
            "detail": ("queue unreachable: "
                       + (f"{queue} is not a directory" if not queue.is_dir()
                          else f"missing: {', '.join(missing)}")
                       + "; nothing below is health evidence")}
        snapshot.update({"worker": _role_state(state_path, "worker",
                                              "worker.py"),
                         "cleaner": _role_state(state_path, "cleaner",
                                               "cleaner.py"),
                         "counts": {"active": 0, "pending": 0,
                                    "finished": 0, "refused": 0,
                                    "unknown": 0},
                         "active": [], "pending": [], "recent": [],
                         "recentNote": "queue unreadable: no recent data"})
        return snapshot
    snapshot["connection"] = {"state": "ok",
                              "detail": "queue dirs readable; every job "
                                        "below is MAC-verified"}
    job_ids, jobs_note = _list_ids(queue / "jobs")
    result_ids, results_note = _list_ids(queue / "results")
    result_set = set(result_ids)
    active, refused, unknown = [], [], []
    for job_id in job_ids:
        if job_id in result_set:
            continue
        try:
            entry = _classify_job(queue, job_id, key, now_epoch)
        except Exception as error:
            entry = {"id": job_id, "outcome": "unknown", "confirmed": False,
                     "detail": "job could not be verified on this read "
                               f"({error}): not evidence of any state",
                     "proof": {"result": "not read", "job": "not read",
                               "lease": "not read"}}
            unknown.append(entry)
            continue
        ({"waiting": active, "unknown": unknown,
          "refused": refused}.get(entry["outcome"], unknown)).append(entry)
    pending_ids = sorted(job_id for job_id in job_ids
                         if job_id not in result_set)
    counts = {"active": len(active), "pending": len(pending_ids),
              "finished": len(result_ids), "refused": len(refused),
              "unknown": len(unknown)}
    recent = []
    try:
        files = sorted((queue / "results").glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
        results_note = (results_note or "") + "; results unreadable"
    for path in files[:max(0, int(recent_limit))]:
        job_id = path.name[:-5]
        if not _valid_id(job_id):
            continue
        payload, proof = _read_verified(
            queue / "results", job_id, key, "result")
        if payload is None:
            recent.append({"id": job_id, "outcome": "refused",
                           "confirmed": False,
                           "detail": "result file unverifiable: not "
                                     "evidence of any terminal state"})
            continue
        terminal = _result_terminal(payload)
        recent.append({"id": job_id, "outcome": terminal["outcome"],
                       "confirmed": True, "terminal": terminal})
    if jobs_note or results_note:
        snapshot["listNote"] = "; ".join(
            note for note in (jobs_note, results_note) if note)
    snapshot.update({
        "worker": _role_state(state_path, "worker", "worker.py"),
        "cleaner": _role_state(state_path, "cleaner", "cleaner.py"),
        "counts": counts,
        "active": sorted(active, key=lambda e: e["id"]),
        "pending": [e for e in sorted(
            unknown + refused, key=lambda e: e["id"])],
        "recent": recent})
    return snapshot


def render_text(snapshot: dict, interval_s: object = None) -> str:
    """One human screen: states and counts only, no secrets, no liveness."""
    lines = []
    title = f"Lenovo queue screen @ {snapshot.get('at', '?')}"
    if interval_s is not None:
        title += f" (refresh every {interval_s}s; Ctrl+C to stop)"
    lines += [title, "=" * len(title), ""]
    conn = snapshot.get("connection", {})
    lines.append(f"Queue: {snapshot.get('queue', '?')} -- "
                 f"{str(conn.get('state', '?')).upper()}: "
                 f"{conn.get('detail', '')}")
    for role in ("worker", "cleaner"):
        info = snapshot.get(role, {})
        pid = f" pid {info['pid']}" if "pid" in info else ""
        lines.append(f"{role.capitalize()}: {info.get('state', '?')}{pid} "
                     f"-- {info.get('detail', '')}")
    counts = snapshot.get("counts", {})
    lines += ["",
              f"Active: {counts.get('active', 0)} | Pending ids: "
              f"{counts.get('pending', 0)} | Finished results: "
              f"{counts.get('finished', 0)} | Refused: "
              f"{counts.get('refused', 0)} | Unknown: "
              f"{counts.get('unknown', 0)}",
              ""]
    actives = snapshot.get("active", [])
    if actives:
        lines.append("Active jobs (lease-held; ownership, NOT liveness):")
        for entry in actives:
            owner = entry.get("observedOwner", {})
            start = ("start confirmed" if entry.get("startConfirmed")
                     else "start UNCONFIRMED")
            alive = ("owner observed alive"
                     if entry.get("ownerAlive") else "owner NOT observed "
                     "alive (outcome still unknown)")
            age = (f", age {entry['ageS']:.0f}s" if entry.get("ageS")
                   is not None else "")
            deadline = (f", budget {entry['deadlineS']}s"
                        if entry.get("deadlineS") is not None else "")
            lines.append(f"  {entry['id']} owner={owner.get('instance')}/"
                         f"{owner.get('pid')} {start}; {alive}{age}"
                         f"{deadline}")
    else:
        lines.append("Active jobs: none observed.")
    unknowns = [e for e in snapshot.get("pending", [])
                if e.get("outcome") == "unknown"]
    if unknowns:
        lines.append("Awaiting lease (signed request, no lease, no result):")
        lines += [f"  {e['id']}" for e in unknowns]
    refused = ([e for e in snapshot.get("pending", [])
                if e.get("outcome") == "refused"]
               + [e for e in snapshot.get("recent", [])
                  if e.get("outcome") == "refused"])
    if refused:
        lines.append("Refused (present but unverifiable; not evidence):")
        lines += [f"  {e['id']} -- {e.get('detail', '')}" for e in refused]
    recent = [e for e in snapshot.get("recent", [])
              if e.get("outcome") != "refused"]
    if recent:
        lines.append("Recent verified results:")
        for entry in recent:
            term = entry.get("terminal", {})
            lines.append(f"  {entry['id']} {entry['outcome']} "
                         f"(status={term.get('status')} "
                         f"exit={term.get('exitCode')} "
                         f"timed_out={term.get('timedOut')})")
    else:
        lines.append("Recent verified results: none.")
    if snapshot.get("recentNote"):
        lines.append(snapshot["recentNote"])
    if snapshot.get("listNote"):
        lines.append(snapshot["listNote"])
    lines += ["",
              "Legend: waiting = signed lease holds the job (ownership, not "
              "liveness); unknown = no verified result/lease; refused = "
              "unverifiable file; finished = verified result only; "
              "connection lost = queue unreadable, nothing is healthy; "
              "unconfirmed start/owner = active job whose execution cannot "
              "be proven.",
              "This screen changes nothing: no stop, no restart, no retry, "
              "no recovery."]
    return "\n".join(lines) + "\n"


def snapshot_json(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2,
                      sort_keys=True)
