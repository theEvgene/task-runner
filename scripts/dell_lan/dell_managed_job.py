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
from the signed envelope, and a job under the id whose command, working
directory, expected working directory, dialect, receipt script path, return
files, or reserved footprint is not this request's is refused rather than
reported as recovered.

``recovery_state`` publishes nothing, scans no other id, and trusts no file
name: it opens exactly ``jobs/<id>.json``, ``results/<id>.json`` and
``reservations/<id>.json`` and re-derives their bindings. An unavailable queue
is refused as ``queue-missing``; it is never evidence that the job is absent.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

SCHEMA = "dell-managed-recovery/1"
ID_SCHEMA = "dell-managed-job-id/1"
_ID_DOMAIN = ID_SCHEMA.encode("ascii") + b"\0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DETAIL_MAX = 200
_BOUND_FIELDS = (
    ("working_directory", "expectedCwd", "working directory"),
    ("expected_working_directory", "expectedCwd",
     "expected working directory"),
    ("dialect", "dialect", "dialect"),
    ("receipt_script_path", "driverPath", "receipt script path"),
)


class RecoveryRefused(ValueError):
    """The exact managed-write state cannot be established from queue bytes."""


def managed_job_id(request_sha256: str) -> str:
    """The one job id this verified request names; refuses anything else."""

    if (not isinstance(request_sha256, str)
            or _SHA256_RE.fullmatch(request_sha256) is None):
        raise ValueError("request digest must be 64 lowercase hex")
    material = _ID_DOMAIN + request_sha256.encode("ascii")
    return hashlib.sha256(material).hexdigest()[:32]


def require_queue(queue: Path) -> Path:
    """Refuse an unavailable queue instead of classifying it as absence."""

    queue = Path(queue)
    if not queue.is_dir():
        raise RecoveryRefused(
            "queue-missing: the queue directory is absent or not mounted")
    for name in ("jobs", "results"):
        if not (queue / name).is_dir():
            raise RecoveryRefused(
                f"queue-missing: the queue has no {name}/ directory")
    reservations = queue / "reservations"
    if reservations.exists() and not reservations.is_dir():
        raise RecoveryRefused(
            "queue-missing: the reservations path is not a directory")
    return queue


def _opened(bridge, directory: Path, key, kind: str,
            job_id: str) -> tuple[dict, bytes] | None:
    """The authenticated record at exactly this id, or None when absent."""

    if not directory.is_dir():
        raise RecoveryRefused(
            f"queue-missing: the {directory.name}/ directory is absent")
    if not (directory / f"{job_id}.json").is_file():
        return None
    try:
        raw = bridge.read(str(directory), job_id)
        return bridge.open_envelope(raw, key, kind, job_id), raw
    except Exception as error:
        raise RecoveryRefused(
            f"{kind} record unreadable or unauthenticated: "
            f"{str(error)[:_DETAIL_MAX]}") from error


def _worker_module(name: str) -> object:
    """Load one queue-side module the release and claim proofs come from."""

    directory = Path(__file__).resolve().parent.parent / "lenovo_worker"
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    try:
        return __import__(name)
    except Exception as error:
        raise RecoveryRefused(
            f"{name} unavailable: {str(error)[:_DETAIL_MAX]}") from error


def _expected_resources(claims: list, expected_cwd: str) -> list[str]:
    """The one footprint this request reserves, normalized like the referee."""

    vocabulary = _worker_module("job_reservation_claims")
    try:
        return list(vocabulary.normalize_resources(
            claims, "working-directory:" + expected_cwd))
    except Exception as error:
        raise RecoveryRefused(
            "the request footprint is not a provable reservation: "
            f"{str(error)[:_DETAIL_MAX]}") from error


def _require_binding(payload: dict, command: str, parameters: dict) -> None:
    """Refuse a job under this id that is not the one this request defines."""

    if payload.get("command") != command:
        raise RecoveryRefused(
            "the job under this id is not this request's command")
    for field, source, label in _BOUND_FIELDS:
        if payload.get(field) != parameters[source]:
            raise RecoveryRefused(
                f"the job under this id is not this request's {label}")
    return_file = parameters["returnFile"]
    if payload.get("return_files") != ([return_file] if return_file else []):
        raise RecoveryRefused(
            "the job under this id is not this request's return files")


def recovery_state(bridge, queue: Path, key, job_id: str,
                   request_sha256: str, parameters: dict, command: str,
                   claims: list) -> dict:
    """Read the exact id back; raise instead of guessing on any mismatch."""

    queue = require_queue(queue)
    job = _opened(bridge, queue / "jobs", key, "job", job_id)
    result = _opened(bridge, queue / "results", key, "result", job_id)
    if job is None and result is not None:
        raise RecoveryRefused(
            "a result exists under this id without its job; the state is "
            "contradictory")
    if job is not None:
        _require_binding(job[0], command, parameters)
    job_digest = hashlib.sha256(job[1]).hexdigest() if job else None
    expects_claims = bool(claims)
    reservation = "absent"
    held: bool | None = False
    resources: list = []
    path = queue / "reservations" / f"{job_id}.json"
    if path.is_file() and not expects_claims:
        raise RecoveryRefused(
            "a read-only request's job has a reservation; the state is "
            "contradictory")
    if job is None and path.is_file():
        # A reservation without its job is unresolved: the producer may have
        # aborted between the two publishes, or may still be completing.
        # Without the referee lock this snapshot cannot prove which, so it is
        # not called free; resubmitting the same record resolves it there.
        reservation = "reservation-only"
        held = None
    elif job is not None and path.is_file():
        store = _worker_module("job_reservation_store")
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
            if list(record["resources"]) != _expected_resources(
                    claims, parameters["expectedCwd"]):
                raise RecoveryRefused(
                    "the reservation under this id is not this request's "
                    "footprint")
            reservation = "present"
            resources = list(record["resources"])
            held = not store.released(queue, key, bridge, record, job_digest)
    elif job is not None and expects_claims:
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
    if job is not None:
        # The request digest binds every execution field above. Timeout and
        # max-output are CLI-only and therefore reported, not proven; the
        # target is verified because the expected and actual directories are
        # both this request's expectedCwd.
        state["execution"] = {
            "requestBound": True,
            "timeoutSeconds": job[0].get("timeout_seconds"),
            "maxOutputBytes": job[0].get("max_output_bytes"),
            "targetDecision": "verified",
        }
    if result is not None:
        state["resultStatus"] = result[0].get("status")
        state["exitCode"] = result[0].get("exit_code")
    return state
