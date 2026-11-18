#!/usr/bin/env python3
"""Submit one Dell managed write with its authoritative resource reservation.

This is the Dell producer entry for the managed-write routes (``transfer``,
``publish``, ``publish-existing``): it hands the sealed command to
``lan_bridge.submit_job(..., resource_claims=...)`` so the signed job and its
reservation publish together under the queue-local referee (#89, #90, #91).
Nothing here re-implements the referee or adds a second queue.

Every decision is derived from the digest-bound request file, never from free
command text and never from the operator's directory. Refused before any queue
write: a request digest, schema, worktree/checkout path, or revision the file
cannot prove; a ``--cwd`` that is not that checkout (the digest-bound request
is the only provable target); and a command that is not exactly one invocation
of the one driver owning this schema, with ``--request`` and
``--request-sha256`` for this exact request and no other statement or
argument. A substring marker is never enough (see ``verify_first_job``).

After publication the job's reservation is read back against the exact job
bytes; that receipt is the proof. The job id is printed the moment publication
returns, so a proof that cannot be read is an ``unknown`` outcome with a
machine-readable id and its own receipt: the slot stays held and the same id is
re-read, never resubmitted. A failed publish removes only this attempt's
provisional reservation (the referee does it).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, PUBLISH_REQUEST_SCHEMA, Stop, default_bridge_dir, load_bridge,
    read_key, sha256_bytes, write_json_atomic,
)
from dell_standalone_common import (
    PUBLISH_EXISTING_REQUEST_SCHEMA, TRANSFER_REQUEST_SCHEMA,
)

RECEIPT_SCHEMA = "dell-lan-managed-submit/1"
#: One schema, one driver that owns its footprint.
DRIVERS = {
    PUBLISH_REQUEST_SCHEMA: "lenovo_publish.py",
    TRANSFER_REQUEST_SCHEMA: "lenovo_transfer.py",
    PUBLISH_EXISTING_REQUEST_SCHEMA: "lenovo_publish_existing.py",
}
MANAGED_SCHEMAS = frozenset(DRIVERS)
PYTHON_NAMES = frozenset({"python", "python.exe", "python3", "python3.exe"})
LAUNCHER_NAMES = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
LAUNCHER_FLAGS = frozenset({"-noprofile", "-noninteractive", "-nologo"})
#: No generated argument carries these: such a token is a second statement.
SHELL_META = ("%", "!", "^", "&", "|", "<", ">", "(", ")", ";", '"', "\n",
              "\r", "\x00")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_COMMAND_BYTES = 1024 * 1024


class Unproven(Exception):
    """The job is published but its reservation proof cannot be read (#91).

    The id and receipt stay machine-readable; no resubmit is ever implied.
    """

    def __init__(self, job_id: str, receipt: dict, reason: str = "",
                 detail: str = "", code: int = 3) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.job_id = job_id
        self.receipt = receipt
        self.reason = reason
        self.detail = detail
        self.code = code or 3


def _reservations():
    """Load the queue-side referee next to the worker package, or fail closed."""

    directory = Path(__file__).resolve().parent.parent / "lenovo_worker"
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    try:
        import job_lifecycle
        import job_reservations
    except Exception as error:
        raise Stop("driver-stale", detail=(
            f"reservation module unavailable: {error}"[:DETAIL_MAX])) from error
    return job_lifecycle, job_reservations


def load_request(path: Path, expected_sha256: str) -> tuple[dict, str]:
    """Read the request only when its bytes match the declared digest."""

    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise Stop("request-unreadable", detail=str(error)[:DETAIL_MAX]) from error
    digest = sha256_bytes(raw)
    if digest != (expected_sha256 or "").strip().lower():
        raise Stop("request-mismatch", detail=(
            f"request sha256 {digest} does not match the declared digest"))
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Stop("request-unreadable", detail=str(error)[:DETAIL_MAX]) from error
    if not isinstance(request, dict):
        raise Stop("request-unreadable", detail="request file is not a JSON object")
    if request.get("schema") not in MANAGED_SCHEMAS:
        raise Stop("request-not-managed", detail=(
            f"schema {request.get('schema')!r} is not a managed write"))
    return request, digest


def revisions(request: dict) -> dict:
    """The revision identity this request pins, or a refusal."""

    pinned = {}
    for name in ("expectedBaseSha", "expectedCommit", "expectedPreviousCommit"):
        value = request.get(name)
        if value is None:
            pinned[name] = None
            continue
        if not isinstance(value, str) or SHA_RE.fullmatch(value.lower()) is None:
            raise Stop("revision-unprovable", detail=(
                f"{name} is not a 40-hex commit"))
        pinned[name] = value.lower()
    return pinned


def _same_path(left: object, right: object) -> bool:
    """Compare two Lenovo paths by spelling; links are resolved on Lenovo only."""

    try:
        first, second = Path(str(left)), Path(str(right))
        if not first.is_absolute() or not second.is_absolute():
            return False
        return (os.path.normcase(os.path.normpath(str(first)))
                == os.path.normcase(os.path.normpath(str(second))))
    except (OSError, ValueError, RuntimeError):
        return False


def _absolute(value: object, label: str) -> Path:
    """One proven absolute Lenovo path from the digest-bound request."""

    if (not isinstance(value, str) or not value.strip()
            or not Path(value).is_absolute()):
        raise Stop("claim-unprovable", detail=(
            f"{label} must be the absolute Lenovo path"))
    return Path(value)


def claims_for(request: dict) -> list[str]:
    """The real write footprint: the task worktree plus the ref-store checkout."""

    worktree = _absolute(request.get("lenovoWorktree"), "lenovoWorktree")
    checkout = _absolute(request.get("lenovoCheckout"), "lenovoCheckout")
    if _same_path(worktree, checkout):
        raise Stop("claim-unprovable", detail=(
            "lenovoWorktree and lenovoCheckout name the same directory"))
    return [f"file:{worktree}", f"file:{checkout}"]


def working_directory(request: dict, declared: object) -> str:
    """The job's real target: the request's checkout, never the operator's cwd."""

    checkout = str(Path(str(request["lenovoCheckout"])))
    if declared is None:
        return checkout
    if not _same_path(declared, checkout):
        raise Stop("cwd-unbound", code=2, detail=(
            "--cwd must be the request's lenovoCheckout: the digest-bound "
            "request is the only provable target"))
    return checkout


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError as error:
        raise Stop("command-unbound", detail=str(error)[:DETAIL_MAX]) from error


def _refuse(detail: str) -> Stop:
    return Stop("command-unbound", detail=detail[:DETAIL_MAX])


def _extra_args(tail: list[str]) -> None:
    """Only the documented flow arguments may follow the bound request pair."""

    if tail[:1] == ["--flow-dir"]:
        if len(tail) < 2 or not Path(tail[1]).is_absolute():
            raise _refuse("--flow-dir needs one absolute path")
        tail = tail[2:]
    if tail and tail != ["--reconcile"]:
        raise _refuse("unexpected extra arguments after the bound request pair")


def _driver_argv(command: str, dialect: str) -> list[str]:
    """The single driver invocation the sealed command runs, or a refusal.

    Mirrors ``verify_first_job``: parse, never match a substring. One absolute
    python interpreter, one absolute driver, and the exact ``--request`` /
    ``--request-sha256`` pair. The PowerShell dialect must carry the
    transport's explicit ``-Command`` launcher whose script is one ``&`` call
    of that argv; any second statement, operator, or trailing argument refuses
    before publication.
    """

    argv = _split(command)
    if dialect == "powershell":
        name = (argv[0].replace("/", "\\").rsplit("\\", 1)[-1].lower()
                if argv else "")
        if name not in LAUNCHER_NAMES:
            raise _refuse("--dialect powershell needs the explicit "
                          "powershell -Command launcher")
        if len(argv) < 3 or argv[-2].lower() != "-command":
            raise _refuse("the launcher must end with one -Command script")
        if not {token.lower() for token in argv[1:-2]} <= LAUNCHER_FLAGS:
            raise _refuse("unsupported powershell launcher flag")
        script = _split(argv[-1])
        if not script or script[0] != "&":
            raise _refuse("the -Command script must be one '&' call")
        argv = script[1:]
    elif dialect != "cmd":
        raise _refuse("explicit dialect required: cmd|powershell")
    for token in argv:
        if not token or any(char in token for char in SHELL_META):
            raise _refuse("the command carries a shell operator or an empty "
                          "argument")
    if len(argv) < 6:
        raise _refuse("required: <python> <driver> --request <path> "
                      "--request-sha256 <digest>")
    python, driver, flag_request, request_path, flag_digest, digest = argv[:6]
    if (Path(python).name.lower() not in PYTHON_NAMES
            or not Path(python).is_absolute()):
        raise _refuse("the command must start with one absolute python "
                      "interpreter")
    if not Path(driver).is_absolute():
        raise _refuse("the driver path must be absolute")
    if flag_request != "--request" or flag_digest != "--request-sha256":
        raise _refuse("the command must pass --request and --request-sha256")
    if DIGEST_RE.fullmatch(digest.lower()) is None:
        raise _refuse("the command must name a 64-hex request sha256")
    _extra_args(argv[6:])
    return [python, driver, request_path, digest]


def bind_command(command: str, request_path: Path, request_digest: str,
                 schema: str, dialect: str) -> None:
    """The sealed command must be exactly this request's one driver invocation."""

    if not isinstance(command, str) or not command.strip():
        raise Stop("command-unreadable", detail="command file is empty")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise Stop("command-unreadable", detail="command file is too large")
    _, driver, named_request, digest = _driver_argv(command, dialect)
    expected = DRIVERS.get(schema)
    if expected is None or Path(driver).name.lower() != expected:
        raise Stop("command-unbound", detail=(
            f"the command must run {expected} for this request schema"))
    if not _same_path(named_request, request_path):
        raise Stop("command-unbound", detail=(
            "the command must name this exact request file"))
    if digest.lower() != request_digest:
        raise Stop("command-unbound", detail=(
            "the command must name this request's sha256"))


def _read_command(path: Path) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise Stop("command-unreadable", detail=str(error)[:DETAIL_MAX]) from error
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Stop("command-unreadable", detail=str(error)[:DETAIL_MAX]) from error


def _proof(bridge, queue: Path, key: bytes, job_id: str, claims: list[str],
           cwd: str, command: str, request_path: Path, digest: str,
           request: dict, pinned: dict) -> dict:
    """Read the published job and its reservation; the receipt is the proof."""

    job_lifecycle, job_reservations = _reservations()
    try:
        raw_job = bridge.read(str(Path(queue) / "jobs"), job_id)
        job_digest = sha256_bytes(raw_job)
        record = job_reservations.read(
            queue, key, bridge, job_id, job_digest)
        payload = bridge.open_envelope(raw_job, key, "job", job_id)
        expected = list(job_reservations.normalize_resources(
            claims, job_lifecycle.resource_identity(cwd)))
    except Exception as error:
        raise Stop("reservation-unreadable", verdict="unknown", code=3,
                   detail=(f"job {job_id} is published but its reservation "
                           f"proof cannot be read; keep the slot and do not "
                           f"resubmit: {error}"[:DETAIL_MAX])) from error
    if record is None:
        raise Stop("reservation-missing", verdict="unknown", code=3, detail=(
            f"job {job_id} is published without a reservation; keep the slot "
            "and do not resubmit"))
    if (record["resources"] != expected
            or record["job_envelope_sha256"] != job_digest):
        raise Stop("reservation-mismatch", verdict="unknown", code=3, detail=(
            f"job {job_id} reservation does not match this submit"))
    if (payload.get("command") != command
            or payload.get("working_directory") != cwd
            or payload.get("expected_working_directory") != cwd):
        raise Stop("reservation-mismatch", verdict="unknown", code=3, detail=(
            f"job {job_id} payload differs from this submit"))
    return {
        "schema": RECEIPT_SCHEMA,
        "outcome": "reserved",
        "jobId": job_id,
        "requestSchema": request["schema"],
        "requestPath": str(request_path),
        "requestSha256": digest,
        "branch": request.get("branch"),
        "revisions": pinned,
        "claims": list(claims),
        "resources": record["resources"],
        "reservation": {
            "generation": record["generation"],
            "resourceDigest": record["resource_digest"],
            "jobEnvelopeSha256": record["job_envelope_sha256"],
            "owner": record["owner"],
            "reservedAt": record["reserved_at"],
        },
        "workingDirectory": cwd,
        "worktree": request.get("lenovoWorktree"),
        "checkout": request.get("lenovoCheckout"),
    }


def unknown_receipt(request: dict, request_path: Path, digest: str,
                    claims: list[str], cwd: str, job_id: str, reason: str,
                    detail: str) -> dict:
    """The receipt for a published job whose proof is unknown: no resubmit."""

    return {
        "schema": RECEIPT_SCHEMA,
        "outcome": "unknown",
        "jobId": job_id,
        "requestSchema": request.get("schema"),
        "requestPath": str(request_path),
        "requestSha256": digest,
        "branch": request.get("branch"),
        "claims": list(claims),
        "reservation": None,
        "workingDirectory": cwd,
        "worktree": request.get("lenovoWorktree"),
        "checkout": request.get("lenovoCheckout"),
        "reason": reason,
        "detail": detail[:DETAIL_MAX],
        "resubmit": "forbidden",
        "nextAction": "re-read the SAME job id; the footprint stays held",
    }


def submit_managed(bridge, queue: Path, key: bytes, *, request_path: Path,
                   request_sha256: str, command: str, cwd: str | None,
                   dialect: str, receipt_script_path: str,
                   timeout_seconds: int = 900,
                   max_output_bytes: int = 65536,
                   published=None) -> dict:
    """Reserve the proven footprint and publish the signed job atomically."""

    request, digest = load_request(Path(request_path), request_sha256)
    pinned = revisions(request)
    claims = claims_for(request)
    target = working_directory(request, cwd)
    bind_command(command, Path(request_path), digest, str(request["schema"]),
                 dialect)
    try:
        job_id = bridge.submit_job(
            str(queue), key, command, target, timeout_seconds, max_output_bytes,
            None, dialect, receipt_script_path,
            resource_claims=claims, expected_working_directory=target)
    except bridge.BridgeError as error:
        # The referee refused before publishing: no job, no reservation, and
        # nothing to roll back. The caller may fix the footprint and resubmit.
        raise Stop("reservation-refused", code=2,
                   detail=str(error)[:DETAIL_MAX]) from error
    except (ValueError, TypeError, OSError) as error:
        raise Stop("submit-refused", code=2,
                   detail=str(error)[:DETAIL_MAX]) from error
    # Published. Announce the id at once and turn every later failure into one
    # unknown outcome that keeps the slot and never invites a resubmit.
    if published is not None:
        published(job_id)
    try:
        return _proof(bridge, Path(queue), key, job_id, claims, target, command,
                      Path(request_path), digest, request, pinned)
    except Stop as stop:
        raise Unproven(
            job_id, unknown_receipt(request, Path(request_path), digest, claims,
                                    target, job_id, stop.reason, stop.detail),
            stop.reason, stop.detail, stop.code) from stop
    except Exception as error:  # never lose the id on an unexpected failure
        detail = str(error)[:DETAIL_MAX]
        raise Unproven(
            job_id, unknown_receipt(request, Path(request_path), digest, claims,
                                    target, job_id, "reservation-unreadable",
                                    detail),
            "reservation-unreadable", detail, 3) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dell_managed_submit.py", description=__doc__)
    parser.add_argument("--queue", required=True, help="signed queue directory")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--request", required=True, help="request file on the LAN share")
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--command-file", required=True)
    parser.add_argument("--cwd", required=True,
                        help="the request's absolute Lenovo checkout")
    parser.add_argument("--dialect", required=True, choices=("cmd", "powershell"))
    parser.add_argument("--receipt-script-path", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-output", type=int, default=65536)
    parser.add_argument("--shared", required=True,
                        help="owner-provided LAN share (key custody proof)")
    parser.add_argument("--repo", action="append", required=True,
                        help="Dell checkout root (key custody proof; repeatable)")
    parser.add_argument("--bridge-dir", default=None,
                        help="lan_bridge scripts dir (default: the configured one)")
    parser.add_argument("--receipt", required=True,
                        help="where to write the dell-lan-managed-submit/1 receipt")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    def announce(job_id: str) -> None:
        # The handle exists before any proof is read.
        print(f"JOBID={job_id}", flush=True)

    try:
        bridge = load_bridge(
            Path(args.bridge_dir) if args.bridge_dir else default_bridge_dir())
        queue = Path(args.queue)
        key = read_key(bridge, queue, Path(args.key_file), args.key_format,
                       repos=[Path(item) for item in args.repo],
                       shared=Path(args.shared))
        receipt = submit_managed(
            bridge, queue, key,
            request_path=Path(args.request),
            request_sha256=args.request_sha256,
            command=_read_command(Path(args.command_file)),
            cwd=str(Path(args.cwd)),
            dialect=args.dialect,
            receipt_script_path=args.receipt_script_path,
            timeout_seconds=int(args.timeout),
            max_output_bytes=int(args.max_output),
            published=announce)
    except Unproven as unproven:
        detail = f": {unproven.detail}" if unproven.detail else ""
        print(f"dell managed submit UNKNOWN: {unproven.reason}{detail}",
              file=sys.stderr)
        print(f"dell managed submit: job {unproven.job_id} is published; keep "
              "its slot and re-read the SAME id, never resubmit",
              file=sys.stderr)
        try:
            write_json_atomic(Path(args.receipt), unproven.receipt)
        except OSError as error:
            print(f"dell managed submit: unknown receipt not written: "
                  f"{str(error)[:DETAIL_MAX]}", file=sys.stderr)
        return unproven.code
    except Stop as stop:
        message = f"dell managed submit refused: {stop.reason}"
        if stop.detail:
            message += f": {stop.detail}"
        print(message, file=sys.stderr)
        return stop.code
    except OSError as error:
        print(f"dell managed submit refused: {str(error)[:DETAIL_MAX]}",
              file=sys.stderr)
        return 1
    # The job is published: its id is already announced, and the receipt below
    # is the reservation proof. A failed local write never hides the live slot.
    try:
        write_json_atomic(Path(args.receipt), receipt)
    except OSError as error:
        print(f"dell managed submit: receipt not written: "
              f"{str(error)[:DETAIL_MAX]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
