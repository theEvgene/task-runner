"""Lenovo signed-queue worker (stdlib only).

Verified jobs execute at most once and results publish immutably. Refusals,
spawn failures, timeouts, and oversized output become bounded terminal
results. Missing queue infrastructure and unsealed results fail closed.
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
# Return-file bounds: the job contract allows any declared path (same as the
# signed bridge's --return-file), but each file and their total are capped so
# the sealed envelope stays within protocol limits. Truncation is reported in
# output_truncated; sealing failures are reported, never claimed as sealed.
RETURN_FILES_MAX_COUNT = 8
RETURN_FILE_MAX_BYTES = 262144
RETURN_FILES_MAX_TOTAL_BYTES = 1048576


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


# Envelope budget: the signed bridge refuses envelopes over 2 MiB. The
# sealed payload is base64-expanded (~4/3) inside the envelope, so the payload
# JSON must stay well under ~1.5 MiB. Fit outputs + encoded files to this
# safe payload ceiling before sealing; on a size refusal seal a minimal
# fallback so the job ends terminal instead of re-executing every poll.
PAYLOAD_SAFE_MAX_BYTES = 1400000
MIN_FALLBACK_STDERR = "result too large; outputs dropped to fit the signed envelope"


def _payload_size(payload: dict) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return PAYLOAD_SAFE_MAX_BYTES + 1


def _fit_payload(payload: dict) -> dict:
    """Trim payload in place so its JSON stays under the safe ceiling."""
    if _payload_size(payload) <= PAYLOAD_SAFE_MAX_BYTES:
        return payload
    if payload.get("files"):
        payload["files"] = {}
        payload["output_truncated"] = True
    if _payload_size(payload) <= PAYLOAD_SAFE_MAX_BYTES:
        return payload
    for budget in (65536, 16384, 4096, 0):
        if budget == 0:
            payload["stdout"] = ""
            payload["stderr"] = MIN_FALLBACK_STDERR[:4096]
            payload["output_truncated"] = True
            break
        payload["stdout"] = str(payload.get("stdout") or "")[:budget]
        payload["stderr"] = str(payload.get("stderr") or "")[:budget]
        payload["output_truncated"] = True
        if _payload_size(payload) <= PAYLOAD_SAFE_MAX_BYTES:
            break
    return payload


def _seal_result(queue: Path, key: bytes, job_id: str, **fields) -> str:
    """Seal a result. Returns "sealed", "skipped", or "failed"."""

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
    _fit_payload(payload)
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
                "stderr": MIN_FALLBACK_STDERR,
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
             dialect: str | None = None) -> dict:
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
        proc = subprocess.run(
            effective, shell=True, cwd=workdir, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or "") if isinstance(error.stdout, str) else ""
        err = (error.stderr or "") if isinstance(error.stderr, str) else ""
        return {"status": "error", "exit_code": 124,
                "stdout": out[:limit], "stderr": (f"timed out on the worker after {timeout_s}s\n" + err)[:limit],
                "started_at": started, "timed_out": True, "output_truncated": len(out) > limit}
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
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    return {"status": "ok" if proc.returncode == 0 else "error",
            "exit_code": proc.returncode,
            "stdout": stdout[:limit], "stderr": stderr[:limit],
            "started_at": started, "timed_out": False,
            "output_truncated": len(stdout) > limit or len(stderr) > limit}


def _collect_return_files(declared: list, limit_files: int = RETURN_FILES_MAX_COUNT,
                          limit_each: int = RETURN_FILE_MAX_BYTES,
                          limit_total: int = RETURN_FILES_MAX_TOTAL_BYTES,
                          ) -> tuple[dict, bool]:
    """Collect declared return files with byte bounds. Returns (files, truncated).

    Any declared path is honored (the bridge contract puts no path
    restriction on --return-file); only size is bounded.
    """

    files: dict[str, str] = {}
    total = 0
    truncated = False
    for raw in (declared or [])[:limit_files]:
        try:
            path = Path(str(raw))
        except (ValueError, TypeError):
            continue
        try:
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                data = handle.read(limit_each + 1)
        except OSError:
            continue
        if len(data) > limit_each:
            data = data[:limit_each]
            truncated = True
        if total + len(data) > limit_total:
            truncated = True
            break
        total += len(data)
        files[str(path)] = base64.b64encode(data).decode("ascii")
    if isinstance(declared, list) and len(declared) > limit_files:
        truncated = True
    return files, truncated


def _run_one(queue: Path, key: bytes, job_id: str) -> str:
    """Process one job id. Returns: sealed | skipped | refused | failed."""

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
    try:
        outcome = _execute(command, workdir, timeout_s, limit, dialect)
    except Exception:  # _execute is total, but stay terminal anyway
        outcome = {"status": "error", "exit_code": 2, "stdout": "",
                   "stderr": "execution failed on the worker"[:limit],
                   "started_at": _now(), "timed_out": False,
                   "output_truncated": False}
    try:
        declared = job.get("return_files")
        if not isinstance(declared, list):
            declared = []
            files_truncated = bool(job.get("return_files"))
        else:
            returned, files_truncated = _collect_return_files(declared)
            outcome["files"] = returned
            outcome["output_truncated"] = outcome["output_truncated"] or files_truncated
            return _seal_result(queue, key, job_id, **outcome)
        returned, _ = _collect_return_files(declared)
        outcome["files"] = returned
        outcome["output_truncated"] = outcome.get("output_truncated", False) or files_truncated
    except Exception:  # return-file walk must never crash/re-execute
        outcome["files"] = {}
        outcome["output_truncated"] = True
        outcome["stderr"] = (str(outcome.get("stderr") or "")
                             + "\nreturn-files skipped")[:limit]
    return _seal_result(queue, key, job_id, **outcome)


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
    counts = {"sealed": 0, "skipped": 0, "refused": 0, "failed": 0}
    for job_id in _pending(queue, key):
        counts[_run_one(queue, key, job_id)] += 1
    return counts


def _batch_exit(counts: dict) -> int:
    """A finished batch fails when any job was refused or never sealed."""

    return 2 if (counts.get("refused") or counts.get("failed")) else 0


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
        return _batch_exit(counts)
    done = 0
    while True:
        counts = run_once(queue, key)
        done += counts["sealed"]
        if args.max_jobs and done >= args.max_jobs:
            break
        time.sleep(float(args.poll))
    if args.max_jobs:
        return _batch_exit(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
