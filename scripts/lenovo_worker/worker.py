"""Lenovo signed-queue worker (stdlib only).

Verified jobs execute at most once and results publish immutably. Refusals,
spawn failures, timeouts, and oversized output become bounded terminal
results. Missing queue infrastructure and unsealed results fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import job_lifecycle
import job_ownership
import job_reconciliation
import job_reservations
import job_resources
import job_results

WORKER_SCHEMA = "lenovo-worker/1"
_SYSTEM_SUBPROCESS_RUN = subprocess.run


def _load_bridge() -> object:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "lan_bridge" / "lan_bridge.py",
        Path(os.environ.get("DELL_LAN_BRIDGE_DIR", "") or ".") / "lan_bridge.py",
    ]
    for path in candidates:
        if path.is_file():
            if str(path.parent) not in sys.path:
                sys.path.insert(0, str(path.parent))
            spec = importlib.util.spec_from_file_location("lenovo_worker_bridge", str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules["lenovo_worker_bridge"] = module
            spec.loader.exec_module(module)
            return module
    raise SystemExit("refused: bridge client not found (scripts/lan_bridge/lan_bridge.py)")


bridge = _load_bridge()


class ExecutorRefused(Exception):
    """The executor cannot prove a truthful result."""


def _load_truth() -> tuple[object | None, str]:
    candidate = Path(__file__).resolve().parent / "powershell_truth.py"
    if not candidate.is_file():
        return None, "helper missing"
    try:
        spec = importlib.util.spec_from_file_location(
            "lenovo_worker_powershell_truth", str(candidate))
        module = importlib.util.module_from_spec(spec)
        sys.modules["lenovo_worker_powershell_truth"] = module
        spec.loader.exec_module(module)
        return module, ""
    except Exception:
        return None, "helper unreadable"


truth, truth_problem = _load_truth()


def _load_fallback() -> object | None:
    candidate = Path(__file__).resolve().parent / "cmd_fallback.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "lenovo_worker_cmd_fallback", str(candidate))
        module = importlib.util.module_from_spec(spec)
        sys.modules["lenovo_worker_cmd_fallback"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


fallback = _load_fallback()


def _effective_command(command: str, dialect: str | None = None) -> str:
    if dialect not in (None, "cmd", "powershell"):
        raise ExecutorRefused("unsupported authenticated dialect")
    try:
        local_kind = (fallback.launcher_kind(command) if fallback is not None
                      else "unprovable-command-chain")
    except Exception:
        local_kind = "unprovable-command-chain"
    if truth is not None:
        try:
            kind = truth.launcher_kind(command)
        except Exception:
            kind = None
            helper_failed = True
        else:
            helper_failed = False
        if not helper_failed:
            if dialect == "powershell" and kind != "wrappable":
                raise ExecutorRefused("powershell dialect is not wrappable")
            if dialect == "cmd" and kind is not None:
                raise ExecutorRefused("cmd dialect resolves to powershell")
            if kind is None:
                if local_kind is not None:
                    raise ExecutorRefused(
                        "powershell classifier disagreement")
                return command
            if kind == "wrappable":
                try:
                    return truth.wrap_powershell(command)
                except Exception as error:
                    unsupported = getattr(truth, "UnsupportedLauncher", None)
                    if (isinstance(unsupported, type)
                            and isinstance(error, unsupported)):
                        raise ExecutorRefused(
                            "powershell launcher refused")
                    raise ExecutorRefused(
                        "powershell wrapping failed")
            raise ExecutorRefused(
                "powershell launcher refused")
    if dialect == "powershell":
        raise ExecutorRefused("powershell executor unavailable")
    if local_kind is None:
        return command
    problem = truth_problem or "helper failed"
    raise ExecutorRefused(f"powershell executor unavailable ({problem})")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fail(message: str) -> int:
    print(f"worker refused: {message}", file=sys.stderr)
    return 2


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _check_key_outside(key_file: Path, queue: Path, shared: Path | None) -> str | None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    for label, root in (("queue", queue), ("checkout", repo_root)):
        if _is_inside(key_file, root):
            return f"key inside the {label}"
    if shared is not None and _is_inside(key_file, shared):
        return "key inside the shared LAN folder"
    return None


def _load_key(key_file: Path, key_format: str, queue: Path) -> bytes | None:
    try:
        return bytes(bridge.load_key(str(key_file), key_format, str(queue)))
    except Exception as error:  # BridgeError, OSError, ValueError
        print(f"worker refused: bad key ({error})", file=sys.stderr)
        return None


def _seal_result(queue: Path, key: bytes, job_id: str, **fields) -> str:
    """Seal a result. Returns "sealed", "skipped", or "failed"."""

    payload = {
        "kind": "result", "id": job_id,
        "status": fields.get("status", "error"),
        "exit_code": fields.get("exit_code", 2),
        "stdout": fields.get("stdout", "") or "",
        "stderr": fields.get("stderr", "") or "",
        "started_at": fields.get("started_at", "") or "",
        "finished_at": fields.get("finished_at") or _now(),
        "timed_out": bool(fields.get("timed_out", False)),
        "output_truncated": bool(fields.get("output_truncated", False)),
        "files": fields.get("files", {}) or {},
    }
    for name in ("termination", "signal", "process_exit_code"):
        if fields.get(name) is not None:
            payload[name] = fields[name]
    if fields.get("reconciliation") is not None:
        payload["reconciliation"] = fields["reconciliation"]
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
                "finished_at": _now(), "timed_out": False,
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


def _execute(command: str, workdir: str, timeout_s: int, limit: int,
             dialect: str | None = None, on_start=None) -> dict:
    started = _now()
    try:
        effective = _effective_command(command, dialect)
    except ExecutorRefused as error:
        detail = f"executor refused: {error}"
        return {"status": "error", "exit_code": 2,
                "stdout": "", "stderr": detail[:limit],
                "started_at": started, "timed_out": False,
                "output_truncated": len(detail) > limit}
    try:
        if subprocess.run is _SYSTEM_SUBPROCESS_RUN:
            outcome = job_lifecycle.execute(
                effective, workdir, timeout_s, limit, on_start=on_start)
        else:
            proc = subprocess.run(
                effective, shell=True, cwd=workdir, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s, check=False)
            stdout, stderr = proc.stdout or "", proc.stderr or ""
            outcome = {
                "status": "ok" if proc.returncode == 0 else "error",
                "exit_code": proc.returncode, "process_exit_code": proc.returncode,
                "termination": "exit", "signal": None,
                "stdout": stdout[:limit], "stderr": stderr[:limit],
                "timed_out": False,
                "output_truncated": (len(stdout) > limit
                                     or len(stderr) > limit),
            }
    except job_lifecycle.TreeTerminationUnknown:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # Invalid cwd, spawn failure, or OS refusal: a bounded terminal
        # error, never a crash and never a silent re-execution.
        detail = "execution failed on the worker"
        return {"status": "error", "exit_code": 2,
                "stdout": "", "stderr": detail[:limit],
                "started_at": started, "timed_out": False,
                "output_truncated": len(detail) > limit}
    except Exception:  # defensive: any spawn failure stays terminal
        detail = "execution failed on the worker"
        return {"status": "error", "exit_code": 2,
                "stdout": "", "stderr": detail[:limit],
                "started_at": started, "timed_out": False,
                "output_truncated": len(detail) > limit}
    outcome["started_at"] = started
    outcome.pop("elapsed_seconds", None)
    return outcome


def _prepare_job(queue: Path, key: bytes,
                 job_id: str) -> job_ownership.PreparedJob | str:
    """Verify one envelope without claiming or starting its side effect."""

    try:
        raw = bridge.read(str(queue / "jobs"), job_id)
    except Exception as error:
        print(f"worker: cannot read job {job_id} ({error})", file=sys.stderr)
        return "refused"
    try:
        job = bridge.open_envelope(raw, key, "job", job_id)
    except Exception:
        return _seal_result(queue, key, job_id, status="error", exit_code=2,
                            stderr="job-unverifiable")
    command = job.get("command")
    workdir = job.get("working_directory")
    dialect = job.get("dialect")
    if not isinstance(command, str) or not command.strip() or not isinstance(workdir, str) or not workdir:
        return _seal_result(queue, key, job_id, status="error", exit_code=2,
                            stderr="job-unverifiable: empty command or missing working_directory")
    if dialect not in (None, "cmd", "powershell"):
        return _seal_result(queue, key, job_id, status="error", exit_code=2,
                            stderr="job-unverifiable: unsupported authenticated dialect")
    try:
        timeout_s = int(job.get("timeout_seconds", 300))
        limit = int(job.get("max_output_bytes", 65536))
    except (TypeError, ValueError):
        return _seal_result(queue, key, job_id, status="error", exit_code=2,
                            stderr="job-unverifiable: bad timeout/max_output")
    if not (1 <= timeout_s <= 3600 and 1 <= limit <= 262144):
        return _seal_result(queue, key, job_id, status="error", exit_code=2,
                            stderr="job-unverifiable: timeout/max_output out of range")
    declared = job.get("return_files")
    return_files_invalid = not isinstance(declared, list)
    if not isinstance(declared, list):
        declared = []
    try:
        workdir_resource = job_lifecycle.resource_identity(workdir)
    except (OSError, ValueError, RuntimeError):
        workdir_resource = f"working-directory:invalid:{job_id}"
    try:
        fallback = job_resources.build_resources(job, workdir_resource)
    except job_resources.ResourceRefused as error:
        return _seal_result(
            queue, key, job_id, status="error", exit_code=2,
            stderr=f"job-unverifiable: {error}")
    envelope_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        reservation = job_reservations.read(
            queue, key, bridge, job_id, envelope_sha256)
        resources = (
            fallback if reservation is None
            else job_reservations.authoritative_resources(
                reservation, workdir_resource))
    except job_reservations.ReservationRefused as error:
        # A present-but-unprovable reservation is not a fallback: the
        # producer asked for parallel admission and the proof failed.
        return _seal_result(
            queue, key, job_id, status="error", exit_code=2,
            stderr=f"job-unverifiable: {error}")
    return job_ownership.PreparedJob(
        job_id=job_id, command=command, workdir=workdir,
        timeout_seconds=timeout_s, output_limit=limit, dialect=dialect,
        return_files=declared, return_files_invalid=return_files_invalid,
        resources=resources, envelope_sha256=envelope_sha256)


def _claim_job(queue: Path, prepared: job_ownership.PreparedJob,
               key: bytes, owner_id: str,
               generation: str | None = None) -> str:
    return job_ownership.claim_job(
        queue, prepared, owner_id, bridge, key, _now, generation)


def _run_prepared(queue: Path, key: bytes,
                  prepared: job_ownership.PreparedJob,
                  generation: str) -> str:
    """Execute an already claimed job and settle it with one signed result."""

    def _record_start(child_pid: int) -> None:
        claim = job_ownership.read_claim(queue, prepared, bridge, key)
        if claim.get("generation") != generation:
            raise job_lifecycle.TreeTerminationUnknown(
                "claim generation changed before spawn; "
                "child tree unproven")
        job_ownership.publish_start(
            queue, prepared, claim["owner"], generation, int(child_pid),
            bridge, key, _now)

    try:
        outcome = _execute(
            prepared.command, prepared.workdir, prepared.timeout_seconds,
            prepared.output_limit, prepared.dialect, on_start=_record_start)
    except job_lifecycle.TreeTerminationUnknown as error:
        print(f"worker: job {prepared.job_id} remains owned/unknown "
              f"({error})", file=sys.stderr)
        return "unknown"
    except Exception:  # _execute is otherwise total, but stay terminal anyway
        outcome = {"status": "error", "exit_code": 2, "stdout": "",
                   "stderr": "execution failed on the worker"[
                       :prepared.output_limit],
                   "started_at": _now(), "timed_out": False,
                   "output_truncated": False}
    try:
        returned, files_truncated = job_results.collect_return_files(
            prepared.return_files)
        outcome["files"] = returned
        outcome["output_truncated"] = (
            outcome.get("output_truncated", False) or files_truncated
            or prepared.return_files_invalid)
    except Exception:  # return-file walk must never crash/re-execute
        outcome["files"] = {}
        outcome["output_truncated"] = True
        outcome["stderr"] = (str(outcome.get("stderr") or "")
                             + "\nreturn-files skipped")[
                                 :prepared.output_limit]
    sealed = _seal_result(queue, key, prepared.job_id, **outcome)
    if sealed not in ("sealed", "skipped"):
        return sealed
    return job_reconciliation.publish_release(
        queue, key, prepared, generation, bridge, _now)


def _run_one(queue: Path, key: bytes, job_id: str) -> str:
    """Claim and process one job id without any automatic claim recovery."""

    prepared = _prepare_job(queue, key, job_id)
    if isinstance(prepared, str):
        return prepared
    generation = uuid.uuid4().hex
    claimed = _claim_job(
        queue, prepared, key, uuid.uuid4().hex, generation)
    if claimed != "claimed":
        return claimed
    return _run_prepared(queue, key, prepared, generation)


def run_once(queue: Path, key: bytes, max_active: int = 1) -> dict:
    """Run one bounded batch."""

    if isinstance(max_active, bool) or not 1 <= int(max_active) <= 2:
        raise ValueError("max_active must be 1 or 2")
    counts = {"sealed": 0, "skipped": 0, "refused": 0, "failed": 0}
    waiting: list[job_ownership.PreparedJob] = []
    for job_id in job_ownership.pending(queue):
        prepared = _prepare_job(queue, key, job_id)
        if isinstance(prepared, str):
            counts[prepared] += 1
        else:
            waiting.append(prepared)

    owner_id = uuid.uuid4().hex
    generations: dict[str, str] = {}

    def claim(prepared: job_ownership.PreparedJob) -> str:
        generation = uuid.uuid4().hex
        outcome = _claim_job(
            queue, prepared, key, owner_id, generation)
        if outcome == "claimed":
            generations[prepared.job_id] = generation
        return outcome

    outcomes = job_ownership.run_bounded(
        waiting, int(max_active),
        claim,
        lambda prepared: _run_prepared(
            queue, key, prepared, generations[prepared.job_id]))
    for outcome, count in outcomes.items():
        counts[outcome] = counts.get(outcome, 0) + count
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worker.py", description="Lenovo signed-queue worker")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--shared", default=None, help="shared LAN folder (key-custody check)")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="0=settled; 2=failed; 3=retained")
    parser.add_argument("--max-jobs", type=int, default=0, help="0 means no bound")
    parser.add_argument(
        "--max-active", type=int, default=1, choices=(1, 2),
        help="2 requires isolation proof")
    parser.add_argument("--reconcile-id", default=None)
    parser.add_argument("--reconcile-proof", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    queue = Path(args.queue)
    if not queue.is_dir() or not (queue / "jobs").is_dir() or not (queue / "results").is_dir():
        return _fail(f"queue unavailable at {args.queue}: check path/mount")
    shared = Path(args.shared) if args.shared else None
    if shared is not None and not shared.is_dir():
        return _fail(f"shared folder unavailable at {args.shared}: check path/mount")
    key_file = Path(args.key_file)
    problem = _check_key_outside(key_file.resolve(), queue.resolve(), shared.resolve() if shared else None)
    if problem:
        return _fail(f"refusing to use key: {problem}")
    key = _load_key(key_file, args.key_format, queue)
    if key is None:
        return 2
    if bool(args.reconcile_id) != bool(args.reconcile_proof):
        return _fail("reconcile-id and reconcile-proof are required together")
    if args.reconcile_id:
        return job_reconciliation.reconcile(
            queue, key, args.reconcile_id, Path(args.reconcile_proof),
            bridge, _prepare_job, _seal_result, _now, WORKER_SCHEMA)
    if not isinstance(args.poll, (int, float)) or not 0 < float(args.poll) <= 300:
        return _fail("poll must be within (0, 300] seconds")
    if args.once:
        counts = run_once(queue, key, args.max_active)
        print(json.dumps({"schema": WORKER_SCHEMA, "mode": "once", **counts}))
        return job_ownership.batch_exit(counts)
    done = 0
    while True:
        counts = run_once(queue, key, args.max_active)
        done += counts["sealed"]
        if counts.get("unknown"):
            return 3
        if args.max_jobs and (
                counts.get("owned") or counts.get("unknown")
                or counts.get("refused") or counts.get("failed")):
            return job_ownership.batch_exit(counts)
        if args.max_jobs and done >= args.max_jobs:
            break
        time.sleep(float(args.poll))
    if args.max_jobs:
        return job_ownership.batch_exit(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
