#!/usr/bin/env python3
"""Submit one managed Dell write request through the signed queue.

The three Lenovo-writing Dell routes (``publish``, ``transfer``,
``publish-existing``) must reach the queue as one atomic reserve-and-publish:
the job's explicit Lenovo checkout and worktree footprint is reserved and the
signed job is published under the same queue-local referee lock, so two
producers naming the same or a nested footprint cannot both publish.

This is that single call. It takes the request writer's own
``dell-lan-request/1`` record, re-reads the request file, verifies its
SHA-256 against the digest the writer printed, re-derives the claim set from
those verified bytes, and refuses anything that disagrees. A caller therefore
cannot add, drop, or re-point a claim: the verified request is the only claim
source.

The executed command is likewise derived from the verified request
(``dell_managed_command``): its bytes must equal the one command that request
defines, and that derived text is what gets sealed. A command file swapped
beside the request, a read-only request paired with a write command, or a
writer record whose dialect, driver, working directory, or kind disagrees is
refused before anything is reserved or published.

It never parses shell text, never infers a dialect or a target, and never
holds a claim of its own: the referee publishes its provisional reservation
and removes exactly that record if the job publish fails, and a reservation
stays held until the worker's proof-bound release. Pair it with the bridge
``fetch``/``get`` commands: an unknown outcome keeps the same job id and the
footprint, and only the read-only ``--reconcile`` request (which declares no
claims) may be submitted meanwhile.

The job id is a pure function of the verified request digest
(``dell_managed_job.managed_job_id``), so a lost submit response costs one
read-only ``--recover`` readback of exactly that id: never a scan of other
reservations and never a second write attempt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, Stop, default_bridge_dir, load_bridge, read_key, sha256_bytes,
)
from dell_managed_claims import ClaimRefused, is_write_request, verify_writer_output
from dell_managed_command import (
    ExecutionRefused, expected_command, verified_command,
)
from dell_managed_job import (
    RecoveryRefused, managed_job_id, recovery_state,
)

SCHEMA = "dell-lan-request/1"


def refuse(message: str) -> int:
    print(f"dell managed submit refused: {message}"[:DETAIL_MAX], file=sys.stderr)
    return 1


def _load_output(args) -> dict:
    if args.writer_output == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.writer_output).read_text(encoding="utf-8-sig")
        except OSError as error:
            raise Stop("usage", code=2,
                       detail=f"--writer-output unreadable: {error}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise Stop("usage", code=2,
                   detail=f"--writer-output is not JSON: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise Stop("usage", code=2,
                   detail=f"--writer-output must be one {SCHEMA} record")
    for name in ("requestPath", "requestSha256", "commandFile", "expectedCwd",
                 "receiptScriptPath"):
        if not isinstance(document.get(name), str) or not document[name]:
            raise Stop("usage", code=2,
                       detail=f"--writer-output is missing {name}")
    return document


def _verified_request(document: dict) -> tuple[dict, list[str], str]:
    """The request document named by the writer record, its claims, digest."""

    request_path = Path(document["requestPath"])
    try:
        raw = request_path.read_bytes()
    except OSError as error:
        raise Stop("usage", code=2,
                   detail=f"request file unreadable: {error}") from error
    digest = sha256_bytes(raw)
    if digest != document["requestSha256"]:
        raise Stop("request-mismatch", detail=(
            "request file does not match the digest in the writer output; "
            "rebuild the request instead of editing it"))
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Stop("request-malformed",
                   detail=f"request file is not JSON: {error}") from error
    if not is_write_request(request):
        raise Stop("usage", code=2, detail=(
            "only publish, transfer, and publish-existing requests are managed "
            "writes; base and first keep the unchanged bridge submit"))
    try:
        claims = verify_writer_output(document, request)
    except ClaimRefused as error:
        raise Stop("claims-refused", detail=str(error)) from error
    if document.get("readOnly") is not (not claims):
        raise Stop("claims-refused", detail=(
            "writer output readOnly flag disagrees with the verified request"))
    return request, claims, digest


def _key(bridge, args, request: dict) -> bytes:
    """Load the queue key, proven outside the checkout and the LAN share."""

    shared = request.get("shared")
    if not isinstance(shared, str) or not Path(shared).is_absolute():
        raise Stop("usage", code=2,
                   detail="the request does not name its absolute shared path")
    return read_key(bridge, Path(args.queue), Path(args.key_file),
                    args.key_format, repos=[Path(args.repo)],
                    shared=[Path(shared)])


def _submit(bridge, args, document: dict, request: dict,
            claims: list[str], digest: str, job_id: str) -> str:
    try:
        command_file = Path(document["commandFile"]).read_text(encoding="utf-8-sig")
    except OSError as error:
        raise Stop("usage", code=2,
                   detail=f"command file unreadable: {error}") from error
    try:
        command, parameters = verified_command(
            document, command_file, request, digest)
    except ExecutionRefused as error:
        raise Stop("command-refused", detail=str(error)) from error
    return_file = parameters["returnFile"]
    return_files = [return_file] if return_file else None
    target = parameters["expectedCwd"]
    key = _key(bridge, args, request)
    try:
        return bridge.submit_job(
            str(Path(args.queue)), key, command, target,
            int(args.timeout), int(args.max_output), return_files,
            parameters["dialect"], parameters["driverPath"],
            resource_claims=claims or None,
            expected_working_directory=target,
            job_id=job_id)
    except (bridge.BridgeError, bridge.ProtocolError, ValueError, TypeError) as error:
        raise Stop("submit-refused", detail=f"submit refused: {error}"[:DETAIL_MAX]) from error


def _recover(bridge, args, request: dict, digest: str, job_id: str,
             expects_claims: bool) -> dict:
    """Read the exact id back; this path never publishes anything."""

    try:
        command, _ = expected_command(request, digest)
        return recovery_state(bridge, Path(args.queue), _key(bridge, args, request),
                              job_id, digest, command, expects_claims)
    except ExecutionRefused as error:
        raise Stop("command-refused", detail=str(error)) from error
    except RecoveryRefused as error:
        raise Stop("recovery-unproven", detail=str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dell_managed_submit.py", description=__doc__)
    parser.add_argument("--writer-output", required=True,
                        help="the dell_request.py JSON record, or - for stdin")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--repo", required=True,
                        help="Dell checkout the key must be proven outside")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-output", type=int, default=65536)
    parser.add_argument("--bridge-dir", default=None,
                        help="lan_bridge scripts dir (default: this repository's)")
    parser.add_argument(
        "--recover", action="store_true",
        help="read the exact job id back and print its authenticated state; "
             "never publishes, never scans another reservation")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = _load_output(args)
        request, claims, digest = _verified_request(document)
        bridge = load_bridge(Path(args.bridge_dir) if args.bridge_dir
                             else default_bridge_dir())
        for name in ("submit_job", "load_key", "BridgeError", "ProtocolError",
                     "read", "open_envelope"):
            if not hasattr(bridge, name):
                raise Stop("driver-stale",
                           detail=f"bridge API incomplete: missing {name}")
        job_id = managed_job_id(digest)
        if args.recover:
            state = _recover(bridge, args, request, digest, job_id,
                             expects_claims=bool(claims))
        else:
            job_id = _submit(bridge, args, document, request, claims, digest,
                             job_id)
    except Stop as stop:
        if stop.code == 2:
            print(f"dell managed submit usage: {stop.detail or stop.reason}",
                  file=sys.stderr)
            return 2
        return refuse(stop.detail or stop.reason)
    except OSError as error:
        return refuse(str(error)[:DETAIL_MAX])
    if args.recover:
        print(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
        return 0
    print(f"JOBID={job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
