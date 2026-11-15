"""Authenticated resource-scope helpers: declaration validation + fallback.

Producer ``resource_claims`` / ``resource_scope`` inside the job payload
is a self-declaration, never independence proof: an unmodelled side effect
outside the listed set would overlap silently. The only independent
authority is a verified ``lenovo-job-reservation/1`` record bound to the
exact signed job bytes (``job_reservations``).

A job without such a reservation receives ``("exclusive:global", workdir)``
and runs sequentially. This serializes rather than guessing: two
self-attested disjoint sets never admit concurrency.
"""

from __future__ import annotations

import re

MAX_CLAIMS = 16
MAX_CLAIM_LEN = 256
MAX_AUTHORITY_LEN = 128

# Canonical typed claim: "<type>:<value>", no whitespace. Examples:
# "tcp:127.0.0.1:54321", "file:C:\\data\\x", "fixture:job-a".
_CLAIM_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}:[^\s]{1,200}$")


class ResourceRefused(ValueError):
    """The authenticated resource declaration is malformed."""


def build_resources(job: dict, workdir_resource: str) -> tuple[str, ...]:
    """Fallback resource set: fail-closed sequential without a reservation.

    Raises ResourceRefused when ``resource_claims`` is present but
    malformed. A well-formed declaration is still only self-attestation,
    so this returns ("exclusive:global", workdir) and forces sequential
    admission. Parallel admission is decided by ``job_reservations`` from
    a signed reservation bound to the exact job envelope.
    """

    claims = job.get("resource_claims")
    if claims is None:
        return ("exclusive:global", workdir_resource)
    if (
        not isinstance(claims, list)
        or not claims
        or len(claims) > MAX_CLAIMS
        or any(
            not isinstance(item, str)
            or len(item) > MAX_CLAIM_LEN
            or _CLAIM_RE.fullmatch(item) is None
            for item in claims
        )
        or len(set(claims)) != len(claims)
    ):
        raise ResourceRefused("bad authenticated resource_claims")
    # Even a well-formed producer attestation is not #88/#89 authority:
    # the listed set may omit a shared worktree, port, file, or service.
    # Serialize, do not guess. A future lease verifier will replace this
    # fallback only when it can present a live reservation readback bound
    # to this signed job.
    return ("exclusive:global", workdir_resource)
