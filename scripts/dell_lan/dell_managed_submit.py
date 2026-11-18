#!/usr/bin/env python3
"""Submit one Dell managed write with its authoritative resource reservation.

``dell_request.py`` writes the request file and its command file and never
submits. This is the Dell producer entry for the managed-write routes
(``transfer``, ``publish``, ``publish-existing``): it hands the sealed command
to ``lan_bridge.submit_job(..., resource_claims=...)`` so the signed job and
its reservation are published together under the queue-local referee (#89,
#90, #91). Nothing here re-implements the referee or adds a second queue.

The claim set is derived only from the digest-bound request file, never from
free command text: the request's explicit Lenovo task worktree is the one
write footprint this route can prove. The job's working directory is reserved
by the referee itself from the signed payload, so the checkout a Dell route
pins is covered without a second spelling.

Binding rules (fail closed, refuse before any queue write):

* The request file must hash to the declared ``--request-sha256``.
* The schema must be one of the three managed-write requests.
* ``lenovoWorktree`` must be an absolute path; it becomes ``file:<worktree>``.
* The declared revisions must be 40-hex commits (or absent).
* The command file must name this exact request path and its digest, so a
  claim can never be paired with another request's command, revision, or
  worktree.

After publication the job's own reservation is read back and verified against
the exact job bytes; the receipt is that proof. A failed publish removes only
this attempt's provisional reservation (the referee does it), and a footprint
stays held until the worker publishes a proof-bound release, so an unknown
outcome keeps the slot and never invites a blind resubmit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, Stop, default_bridge_dir, load_bridge, read_key, sha256_bytes,
    write_json_atomic,
)
from dell_standalone_common import (
    PUBLISH_EXISTING_REQUEST_SCHEMA, TRANSFER_REQUEST_SCHEMA,
)

RECEIPT_SCHEMA = "dell-lan-managed-submit/1"
MANAGED_SCHEMAS = frozenset({
    "dell-lan-publish-request/1",
    TRANSFER_REQUEST_SCHEMA,
    PUBLISH_EXISTING_REQUEST_SCHEMA,
})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_COMMAND_BYTES = 1024 * 1024


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


def claims_for(request: dict) -> list[str]:
    """The one explicit write footprint this request proves."""

    worktree = request.get("lenovoWorktree")
    if (not isinstance(worktree, str) or not worktree.strip()
            or not Path(worktree).is_absolute()):
        raise Stop("claim-unprovable", detail=(
            "lenovoWorktree must be the absolute task worktree path"))
    return [f"file:{Path(worktree)}"]


def bind_command(command: str, request_path: Path, request_digest: str) -> None:
    """The submitted command must name this request and its digest."""

    if not command.strip():
        raise Stop("command-unreadable", detail="command file is empty")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise Stop("command-unreadable", detail="command file is too large")
    lowered = command.lower()
    if str(request_path).lower() not in lowered or request_digest not in lowered:
        raise Stop("command-unbound", detail=(
            "the command file must name this exact request path and its sha256"))


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


def submit_managed(bridge, queue: Path, key: bytes, *, request_path: Path,
                   request_sha256: str, command: str, cwd: str,
                   dialect: str, receipt_script_path: str,
                   timeout_seconds: int = 900,
                   max_output_bytes: int = 65536) -> dict:
    """Reserve the proven footprint and publish the signed job atomically."""

    request, digest = load_request(Path(request_path), request_sha256)
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        raise Stop("claim-unprovable", code=2, detail=(
            "--cwd must be the absolute Lenovo working directory"))
    pinned = revisions(request)
    claims = claims_for(request)
    bind_command(command, Path(request_path), digest)
    try:
        job_id = bridge.submit_job(
            str(queue), key, command, cwd, timeout_seconds, max_output_bytes,
            None, dialect, receipt_script_path,
            resource_claims=claims, expected_working_directory=cwd)
    except bridge.BridgeError as error:
        # The referee refused before publishing: no job, no reservation, and
        # nothing to roll back. The caller may fix the footprint and resubmit.
        raise Stop("reservation-refused", code=2,
                   detail=str(error)[:DETAIL_MAX]) from error
    except (ValueError, TypeError, OSError) as error:
        raise Stop("submit-refused", code=2,
                   detail=str(error)[:DETAIL_MAX]) from error
    receipt = _proof(bridge, Path(queue), key, job_id, claims, cwd, command,
                     Path(request_path), digest, request, pinned)
    return receipt


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
                        help="absolute Lenovo working directory for the job")
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
            max_output_bytes=int(args.max_output))
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
    # The job is published: its id is reported even when the receipt cannot
    # be written, so a failed local write never hides a live reservation.
    print(f"JOBID={receipt['jobId']}")
    try:
        write_json_atomic(Path(args.receipt), receipt)
    except OSError as error:
        print(f"dell managed submit: receipt not written: "
              f"{str(error)[:DETAIL_MAX]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
