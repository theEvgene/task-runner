"""Minimal authoritative reservation prerequisite for bounded parallelism.

This is the narrow seam #88/#89/#90/#91 describe, limited to what #82/#83
need: one signed, job-bound reservation per explicit managed-write
footprint, an atomic referee that refuses overlapping *different* jobs
before publication, and worker-side verification that a job's
parallel-eligible resource identity came from that reservation rather than
from text inside the job payload.

Contract:

* A reservation is a MAC-sealed ``kind=reservation`` envelope in
  ``<queue>/reservations/<job-id>.json``. It names the owner, generation,
  canonical resource set, resource digest, and the SHA-256 of the exact
  signed job bytes it belongs to. Filename authority is never trusted.
* Resource identities are normalized here (absolute, case-folded,
  ``normpath`` paths; and one exclusive ``tcp:*:<port>`` port token per
  port). The TCP address is deliberately dropped: a wildcard bind, a
  loopback alias, ``localhost``, and a DNS name can all name one
  effective port, so two claims naming the same port collide instead of
  being compared as text. Relative, unparsable, or unknown-type claims
  are refused: an unprovable footprint is never admitted for parallel
  execution.
* ``referee_publish`` holds one queue-local operating-system advisory
  lock (an exclusive byte-range/file lock held by the producer process)
  while it scans live reservations and publishes the reservation plus the
  job envelope, so two producers with the same or nested footprint cannot
  both publish. The operating system releases the lock when its holder
  exits, so elapsed age can never break a live lock, and a same-host owner
  that provably died needs no heuristic takeover. A refused attempt
  publishes nothing; a failed job publish removes only its own provisional
  reservation.
* A reservation is live until its exact job has an authenticated release
  record in ``<queue>/releases/<job-id>.json`` bound to the job bytes, the
  reservation resources, the claim generation, and the exact result bytes.
  A signed result alone never frees a footprint (#91), and a missing job
  means the producer aborted under the lock, so the record is inert. Age or
  a timeout never frees it; only the proof-bound release does.
* Producer ``resource_claims`` inside the job payload stay a declaration,
  not authority: without a verified reservation the worker keeps the
  ``exclusive:global`` sequential fallback.

The mutex lives under the queue, never in a worktree, and is the same
atomic no-clobber filesystem the queue already relies on.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

import job_lifecycle

SCHEMA = "lenovo-job-reservation/1"
KIND = "reservation"
RELEASE_SCHEMA = "lenovo-job-release/1"
CLAIM_SCHEMA = "lenovo-job-claim/1"
LOCK_NAME = ".referee.lock"
LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.02
MAX_RESOURCES = 17
MAX_CLAIM_LEN = 256
# Only a genuine "somebody holds it" errno is contention. Anything else is
# an unusable mutex and refuses instead of being retried into a timeout.
_LOCK_CONTENTION = {
    errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EINTR,
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
}

_PATH_CLAIM_PREFIXES = ("working-directory", "file")
_CLAIM_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}:[^\s]{1,200}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


class ReservationRefused(ValueError):
    """The reservation is absent, unprovable, or conflicts."""


def _hex32(value: object) -> bool:
    return isinstance(value, str) and _HEX32_RE.fullmatch(value) is not None


def _normalized_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise ReservationRefused(f"relative resource path: {value}")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ReservationRefused(f"unresolvable resource path: {value}") from error
    return os.path.normcase(os.path.normpath(os.fspath(resolved)))


def normalize_claim(claim: object) -> str:
    """Return the canonical form of one explicit resource claim."""

    if (not isinstance(claim, str) or len(claim) > MAX_CLAIM_LEN
            or _CLAIM_RE.fullmatch(claim) is None):
        raise ReservationRefused("bad reservation resource claim")
    kind, _, value = claim.partition(":")
    if kind in _PATH_CLAIM_PREFIXES:
        return f"{kind}:{_normalized_path(value)}"
    if kind == "tcp":
        host, separator, port = value.rpartition(":")
        if not separator or not host:
            raise ReservationRefused(f"bad tcp claim: {claim}")
        try:
            number = int(port)
        except ValueError as error:
            raise ReservationRefused(f"bad tcp port: {claim}") from error
        if not 1 <= number <= 65535:
            raise ReservationRefused(f"tcp port out of range: {claim}")
        # One exclusive token per port: wildcard binds, loopback aliases,
        # `localhost`, and DNS names cannot be compared reliably, so the
        # address is not part of the authority. Same port means conflict.
        return f"tcp:*:{number}"
    raise ReservationRefused(f"unprovable resource type: {kind}")


def normalize_resources(claims: object,
                        workdir_resource: str) -> tuple[str, ...]:
    """Canonical resource set; the worker's own workdir is never omitted."""

    if not isinstance(claims, (list, tuple)) or not claims:
        raise ReservationRefused("reservation resources are empty")
    normalized = [normalize_claim(claim) for claim in claims]
    if len(set(normalized)) != len(normalized):
        raise ReservationRefused("duplicate reservation resource")
    workdir = normalize_claim(workdir_resource)
    if not workdir.startswith("working-directory:"):
        raise ReservationRefused("workdir resource has the wrong type")
    if workdir not in normalized:
        normalized.append(workdir)
    if len(normalized) > MAX_RESOURCES:
        raise ReservationRefused("too many reservation resources")
    return tuple(sorted(normalized))


def resource_digest(resources: tuple[str, ...] | list[str]) -> str:
    material = json.dumps(list(resources), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def build(job_id: str, job_envelope_sha256: str, claims: object,
          workdir_resource: str, owner_instance: str, owner_pid: int,
          generation: str | None, now) -> dict:
    """Build one reservation record; normalization happens exactly here."""

    if not _hex32(job_id):
        raise ReservationRefused("bad reservation job id")
    if not (isinstance(job_envelope_sha256, str)
            and len(job_envelope_sha256) == 64
            and all(c in "0123456789abcdef"
                    for c in job_envelope_sha256)):
        raise ReservationRefused("bad job envelope digest")
    if not isinstance(owner_instance, str) or not owner_instance:
        raise ReservationRefused("bad reservation owner")
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) \
            or owner_pid <= 0:
        raise ReservationRefused("bad reservation owner pid")
    generation = generation or uuid.uuid4().hex
    if not _hex32(generation):
        raise ReservationRefused("bad reservation generation")
    resources = normalize_resources(claims, workdir_resource)
    return {
        "schema": SCHEMA, "kind": KIND, "id": job_id,
        "owner": {"instance": owner_instance, "pid": int(owner_pid)},
        "generation": generation,
        "resources": list(resources),
        "resource_digest": resource_digest(resources),
        "job_envelope_sha256": job_envelope_sha256,
        "state": "reserved",
        "reserved_at": now(),
    }


def validate(record: object, job_id: str,
             job_envelope_sha256: str) -> bool:
    """Every binding is re-derived; caller-supplied equality is not trust."""

    if not isinstance(record, dict):
        return False
    owner = record.get("owner")
    resources = record.get("resources")
    if not (record.get("schema") == SCHEMA
            and record.get("kind") == KIND
            and record.get("id") == job_id
            and record.get("state") == "reserved"
            and isinstance(owner, dict)
            and isinstance(owner.get("instance"), str)
            and owner["instance"]
            and isinstance(owner.get("pid"), int)
            and not isinstance(owner.get("pid"), bool)
            and owner["pid"] > 0
            and _hex32(record.get("generation"))
            and isinstance(resources, list)
            and 1 <= len(resources) <= MAX_RESOURCES
            and all(isinstance(item, str) for item in resources)
            and len(set(resources)) == len(resources)
            and record.get("resource_digest") == resource_digest(resources)
            and record.get("job_envelope_sha256") == job_envelope_sha256
            and isinstance(record.get("reserved_at"), str)
            and record.get("reserved_at")):
        return False
    try:
        canonical = [normalize_claim(item) for item in resources]
    except ReservationRefused:
        return False
    return canonical == list(resources) and resources == sorted(resources)


def _reservation_path(queue: Path, job_id: str) -> Path:
    return Path(queue) / "reservations" / f"{job_id}.json"


def read(queue: Path, key: bytes, bridge: object, job_id: str,
         job_envelope_sha256: str) -> dict | None:
    """Read and fully verify a job's reservation; None when absent."""

    path = _reservation_path(queue, job_id)
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


def _job_bytes(queue: Path, key: bytes, bridge: object,
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


def _released(queue: Path, key: bytes, bridge: object, record: dict,
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
        and _hex32(claim_record.get("generation"))
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
        if not _hex32(job_id):
            raise ReservationRefused(f"bad reservation file name: {name}")
        try:
            raw = bridge.read(str(directory), job_id)
            record = bridge.open_envelope(raw, key, KIND, job_id)
        except Exception as error:
            raise ReservationRefused(
                f"foreign reservation unreadable: {name} ({error})") from error
        raw_job = _job_bytes(queue, key, bridge, job_id)
        if raw_job is None:
            # No job exists: the producer aborted under the referee mutex,
            # so there is nothing to execute and nothing to hold.
            continue
        digest = hashlib.sha256(raw_job).hexdigest()
        if not validate(record, job_id, digest):
            raise ReservationRefused(
                f"foreign reservation binding invalid: {job_id}")
        if not _released(queue, key, bridge, record, digest):
            records.append(record)
    return records


def _try_lock(descriptor: int) -> bool:
    """One non-blocking attempt at the queue-local exclusive lock."""

    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in _LOCK_CONTENTION:
            return False
        raise ReservationRefused(
            f"reservation mutex unavailable: {error}") from error
    return True


def _unlock(descriptor: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def _referee_lock(queue: Path, wait_seconds: float = LOCK_WAIT_SECONDS):
    """Hold the queue-local referee lock for one check-and-publish.

    The authority is an operating-system lock held by this process, not a
    timestamp: the kernel releases it when the holder exits, so a live
    holder can never be evicted no matter how much wall time passes and no
    stale-lock heuristic is needed. A contender waits its bounded turn and
    then refuses; it never steals.
    """

    directory = Path(queue) / "reservations"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(os.fspath(directory / LOCK_NAME),
                             os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise ReservationRefused(
            f"reservation mutex unavailable: {error}") from error
    deadline = time.monotonic() + float(wait_seconds)
    try:
        if os.fstat(descriptor).st_size == 0:
            try:
                os.write(descriptor, b"\0")
            except OSError:
                # A concurrent creator may hold the byte already; the lock
                # attempt below decides, and reports contention not failure.
                pass
        while not _try_lock(descriptor):
            if time.monotonic() >= deadline:
                raise ReservationRefused("reservation mutex is held")
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(descriptor)
    finally:
        os.close(descriptor)


def referee_publish(queue: Path, key: bytes, bridge: object, record: dict,
                    publish_job,
                    wait_seconds: float = LOCK_WAIT_SECONDS) -> str:
    """Atomically reserve a footprint and publish its signed job.

    Returns ``"reserved"`` or ``"owned"`` (an identical reservation and
    job already exist). Raises ``ReservationRefused`` on conflict or on
    any unprovable existing record; a refused attempt leaves no claim and
    publishes no job.
    """

    job_id = record["id"]
    resources = tuple(record["resources"])
    with _referee_lock(queue, wait_seconds):
        for other in live_records(queue, key, bridge):
            if other["id"] == job_id:
                continue
            if job_lifecycle.resources_overlap(
                    resources, tuple(other["resources"])):
                raise ReservationRefused(
                    f"resource conflict with job {other['id']}")
        job_path = Path(queue) / "jobs" / f"{job_id}.json"
        if job_path.is_file():
            # A published job is never given a reservation after the
            # fact: that could admit a new job beside a running one.
            raw_job = _job_bytes(queue, key, bridge, job_id)
            if (raw_job is None
                    or hashlib.sha256(raw_job).hexdigest()
                    != record["job_envelope_sha256"]):
                raise ReservationRefused(
                    "job already published with different bytes")
            existing = read(
                queue, key, bridge, job_id, record["job_envelope_sha256"])
            if existing is None:
                raise ReservationRefused(
                    "job already published without a reservation")
            if (existing["resources"] != list(resources)
                    or existing["resource_digest"]
                    != record["resource_digest"]):
                raise ReservationRefused(
                    "existing reservation has a different footprint")
            return "owned"
        directory = Path(queue) / "reservations"
        bridge.publish(str(directory), job_id, bridge.seal(record, key))
        try:
            publish_job()
        except Exception:
            try:
                _reservation_path(queue, job_id).unlink()
            except OSError:
                pass
            raise
        return "reserved"


def reserve_and_submit(queue: Path, key: bytes, bridge: object,
                       payload: dict, claims: object, owner_instance: str,
                       owner_pid: int, now,
                       generation: str | None = None,
                       wait_seconds: float = LOCK_WAIT_SECONDS) -> dict:
    """Producer entry point: reserve the explicit footprint, then publish.

    The job envelope bytes are sealed first so the reservation binds the
    exact bytes a worker will verify. A failed job publish removes this
    attempt's own provisional reservation only.
    """

    job_id = payload["id"]
    job_bytes = bridge.seal(payload, key)
    workdir = job_lifecycle.resource_identity(payload["working_directory"])
    record = build(
        job_id, hashlib.sha256(job_bytes).hexdigest(), claims, workdir,
        owner_instance, owner_pid, generation, now)
    outcome = referee_publish(
        queue, key, bridge, record,
        lambda: bridge.publish(str(Path(queue) / "jobs"), job_id, job_bytes),
        wait_seconds)
    if outcome == "owned":
        stored = read(queue, key, bridge, job_id,
                      record["job_envelope_sha256"])
        if stored is None:
            raise ReservationRefused(
                "owned reservation disappeared after referee decision")
        return stored
    return record
