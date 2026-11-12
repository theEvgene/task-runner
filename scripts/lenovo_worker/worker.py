"""Lenovo queue worker for the signed LAN bridge (stdlib only).

Delivery: this file IS the worker. It travels with the task-runner checkout
at the pinned revision the operator copies to Lenovo; nothing is downloaded
or assumed present at a remembered path. The launcher starts the sibling
copy by default (``--worker-dir`` override names another checkout explicitly).

Loop: every poll, seal results only for jobs that have no result yet.
Existing results are never read-modify-written and never replaced
(``publish`` refuses duplicates: a second worker that wins the race loses
here, which is the safe direction). A job whose envelope cannot be verified
is never executed; a refusal result is sealed under the same id so the
observer gets a terminal, verified failure instead of an endless unknown.
The queue is never created here: a missing queue/jobs or queue/results dir
is exit 2 (check path/mount), never an empty new queue.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORKER_SCHEMA = "lenovo-worker/1"
ID_SUFFIX = ".json"


def _load_bridge() -> object:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "lan_bridge" / "lan_bridge.py",
        Path(os.environ.get("DELL_LAN_BRIDGE_DIR", "") or ".") / "lan_bridge.py",
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("lenovo_worker_bridge", str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules["lenovo_worker_bridge"] = module
            spec.loader.exec_module(module)
            return module
    raise SystemExit("refused: bridge client not found (scripts/lan_bridge/lan_bridge.py)")


bridge = _load_bridge()


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


def _seal_result(queue: Path, key: bytes, job_id: str, **fields) -> bool:
    payload = {
        "kind": "result", "id": job_id,
        "status": fields.get("status", "error"),
        "exit_code": fields.get("exit_code", 2),
        "stdout": fields.get("stdout", "") or "",
        "stderr": fields.get("stderr", "") or "",
        "started_at": fields.get("started_at", "") or "",
        "finished_at": _now(),
        "timed_out": bool(fields.get("timed_out", False)),
        "output_truncated": bool(fields.get("output_truncated", False)),
        "files": fields.get("files", {}) or {},
    }
    try:
        bridge.publish(str(queue / "results"), job_id, bridge.seal(payload, key))
        return True
    except bridge.JobExistsError:
        return False  # another worker sealed it first: safe direction
    except Exception as error:
        print(f"worker: cannot seal result for {job_id} ({error})", file=sys.stderr)
        return False


def _execute(command: str, workdir: str, timeout_s: int, limit: int) -> dict:
    started = _now()
    try:
        proc = subprocess.run(
            command, shell=True, cwd=workdir, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or "") if isinstance(error.stdout, str) else ""
        err = (error.stderr or "") if isinstance(error.stderr, str) else ""
        return {"status": "error", "exit_code": 124,
                "stdout": out[:limit], "stderr": (f"timed out on the worker after {timeout_s}s\n" + err)[:limit],
                "started_at": started, "timed_out": True, "output_truncated": len(out) > limit}
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    return {"status": "ok" if proc.returncode == 0 else "error",
            "exit_code": proc.returncode,
            "stdout": stdout[:limit], "stderr": stderr[:limit],
            "started_at": started, "timed_out": False,
            "output_truncated": len(stdout) > limit or len(stderr) > limit}


def _collect_return_files(declared: list, limit_files: int = 8) -> dict:
    files: dict[str, str] = {}
    for raw in (declared or [])[:limit_files]:
        try:
            path = Path(str(raw))
        except (ValueError, TypeError):
            continue
        try:
            if path.is_file():
                files[str(path)] = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
    return files


def _run_one(queue: Path, key: bytes, job_id: str) -> str:
    """Process one job id. Returns: sealed | skipped | refused."""

    try:
        raw = bridge.read(str(queue / "jobs"), job_id)
    except Exception as error:
        print(f"worker: cannot read job {job_id} ({error})", file=sys.stderr)
        return "refused"
    try:
        job = bridge.open_envelope(raw, key, "job", job_id)
    except Exception as error:
        _seal_result(queue, key, job_id, status="error", exit_code=2,
                     stderr=f"job-unverifiable: {error}")
        return "sealed"
    command = job.get("command")
    workdir = job.get("working_directory")
    if not isinstance(command, str) or not command.strip() or not isinstance(workdir, str) or not workdir:
        _seal_result(queue, key, job_id, status="error", exit_code=2,
                     stderr="job-unverifiable: empty command or missing working_directory")
        return "sealed"
    try:
        timeout_s = int(job.get("timeout_seconds", 300))
        limit = int(job.get("max_output_bytes", 65536))
    except (TypeError, ValueError):
        _seal_result(queue, key, job_id, status="error", exit_code=2,
                     stderr="job-unverifiable: bad timeout/max_output")
        return "sealed"
    if not (1 <= timeout_s <= 3600 and 1 <= limit <= 262144):
        _seal_result(queue, key, job_id, status="error", exit_code=2,
                     stderr="job-unverifiable: timeout/max_output out of range")
        return "sealed"
    outcome = _execute(command, workdir, timeout_s, limit)
    outcome["files"] = _collect_return_files(job.get("return_files"))
    _seal_result(queue, key, job_id, **outcome)
    return "sealed"


def _pending(queue: Path, key: bytes) -> list[str]:
    jobs_dir, results_dir = queue / "jobs", queue / "results"
    try:
        names = sorted(p.name for p in jobs_dir.iterdir() if p.is_file() and p.name.endswith(ID_SUFFIX))
    except OSError as error:
        print(f"worker: cannot list jobs ({error})", file=sys.stderr)
        return []
    pending: list[str] = []
    for name in names:
        job_id = name[: -len(ID_SUFFIX)]
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            continue
        if (results_dir / name).exists():
            continue  # sealed already: never touch foreign results
        pending.append(job_id)
    return pending


def run_once(queue: Path, key: bytes) -> dict:
    counts = {"sealed": 0, "skipped": 0, "refused": 0}
    for job_id in _pending(queue, key):
        counts[_run_one(queue, key, job_id)] += 1
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worker.py", description="Lenovo signed-queue worker")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--shared", default=None, help="shared LAN folder (key-custody check)")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="single scan, then exit 0")
    parser.add_argument("--max-jobs", type=int, default=0, help="0 means no bound")
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
    if not isinstance(args.poll, (int, float)) or not 0 < float(args.poll) <= 300:
        return _fail("poll must be within (0, 300] seconds")
    if args.once:
        counts = run_once(queue, key)
        print(json.dumps({"schema": WORKER_SCHEMA, "mode": "once", **counts}))
        return 0
    done = 0
    while True:
        counts = run_once(queue, key)
        done += counts["sealed"]
        if args.max_jobs and done >= args.max_jobs:
            break
        time.sleep(float(args.poll))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
