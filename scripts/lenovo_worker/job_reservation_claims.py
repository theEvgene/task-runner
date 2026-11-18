"""Canonical claim vocabulary and one reservation record's construction.

This module owns what a resource identity *is*: the typed claim grammar, the
path/port/capacity normalization, the digest over a canonical set, and the
record shape with the bindings that ``validate`` re-derives. Nothing here
touches the queue, a lock, or another record, so the normalization can be
read and tested on its own.

Contract:

* A relative, unparsable, or unknown-type claim is refused: an unprovable
  footprint is never admitted for parallel execution.
* Paths are absolute, case-folded, ``normpath`` identities.
* Ports collapse to one exclusive ``tcp:*:<port>`` token; the address is
  deliberately dropped so a wildcard bind, a loopback alias, ``localhost``,
  and a DNS name naming one port collide instead of being compared as text.
* ``capacity:`` claims are the only shared identity, and a claim name and a
  configured quota name share one spelling authority (``CAPACITY_NAME_RE``),
  so one name can never be bounded under two spellings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from job_reservation_errors import ReservationRefused

SCHEMA = "lenovo-job-reservation/1"
KIND = "reservation"
MAX_RESOURCES = 17
MAX_CLAIM_LEN = 256
CAPACITY_PREFIX = "capacity:"

_PATH_CLAIM_PREFIXES = ("working-directory", "file")
_CLAIM_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}:[^\s]{1,200}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
# Shared by ``normalize_claim`` and the configured quota names, so one name
# folds to one spelling everywhere it can be written.
CAPACITY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def hex32(value: object) -> bool:
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
    if kind == "capacity":
        name = value.casefold()
        if CAPACITY_NAME_RE.fullmatch(name) is None:
            raise ReservationRefused(f"bad capacity claim: {claim}")
        return f"{CAPACITY_PREFIX}{name}"
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
        raise ReservationRefused(
            f"too many reservation resources (limit {MAX_RESOURCES})")
    return tuple(sorted(normalized))


def resource_digest(resources: tuple[str, ...] | list[str]) -> str:
    material = json.dumps(list(resources), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def build(job_id: str, job_envelope_sha256: str, claims: object,
          workdir_resource: str, owner_instance: str, owner_pid: int,
          generation: str | None, now) -> dict:
    """Build one reservation record; normalization happens exactly here."""

    if not hex32(job_id):
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
    if not hex32(generation):
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
            and hex32(record.get("generation"))
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
