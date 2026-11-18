"""Typed managed-write claims for the three Lenovo-writing Dell requests.

``publish``, ``transfer``, and ``publish-existing`` already pin the explicit
Lenovo checkout and task worktree they will write. Those two absolute paths
are the job's managed-write footprint: the ``working-directory:`` tokens the
signed submit reserves through the queue-local referee in the same call that
publishes the job.

Contract:

* Claims come from the request document only. ``dell_managed_submit`` re-hashes
  that document against the digest carried inside the signed job before it
  derives them, so a claim list edited beside the request cannot be attached
  to the job.
* A read-only request (``reconcileOnly`` true) declares no claims: the
  recovery probe only observes the branch, worktree, and MR. It must stay
  admissible while the original unknown job still holds the footprint, and
  admission alone never frees that footprint.
* Nothing is normalized here. The referee is the one normalizer; a Dell-side
  pre-normalization would have to be re-derived on Lenovo and could drift
  between the two hosts.
* A managed-write request without a provable absolute checkout and worktree
  refuses instead of submitting a partial footprint.
* ``base`` and ``first`` stay out of this module entirely: they are Lenovo
  reads, so their submit path keeps its unchanged claim-less semantics.
"""

from __future__ import annotations

from pathlib import Path

from dell_lan_common import PUBLISH_REQUEST_SCHEMA
from dell_standalone_common import (PUBLISH_EXISTING_REQUEST_SCHEMA,
                                    TRANSFER_REQUEST_SCHEMA)

WRITE_SCHEMAS = frozenset({
    PUBLISH_REQUEST_SCHEMA, TRANSFER_REQUEST_SCHEMA,
    PUBLISH_EXISTING_REQUEST_SCHEMA,
})
CHECKOUT_FIELD = "lenovoCheckout"
WORKTREE_FIELD = "lenovoWorktree"


class ClaimRefused(ValueError):
    """The request does not prove one explicit managed-write footprint."""


def is_write_request(request: object) -> bool:
    """True for the three Dell routes whose Lenovo job writes."""

    return isinstance(request, dict) and request.get("schema") in WRITE_SCHEMAS


def derived_claims(request: dict) -> list[str]:
    """The exact claim set this request proves; empty for a read-only one.

    A read-only request writes nothing, so it reserves nothing: the referee
    is the only authority that turns a footprint into admission, and a probe
    that reserved the worktree would deadlock behind the very unknown job it
    is meant to observe.
    """

    if not is_write_request(request):
        return []
    read_only = request.get("reconcileOnly")
    if read_only is True:
        return []
    if read_only is not False:
        raise ClaimRefused(
            "request does not state reconcileOnly as a boolean")
    claims: list[str] = []
    for field, label in ((CHECKOUT_FIELD, "--lenovo-checkout"),
                         (WORKTREE_FIELD, "--lenovo-worktree")):
        value = request.get(field)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ClaimRefused(
                f"{label} must be the absolute path on the Lenovo machine")
        claim = f"working-directory:{value}"
        if claim not in claims:
            claims.append(claim)
    return claims


def verify_writer_output(document: object, request: dict) -> list[str]:
    """The claims of a writer record, or a refusal when they disagree.

    The request is already re-hashed by the caller, so this comparison is the
    one place that decides which claims the job is submitted with: a caller
    can neither add, drop, nor re-point one.
    """

    if not isinstance(document, dict):
        raise ClaimRefused("request writer output is not an object")
    stated = document.get("resourceClaims")
    if not isinstance(stated, list) or any(
            not isinstance(item, str) for item in stated):
        raise ClaimRefused("request writer output has no claim list")
    claims = derived_claims(request)
    if stated != claims:
        raise ClaimRefused(
            "stated claims do not match the verified request footprint")
    return claims
