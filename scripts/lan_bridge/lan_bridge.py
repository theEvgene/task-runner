"""Signed LAN command bridge (stdlib only).

Agent-operable route for carrying one command to the Lenovo worker and
fetching its MAC-bound result. Mirrors the reviewed LanTaskRunnerMVP
protocol v1 + immutable file queue semantics (source revision e86d2007):
authenticated raw-byte envelopes (HMAC-SHA256 over exact payload bytes),
32-lowercase-hex ids, immutable ``<id>.json`` publish (no clobber), kind/id
binding on open. No crypto change, no new permission: the key stays in a
file outside the queue and outside the repository and is never printed.

Queue layout (owner-provided ``--queue`` dir, never in the repo):
``<queue>/jobs/<id>.json`` (sealed ``kind=job``), ``<queue>/results/<id>.json``
(sealed ``kind=result``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import syntax as _syn
# The error taxonomy and the immutable file queue live in their own modules.
# They are imported here too: callers hold this module as the one bridge handle
# (`bridge.JobExistsError`, `bridge.read`, ...), and the queue keeps raising
# the same classes the envelope layer raises.
from bridge_errors import (
    AtomicPublishNotSupportedError, BridgeError, FetchTimeout, FileQueueError,
    InvalidJobIdError, JobExistsError, JobNotFoundError, ProtocolError,
    SizeLimitError, UnsafePathError,
)
from file_queue import (
    DEFAULT_READ_MAX, JOB_ID_RE, _validate_job_id, publish, read,
)
from job_payload import build_job_payload

MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
KEY_BYTES = 32
# Owner wait defaults (#12/#13): fetch waits up to 240 s, checking every
# 30 s. Explicit --timeout/--poll stay allowed (shorter or longer). This is
# the *result wait*; `submit --timeout` separately limits remote execution.
DEFAULT_FETCH_TIMEOUT_SECONDS = 240.0
DEFAULT_FETCH_POLL_SECONDS = 30.0


def _is_hex32(value: str) -> bool:
    return isinstance(value, str) and JOB_ID_RE.fullmatch(value) is not None


def _validate_key(key: bytes) -> None:
    if isinstance(key, bool) or not isinstance(key, (bytes, bytearray)):
        raise ProtocolError("invalid key")
    if len(key) != KEY_BYTES:
        raise ProtocolError("invalid key")


def seal(payload: dict, key: bytes) -> bytes:
    """Seal a payload dict into envelope bytes (MAC over exact payload bytes)."""
    _validate_key(key)
    if not isinstance(payload, dict):
        raise ProtocolError("invalid payload")
    kind = payload.get("kind")
    pid = payload.get("id")
    if not isinstance(kind, str) or not isinstance(pid, str):
        raise ProtocolError("invalid payload fields")
    if not _is_hex32(pid):
        raise ProtocolError("invalid payload id")
    payload_bytes = json.dumps(payload, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
    mac = hmac.new(bytes(key), payload_bytes, hashlib.sha256).hexdigest().lower()
    envelope = {"v": 1, "kind": kind, "id": pid,
                "payload_b64": base64.b64encode(payload_bytes).decode("ascii"),
                "mac": mac}
    envelope_bytes = json.dumps(envelope, ensure_ascii=False,
                                separators=(",", ":")).encode("utf-8")
    if len(envelope_bytes) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("envelope too large")
    return envelope_bytes


def open_envelope(data: bytes, key: bytes, expected_kind: str,
                  expected_id: str) -> dict:
    """Verify envelope MAC (constant-time) and kind/id binding, return payload.

    Binding before trust: the requested id must match even when the MAC is
    valid for another id, otherwise the payload belongs to another job.
    """
    _validate_key(key)
    if isinstance(data, bool) or not isinstance(data, (bytes, bytearray)):
        raise ProtocolError("invalid envelope")
    if len(data) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("envelope too large")
    try:
        env = json.loads(bytes(data).decode("utf-8"))
    except Exception as e:
        raise ProtocolError("malformed envelope") from e
    if not isinstance(env, dict):
        raise ProtocolError("malformed envelope")
    if isinstance(env.get("v"), bool) or env.get("v") != 1:
        raise ProtocolError("bad version")
    if not isinstance(env.get("kind"), str) or env.get("kind") != expected_kind:
        raise ProtocolError("bad kind")
    pid = env.get("id")
    if not isinstance(pid, str) or pid != expected_id or not _is_hex32(pid):
        raise ProtocolError("bad id")
    if not isinstance(env.get("payload_b64"), str):
        raise ProtocolError("malformed envelope")
    mac = env.get("mac")
    if not isinstance(mac, str):
        raise ProtocolError("malformed envelope")
    mac_l = mac.lower()
    if len(mac_l) != 64 or any(
            c not in "0123456789abcdef" for c in mac_l):
        raise ProtocolError("bad mac")
    try:
        payload_bytes = base64.b64decode(env["payload_b64"], validate=True)
    except Exception as e:
        raise ProtocolError("malformed base64") from e
    calc = hmac.new(bytes(key), payload_bytes, hashlib.sha256).hexdigest().lower()
    try:
        if not hmac.compare_digest(calc, mac_l):
            raise ProtocolError("bad mac")
    except Exception as e:
        raise ProtocolError("bad mac") from e
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        raise ProtocolError("malformed payload") from e
    if not isinstance(payload, dict):
        raise ProtocolError("invalid payload")
    if payload.get("kind") != expected_kind or payload.get("id") != expected_id:
        raise ProtocolError("bad payload")
    return payload


def generate_job_id() -> str:
    return os.urandom(16).hex()


def validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timeout_seconds must be int")
    if value < 1 or value > 3600:
        raise ValueError("timeout_seconds out of range")
    return value


def validate_max_output_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_output_bytes must be int")
    if value < 1 or value > 262144:
        raise ValueError("max_output_bytes out of range")
    return value


def build_result_payload(job_id: str, status: str, exit_code: int | None = None,
                         stdout: str = "", stderr: str = "",
                         started_at: str = "", finished_at: str = "",
                         timed_out: bool = False,
                         output_truncated: bool = False,
                         files: dict | None = None) -> dict:
    if files is None:
        files = {}
    return {"kind": "result", "id": job_id, "status": status,
            "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "started_at": started_at, "finished_at": finished_at,
            "timed_out": bool(timed_out),
            "output_truncated": bool(output_truncated), "files": files}


def find_repo_root(start: str | os.PathLike | None = None) -> Path | None:
    current = Path(start).resolve() if start else Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def load_key(key_path: str, key_format: str = "raw",
             queue_dir: str | os.PathLike | None = None) -> bytes:
    """Load the 32-byte key; refuse keys inside the queue or the repository."""
    if key_format not in ("raw", "base64"):
        raise BridgeError("key_format must be raw or base64")
    path = Path(key_path).resolve()
    if queue_dir is not None and _is_inside(path, Path(queue_dir)):
        raise BridgeError("refusing to use key inside queue or repository")
    repo_root = find_repo_root()
    if repo_root is not None and _is_inside(path, repo_root):
        raise BridgeError("refusing to use key inside queue or repository")
    try:
        if key_format == "raw":
            raw = path.read_bytes()
        else:
            raw = base64.b64decode(path.read_text(
                encoding="ascii").strip(), validate=True)
    except BridgeError:
        raise
    except Exception as e:
        raise BridgeError("cannot read key") from e
    if len(raw) != KEY_BYTES:
        raise BridgeError("invalid key")
    return bytes(raw)


def submit_job(queue_dir: str, key: bytes, command: str,
               working_directory: str, timeout_seconds: int = 300,
               max_output_bytes: int = 65536,
               return_files: list | None = None, dialect=None,
               receipt_script_path: str | None = None,
               resource_claims: list | None = None,
               expected_working_directory: str | None = None,
               job_id: str | None = None) -> str:
    """Seal the command payload and publish it immutably; return the job id.

    ``resource_claims`` publishes the signed reservation together with the
    job under the queue-local referee; conflicts refuse before publication.
    ``expected_working_directory`` is the producer's declared target: the
    worker refuses a job whose signed expectation differs from its working
    directory before claiming or spawning anything. Without it the target
    stays unverified rather than "assumed equal".

    ``job_id`` names the job before its response exists, so a caller that
    must recover an exact id after a lost response can supply its own
    deterministic one instead of hoping to be told. Publication stays
    immutable and no-clobber: an existing id is refused, and with
    ``resource_claims`` an identical job and reservation resolve as
    ``owned`` instead of publishing a second time.
    """

    _syn.enforce(command, dialect, BridgeError)
    if job_id is None:
        job_id = generate_job_id()
    elif not _is_hex32(job_id):
        raise ValueError("job_id must be 32 lowercase hex")
    payload = build_job_payload(job_id=job_id, command=command,
                                working_directory=working_directory,
                                timeout_seconds=timeout_seconds,
                                max_output_bytes=max_output_bytes,
                                return_files=return_files, dialect=dialect,
                                receipt_script_path=receipt_script_path,
                                expected_working_directory=expected_working_directory)
    if resource_claims is None:
        publish(str(Path(queue_dir) / "jobs"), job_id, seal(payload, key))
        return job_id
    from managed_write import publish_reserved
    publish_reserved(Path(queue_dir), key, sys.modules[__name__], payload,
                     resource_claims)
    return job_id


def _validate_wait(timeout_seconds: float, poll_seconds: float) -> None:
    """Fail fast on unusable waits."""
    for name, value in (("timeout", timeout_seconds),
                        ("poll", poll_seconds)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BridgeError(f"{name} must be a number of seconds")
        if not (value > 0) or value != value or value in (float("inf"),
                                                          float("-inf")):
            raise BridgeError(f"{name} must be positive and finite")


def _require_queue(queue_dir: str) -> str:
    q = Path(queue_dir)
    if not q.is_dir():
        raise BridgeError("queue unavailable: check path/mount")
    r = q / "results"
    if r.exists() and not r.is_dir():
        raise BridgeError("queue results path unavailable: check path/mount")
    return str(r.resolve())


def fetch_result(queue_dir: str, key: bytes, job_id: str,
                 timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
                 poll_seconds: float = DEFAULT_FETCH_POLL_SECONDS) -> dict:
    """Wait for the sealed result; verify MAC + id binding.

    Never publishes (same-id re-read); expiry is "unknown", never "running".
    """
    _validate_job_id(job_id)
    _validate_wait(timeout_seconds, poll_seconds)
    _validate_key(key)
    deadline = time.monotonic() + float(timeout_seconds)
    results_dir = _require_queue(queue_dir)
    polls = 0
    while True:
        try:
            raw = read(results_dir, job_id)
        except JobNotFoundError:
            raw = None
        polls += 1
        if raw is not None:
            # Present but unverifiable: explicit refusal, not a timeout.
            return open_envelope(raw, key, "result", job_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchTimeout(
                f"timeout waiting for {job_id} "
                f"({polls} polls): outcome unknown, not running; "
                f"re-read the SAME id, do not resubmit")
        time.sleep(min(float(poll_seconds), remaining))


def _read_command_file(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except Exception as e:
        raise BridgeError(f"error reading command file: {e}") from e
    if not text.strip():
        raise BridgeError("empty command")
    return text


def cmd_submit(args: argparse.Namespace) -> int:
    try:
        key = load_key(args.key_file, args.key_format, args.queue)
        command = _read_command_file(args.command_file)
        return_files = list(args.return_file or [])
        job_id = submit_job(args.queue, key, command, args.cwd,
                            int(args.timeout), int(args.max_output),
                            return_files, args.dialect,
                            args.receipt_script_path,
                            resource_claims=args.claim,
                            expected_working_directory=args.expected_cwd)
    except (BridgeError, ValueError, TypeError,
            FileQueueError, ProtocolError) as e:
        print(f"submit refused: {e}", file=sys.stderr)
        return 2
    print(f"JOBID={job_id}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    # Errors echo only the job id and wait parameters, never key material.
    started = time.monotonic()
    try:
        key = load_key(args.key_file, args.key_format, args.queue)
        payload = fetch_result(args.queue, key, args.id,
                               float(args.timeout), float(args.poll))
    except FetchTimeout as e:
        print(f"TIMEOUT waiting for {args.id}: {e}", file=sys.stderr)
        return 3
    except (BridgeError, ValueError, FileQueueError, ProtocolError) as e:
        print(f"fetch refused: {e}", file=sys.stderr)
        return 2
    elapsed = time.monotonic() - started
    _report_confirmed_state(payload)
    print(f"elapsed={elapsed:.1f}s timeout={args.timeout} poll={args.poll} "
          f"outcome=verified id={args.id}", file=sys.stderr)
    text = json.dumps(payload, ensure_ascii=False)
    if args.receipt_out:
        out = Path(args.receipt_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _report_confirmed_state(payload: dict) -> None:
    """Name the verified state; there is no "running" outcome."""
    status = payload.get("status")
    exit_code = payload.get("exit_code")
    timed_out = bool(payload.get("timed_out"))
    truncated = bool(payload.get("output_truncated"))
    if timed_out:
        print("worker state: TIMED OUT on the worker "
              "(terminal, not success)", file=sys.stderr)
    elif truncated:
        print("worker state: OUTPUT TRUNCATED "
              "(terminal, output incomplete)", file=sys.stderr)
    elif status not in ("completed", "ok"):
        print(f"worker state: UNCONFIRMED status={status!r} "
              f"(envelope verified, meaning unknown)", file=sys.stderr)
    elif isinstance(exit_code, bool) or exit_code != 0:
        print(f"worker state: FAILED exit={exit_code!r} "
              f"(terminal, not success)", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lan_bridge.py",
                                     description="Signed LAN command bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="seal a command and publish it")
    p_submit.add_argument("--queue", required=True)
    p_submit.add_argument("--key-file", required=True)
    p_submit.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    p_submit.add_argument("--command-file", required=True)
    p_submit.add_argument("--cwd", required=True)
    p_submit.add_argument("--timeout", type=int, default=300)
    p_submit.add_argument("--max-output", type=int, default=65536)
    p_submit.add_argument("--return-file", action="append", default=None)
    p_submit.add_argument(
        "--receipt-script-path", default=None,
        help="absolute intended receipt-producing script path, sealed into the job",
    )
    p_submit.add_argument(
        "--expected-cwd", default=None,
        help="absolute expected working directory on the worker, sealed into "
             "the job; the worker refuses a job whose --cwd differs before "
             "spawning, and an omitted value leaves the target unverified",
    )
    p_submit.add_argument("--claim", action="append",
                          help="managed-write footprint (repeatable)")
    _syn.add_cli(p_submit)
    p_submit.set_defaults(func=cmd_submit)

    p_fetch = sub.add_parser("fetch", help="wait for and verify the result")
    p_fetch.add_argument("--queue", required=True)
    p_fetch.add_argument("--key-file", required=True)
    p_fetch.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    p_fetch.add_argument("--id", required=True)
    p_fetch.add_argument("--timeout", type=float,
                         default=DEFAULT_FETCH_TIMEOUT_SECONDS)
    p_fetch.add_argument("--poll", type=float,
                         default=DEFAULT_FETCH_POLL_SECONDS)
    p_fetch.add_argument("--receipt-out", default=None)
    p_fetch.set_defaults(func=cmd_fetch)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BridgeError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
