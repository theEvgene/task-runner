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
  ``normpath`` paths; one exclusive ``tcp:*:<port>`` token per port; and
  explicitly configured shared ``capacity:<name>`` quotas). The TCP
  address is deliberately dropped: a wildcard bind, a loopback alias,
  ``localhost``, and a DNS name can all name one effective port, so two
  claims naming the same port collide instead of being compared as text.
  Relative, unparsable, or unknown-type claims are refused: an unprovable
  footprint is never admitted for parallel execution.
* A ``capacity:`` claim is shared, never exclusive, and is admitted only
  against one **queue-local** quota configuration,
  ``<queue>/capacity-quotas.json`` (configured, never inferred). That file
  is the single authority: it is read inside the referee lock by every
  producer behind the queue, so no call can raise a ceiling another call
  admitted under. A caller-supplied ``capacities`` mapping is only an
  assertion. Live holders are counted and an over-limit attempt is refused
  by naming the limiting resource; an unknown quota fails closed.
* ``referee_publish`` holds one queue-local operating-system advisory
  lock (an exclusive byte-range/file lock held by the producer process)
  while it reads the quota authority, scans live reservations, and
  publishes the reservation plus the job envelope, so two producers with
  the same or nested footprint cannot both publish. The operating system
  releases the lock when its holder exits, so elapsed age can never break
  a live lock, and a same-host owner that provably died needs no
  heuristic takeover. A refused attempt publishes nothing; a failed job
  publish removes only its own provisional reservation. An already
  published job is resolved first, so an idempotent retry stays
  ``owned`` even after the quota configuration changed. A reservation
  whose job never landed is replaced only under the same lock, and only
  when it authenticates to the identical job bytes and footprint.
* A reservation is live until its exact job has an authenticated release
  record in ``<queue>/releases/<job-id>.json`` bound to the job bytes, the
  reservation resources, the claim generation, and the exact result bytes.
  A signed result alone never frees a footprint (#91), and a reservation
  whose job never landed is unresolved, not free: the next identical submit
  replaces it under the lock. Age or a timeout never frees it; only the
  proof-bound release does.
* Producer ``resource_claims`` inside the job payload stay a declaration,
  not authority: without a verified reservation the worker keeps the
  ``exclusive:global`` sequential fallback.

The mutex lives under the queue, never in a worktree, and is the same
atomic no-clobber filesystem the queue already relies on.

Layout (this module is the public surface; import siblings directly only
when you own their seam):

* ``job_reservation_claims`` normalizes claim identities and builds/validates
  one record.
* ``job_reservation_quota`` is the queue-local ``capacity:`` quota authority.
* ``job_reservation_store`` reads, scans, and proves release of authenticated
  records.
* ``job_reservation_errors`` carries the one ``ReservationRefused`` type.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

import job_lifecycle
import job_reservation_quota as quota
from job_reservation_claims import (CAPACITY_PREFIX, KIND, MAX_CLAIM_LEN,
                                    MAX_RESOURCES, SCHEMA, build, hex32,
                                    normalize_claim, normalize_resources,
                                    resource_digest, validate)
from job_reservation_errors import ReservationRefused
from job_reservation_quota import (CAPACITY_CONFIG_NAME, MAX_CAPACITIES,
                                   MAX_CAPACITY_CONFIG_BYTES, MAX_QUOTA,
                                   capacity_limits,
                                   configured_capacity_limits)
from job_reservation_store import (CLAIM_SCHEMA, RELEASE_SCHEMA,
                                   authoritative_resources, job_bytes,
                                   live_records, read, reservation_path)

__all__ = [
    "CAPACITY_CONFIG_NAME", "CAPACITY_PREFIX", "CLAIM_SCHEMA", "KIND",
    "LOCK_NAME", "LOCK_POLL_SECONDS", "LOCK_WAIT_SECONDS", "MAX_CAPACITIES",
    "MAX_CAPACITY_CONFIG_BYTES", "MAX_CLAIM_LEN", "MAX_QUOTA",
    "MAX_RESOURCES", "RELEASE_SCHEMA", "SCHEMA", "ReservationRefused",
    "authoritative_resources", "build", "capacity_limits",
    "configured_capacity_limits", "hex32", "live_records", "normalize_claim",
    "normalize_resources", "read", "referee_publish", "reserve_and_submit",
    "resource_digest", "reservation_path", "validate",
]

LOCK_NAME = ".referee.lock"
LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.02
# Only a genuine "somebody holds it" errno is contention. Anything else is
# an unusable mutex and refuses instead of being retried into a timeout.
_LOCK_CONTENTION = {
    errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EINTR,
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
}


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
                    wait_seconds: float = LOCK_WAIT_SECONDS,
                    capacities: object = None) -> str:
    """Atomically reserve a footprint and publish its signed job.

    ``capacities`` is an optional assertion, never the authority: quotas
    come from ``<queue>/capacity-quotas.json``, which is read inside the
    referee lock. Every name the mapping states must equal that authority.

    Returns ``"reserved"`` or ``"owned"`` (an identical reservation and
    job already exist). Raises ``ReservationRefused`` on conflict or on
    any unprovable existing record; a refused attempt leaves no claim and
    publishes no job. A refusal names the limiting resource or quota.
    """

    job_id = record["id"]
    resources = tuple(record["resources"])
    with _referee_lock(queue, wait_seconds):
        limits = configured_capacity_limits(queue)
        job_path = Path(queue) / "jobs" / f"{job_id}.json"
        if job_path.is_file():
            # An already-published job is resolved before any admission
            # decision: its reservation is settled, so an idempotent retry
            # returns ``owned`` even after the quota configuration changed.
            # A published job is never given a reservation after the fact:
            # that could admit a new job beside a running one.
            raw_job = job_bytes(queue, key, bridge, job_id)
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
        quota.assert_capacity_assertion(resources, capacities, limits)
        quota.require_known_capacities(resources, limits)
        records = live_records(queue, key, bridge)
        for other in records:
            if other["id"] == job_id:
                continue
            conflicts = job_lifecycle.overlapping_resources(
                resources, tuple(other["resources"]))
            if conflicts:
                raise ReservationRefused(
                    f"resource conflict with job {other['id']}: "
                    + ", ".join(conflicts))
        for token in resources:
            if not token.startswith(CAPACITY_PREFIX):
                continue
            limit = limits[token[len(CAPACITY_PREFIX):]]
            held = sum(1 for other in records
                       if other["id"] != job_id
                       and token in other["resources"])
            if held >= limit:
                raise ReservationRefused(
                    f"capacity quota exhausted: {token} "
                    f"(limit {limit}, held {held})")
        directory = Path(queue) / "reservations"
        existing = reservation_path(queue, job_id)
        if existing.is_file():
            # The job is absent and a reservation exists: a previous attempt
            # aborted between the two publishes. The lock proves no producer
            # is mid-flight, so only a record that authenticates to these
            # exact job bytes and this exact footprint may be replaced. A
            # mismatch refuses and is never released; a published job never
            # reaches this branch.
            try:
                previous = read(queue, key, bridge, job_id,
                                record["job_envelope_sha256"])
            except ReservationRefused as error:
                raise ReservationRefused(
                    "inert reservation does not authenticate to this attempt: "
                    f"{error}") from error
            if previous is None:
                raise ReservationRefused(
                    "inert reservation vanished under the referee lock")
            same_footprint = (
                previous["resources"] == list(resources)
                and previous["resource_digest"] == record["resource_digest"])
            if not same_footprint:
                raise ReservationRefused(
                    "inert reservation has a different footprint")
            try:
                existing.unlink()
            except OSError as error:
                raise ReservationRefused(
                    f"inert reservation could not be cleared: {error}"
                ) from error
        bridge.publish(str(directory), job_id, bridge.seal(record, key))
        try:
            publish_job()
        except Exception:
            try:
                reservation_path(queue, job_id).unlink()
            except OSError:
                pass
            raise
        return "reserved"


def reserve_and_submit(queue: Path, key: bytes, bridge: object,
                       payload: dict, claims: object, owner_instance: str,
                       owner_pid: int, now,
                       generation: str | None = None,
                       wait_seconds: float = LOCK_WAIT_SECONDS,
                       capacities: object = None) -> dict:
    """Producer entry point: reserve the explicit footprint, then publish.

    The job envelope bytes are sealed first so the reservation binds the
    exact bytes a worker will verify. ``capacities`` is only an assertion
    of the queue-local quota authority, never a ceiling of its own. A
    failed job publish removes this attempt's own provisional reservation
    only.
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
        wait_seconds, capacities)
    if outcome == "owned":
        stored = read(queue, key, bridge, job_id,
                      record["job_envelope_sha256"])
        if stored is None:
            raise ReservationRefused(
                "owned reservation disappeared after referee decision")
        return stored
    return record
