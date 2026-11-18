"""The one queue-local authority for shared ``capacity:`` quotas.

A ``capacity:<name>`` claim is a shared quota, never an exclusive owner, and
it is admitted only against an explicit operator configuration: nothing is
inferred, guessed from shell text, or remembered from an earlier call.

Contract:

* The authority is ``<queue>/capacity-quotas.json``, a plain JSON object.
  It lives beside the reservation store rather than inside it, so the store
  keeps its ``<job-id>.json`` shape.
* Every producer behind the queue reads that file inside the referee lock,
  so no call can raise a ceiling another call admitted under, and two
  concurrent producers count against the same bound.
* A caller-supplied mapping is only an assertion: every name it states must
  equal the authority, and a different value is refused by naming the
  resource instead of admitting it under the caller's ceiling.
* A missing file admits no capacity claim; an unreadable, oversized,
  malformed, or ambiguous (fold-colliding) file refuses rather than being
  narrowed or treated as "no quota".
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from job_reservation_claims import CAPACITY_NAME_RE, CAPACITY_PREFIX
from job_reservation_errors import ReservationRefused

MAX_CAPACITIES = 8
MAX_QUOTA = 256
MAX_CAPACITY_CONFIG_BYTES = 64 * 1024
# The one authoritative quota configuration for a queue. It lives beside
# the reservation store, never inside it, so the store keeps its
# ``<job-id>.json`` shape, and it is plain operator input rather than a
# second ledger: nothing is derived, cached, or remembered from a call.
CAPACITY_CONFIG_NAME = "capacity-quotas.json"


def capacity_limits(capacities: object) -> dict[str, int]:
    """Validated explicit quotas for shared ``capacity:`` claims.

    Capacity is never inferred. ``None`` means no capacity claim is
    admissible, and a malformed or over-bounded configuration refuses
    instead of being silently narrowed. Configured names go through the
    same case fold as claims, so one name cannot be bounded under two
    spellings; a collision after folding is ambiguous and refuses.
    """

    if capacities is None:
        return {}
    if not isinstance(capacities, Mapping):
        raise ReservationRefused("bad capacity quota configuration")
    if len(capacities) > MAX_CAPACITIES:
        raise ReservationRefused("too many configured capacity quotas")
    limits: dict[str, int] = {}
    for name, quota in capacities.items():
        if not isinstance(name, str):
            raise ReservationRefused("bad configured capacity name")
        folded = name.casefold()
        if (CAPACITY_NAME_RE.fullmatch(folded) is None
                or folded in limits):
            raise ReservationRefused("bad configured capacity name")
        if (isinstance(quota, bool) or not isinstance(quota, int)
                or not 1 <= quota <= MAX_QUOTA):
            raise ReservationRefused(f"bad configured capacity quota: {name}")
        limits[folded] = quota
    return limits


def configured_capacity_limits(queue: Path) -> dict[str, int]:
    """The one authoritative quota configuration for this queue.

    Read inside the referee lock by every producer, so a per-call mapping
    cannot raise a ceiling. A missing file admits no capacity claim; an
    unreadable, oversized, or malformed file refuses instead of guessing.
    """

    path = Path(queue) / CAPACITY_CONFIG_NAME
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ReservationRefused(
            f"capacity quota configuration unavailable: {error}") from error
    if stat.st_size > MAX_CAPACITY_CONFIG_BYTES:
        raise ReservationRefused("capacity quota configuration too large")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReservationRefused(
            f"capacity quota configuration unreadable: {error}") from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ReservationRefused(
            "bad capacity quota configuration file") from error
    return capacity_limits(document)


def assert_capacity_assertion(resources: tuple[str, ...], capacities: object,
                              limits: dict[str, int]) -> None:
    """A caller-supplied mapping may only confirm the queue authority.

    A producer asserting a larger, stale, or unknown quota for a name it
    claims is refused by naming that resource, never admitted under its own
    ceiling. Omitting a name asserts nothing. A reservation that claims no
    capacity is not checked, since the quota cannot affect it.
    """

    if capacities is None:
        return
    if not any(token.startswith(CAPACITY_PREFIX) for token in resources):
        return
    asserted = capacity_limits(capacities)
    differing = sorted(name for name, quota in asserted.items()
                       if limits.get(name) != quota)
    if differing:
        detail = ", ".join(f"{CAPACITY_PREFIX}{name}"
                           for name in differing)
        if not limits:
            raise ReservationRefused(
                "capacity quota assertion differs from the queue authority: "
                f"{detail} (no {CAPACITY_CONFIG_NAME} in the queue)")
        raise ReservationRefused(
            "capacity quota assertion differs from the queue authority: "
            + detail)


def require_known_capacities(resources: tuple[str, ...],
                             limits: dict[str, int]) -> None:
    """A capacity claim without an explicit quota is refused, not guessed."""

    for token in resources:
        if (token.startswith(CAPACITY_PREFIX)
                and token[len(CAPACITY_PREFIX):] not in limits):
            raise ReservationRefused(f"unknown capacity quota: {token}")
