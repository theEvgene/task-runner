"""Exact managed-write job identity, and the read-only readback of that id.

A managed submit reserves the footprint and publishes the signed job in one
call, then prints the id. When that response is lost (client killed, terminal
closed, transport error) the id must still be recoverable exactly. Scanning
``queue/reservations/*.json`` would be a guess about which held record belongs
to this request, and re-submitting the write would be a write attempt made to
learn an id.

The id is therefore a pure function of the request digest the writer already
recorded: ``sha256("dell-managed-job-id/1\\0" + requestSha256)[:32]``. The same
verified request always names the same job, so a lost response costs one
read-only readback at that exact id. The derivation is a deterministic name,
not a secret and not an authentication token: every state below is established
from the signed envelope, and a job under the id whose command is not this
request's own command is refused rather than reported as recovered.

``recovery_state`` publishes nothing, scans no other id, and trusts no file
name: it opens exactly ``jobs/<id>.json``, ``results/<id>.json`` and
``reservations/<id>.json`` and re-derives their bindings.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

SCHEMA = "dell-managed-recovery/1"
ID_SCHEMA = "dell-managed-job-id/1"
_ID_DOMAIN = b"dell-managed-job-id/1\0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DETAIL_MAX = 200


class RecoveryRefused(ValueError):
    """The exact managed-write state cannot be established from queue bytes."""


def managed_job_id(request_sha256: str) -> str:
    """The one job id this verified request names; refuses anything else."""

    if (not isinstance(request_sha256, str)
            or _SHA256_RE.fullmatch(request_sha256) is None):
        raise ValueError("request digest must be 64 lowercase hex")
    material = _ID_DOMAIN + request_sha256.encode("ascii")
    return hashlib.sha256(material).hexdigest()[:32]


def _opened(bridge, directory: Path, key, kind: str,
            job_id: str) -> tuple[dict, bytes] | None:
    """The authenticated record at exactly this id, or None when absent."""

    if not (directory / f"{job_id}.json").is_file():
        return None
    try:
        raw = bridge.read(str(directory), job_id)
        return bridge.open_envelope(raw, key, kind, job_id), raw
    except Exception as error:
        raise RecoveryRefused(
            f"{kind} record unreadable or unauthenticated: "
            f"{str(error)[:_DETAIL_MAX]}") from error


def _store() -> object:
    """Load the queue-side reservation store for the release-proof check."""

    directory = Path(__file__).resolve().parent.parent / "lenovo_worker"
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    try:
        import job_reservation_store
    except Exception as error:
        raise RecoveryRefused(
            "reservation store unavailable: "
            f"{str(error)[:_DETAIL_MAX]}") from error
    return job_reservation_store


def recovery_state(bridge, queue: Path, key, job_id: str,
                   request_sha256: str, command: str,
                   expects_claims: bool) -> dict:
    """Read the exact id back; raise instead of guessing on any mismatch."""

    queue = Path(queue)
    job = _opened(bridge, queue / "jobs", key, "job", job_id)
    if job is not None and job[0].get("command") != command:
        raise RecoveryRefused(
            "the job under this id is not this request's command")
    job_digest = hashlib.sha256(job[1]).hexdigest() if job else ""
    result = _opened(bridge, queue / "results", key, "result", job_id)
    reservation = "absent"
    held: bool | None = False
    resources: list = []
    if job is None:
        # A reservation file without its job is inert: the producer aborted
        # under the referee lock, so it executes nothing and holds nothing.
        if (queue / "reservations" / f"{job_id}.json").is_file():
            reservation = "inert"
    elif (queue / "reservations" / f"{job_id}.json").is_file():
        store = _store()
        try:
            record = store.read(queue, key, bridge, job_id, job_digest)
        except Exception as error:
            raise RecoveryRefused(
                "reservation unreadable or unauthenticated: "
                f"{str(error)[:_DETAIL_MAX]}") from error
        if record is None:
            reservation = "missing"
            held = None
        else:
            reservation = "present"
            resources = list(record["resources"])
            held = not store.released(queue, key, bridge, record, job_digest)
    elif expects_claims:
        # A published managed write without its reservation proves nothing
        # about the footprint; report it as unprovable, never as free.
        reservation = "missing"
        held = None
    state = {
        "schema": SCHEMA, "idSchema": ID_SCHEMA, "jobId": job_id,
        "requestSha256": request_sha256,
        "job": "published" if job is not None else "absent",
        "jobEnvelopeSha256": job_digest,
        "result": "present" if result is not None else "absent",
        "reservation": reservation, "footprintHeld": held,
        "resources": resources,
    }
    if result is not None:
        state["resultStatus"] = result[0].get("status")
        state["exitCode"] = result[0].get("exit_code")
    return state
