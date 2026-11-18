"""Authenticated reservation store: bound reads, live scan, release proof.

This module is the only reader of the queue's reservation, job, claim and
result envelopes. Everything it returns has been verified against the exact
signed job bytes, so a caller never has to trust a filename, an age, or a
self-report.

Contract:

* ``read`` returns a job's reservation only when the envelope is sealed and
  every binding is re-derived; a present-but-unprovable record refuses.
* ``live_records`` scans the store and fails closed: an unreadable foreign
  reservation is an error, never a silently ignored file. A record whose job
  file is absent is inert (the producer aborted under the referee lock), so
  it holds nothing.
* ``released`` is true only for an exact, proof-bound release of this
  reservation: sealed, naming this job's exact bytes and resource set, the
  generation of the authenticated claim that owns the job, and the digest of
  the exact result bytes. A signed result alone never frees a footprint
  (#91), and age or a timeout never frees one either.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from job_reservation_claims import (KIND, hex32, normalize_resources,
                                    validate)
from job_reservation_errors import ReservationRefused

RELEASE_SCHEMA = "lenovo-job-release/1"
CLAIM_SCHEMA = "lenovo-job-claim/1"


def reservation_path(queue: Path, job_id: str) -> Path:
    return Path(queue) / "reservations" / f"{job_id}.json"


def read(queue: Path, key: bytes, bridge: object, job_id: str,
         job_envelope_sha256: str) -> dict | None:
    """Read and fully verify a job's reservation; None when absent."""

    path = reservation_path(queue, job_id)
    if not path.is_file():
        return None
    try:
        raw = bridge.read(str(path.parent), job_id)
        record = bridge.open_envelope(raw, key, KIND, job_id)
    except Exception as error:
        raise ReservationRefused(
            f"reservation unreadable or unauthenticated: {error}") from error
    if not validate(record, job_id, job_envelope_sha256):
        raise ReservationRefused("reservation binding mismatch")
    return record


def authoritative_resources(record: dict,
                            workdir_resource: str) -> tuple[str, ...]:
    """The worker's admission identity, derived only from the reservation."""

    return normalize_resources(record["resources"], workdir_resource)


def job_bytes(queue: Path, key: bytes, bridge: object,
              job_id: str) -> bytes | None:
    """Exact signed job bytes, or None only when no job file exists."""

    if not (Path(queue) / "jobs" / f"{job_id}.json").is_file():
        return None
    try:
        raw = bridge.read(str(Path(queue) / "jobs"), job_id)
    except Exception as error:
        raise ReservationRefused(
            f"reserved job unreadable: {job_id} ({error})") from error
    bridge.open_envelope(raw, key, "job", job_id)
    return raw


def _authenticated(queue: Path, key: bytes, bridge: object, directory: str,
                   kind: str,
                   job_id: str) -> tuple[dict, bytes] | None:
    """Payload plus raw bytes of one authenticated record, or None if absent."""

    path = Path(queue) / directory / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        raw = bridge.read(str(path.parent), job_id)
        return bridge.open_envelope(raw, key, kind, job_id), raw
    except Exception as error:
        raise ReservationRefused(
            f"{kind} record unreadable: {job_id} ({error})") from error


def released(queue: Path, key: bytes, bridge: object, record: dict,
             job_digest: str) -> bool:
    """True only for an exact, proof-bound release of this reservation.

    A signed result is not termination authority (#91): the release record
    must be sealed, must name this job's exact bytes and resource set, must
    carry the generation of the authenticated claim that owns the job, and
    must bind the digest of the exact result bytes. Anything absent,
    unauthenticated, foreign, or mismatched keeps the footprint held.
    """

    try:
        release = _authenticated(queue, key, bridge, "releases", "release",
                                 record["id"])
        claim = _authenticated(queue, key, bridge, "claims", "claim",
                               record["id"])
        result = _authenticated(queue, key, bridge, "results", "result",
                                record["id"])
    except ReservationRefused:
        return False
    if release is None or claim is None or result is None:
        return False
    release_record = release[0]
    claim_record = claim[0]
    result_raw = result[1]
    owner = claim_record.get("owner")
    resources = list(record["resources"])
    return (
        release_record.get("schema") == RELEASE_SCHEMA
        and release_record.get("kind") == "release"
        and release_record.get("id") == record["id"]
        and release_record.get("generation") == claim_record.get("generation")
        and release_record.get("owner") == owner
        and release_record.get("job_envelope_sha256") == job_digest
        and release_record.get("resources") == resources
        and release_record.get("result_envelope_sha256")
        == hashlib.sha256(result_raw).hexdigest()
        and claim_record.get("schema") == CLAIM_SCHEMA
        and claim_record.get("kind") == "claim"
        and claim_record.get("id") == record["id"]
        and hex32(claim_record.get("generation"))
        and isinstance(owner, dict)
        and isinstance(owner.get("instance"), str) and owner["instance"]
        and isinstance(owner.get("pid"), int)
        and not isinstance(owner.get("pid"), bool)
        and claim_record.get("job_envelope_sha256") == job_digest
        and claim_record.get("resources") == resources
    )


def live_records(queue: Path, key: bytes,
                 bridge: object) -> list[dict]:
    """Verified reservations whose job may still execute; fail closed."""

    directory = Path(queue) / "reservations"
    try:
        names = sorted(path.name for path in directory.iterdir()
                       if path.is_file() and path.name.endswith(".json"))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ReservationRefused(
            f"reservation store unavailable: {error}") from error
    records: list[dict] = []
    for name in names:
        job_id = name[:-len(".json")]
        if not hex32(job_id):
            raise ReservationRefused(f"bad reservation file name: {name}")
        try:
            raw = bridge.read(str(directory), job_id)
            record = bridge.open_envelope(raw, key, KIND, job_id)
        except Exception as error:
            raise ReservationRefused(
                f"foreign reservation unreadable: {name} ({error})") from error
        raw_job = job_bytes(queue, key, bridge, job_id)
        if raw_job is None:
            # No job exists: the producer aborted under the referee mutex,
            # so there is nothing to execute and nothing to hold.
            continue
        digest = hashlib.sha256(raw_job).hexdigest()
        if not validate(record, job_id, digest):
            raise ReservationRefused(
                f"foreign reservation binding invalid: {job_id}")
        if not released(queue, key, bridge, record, digest):
            records.append(record)
    return records
