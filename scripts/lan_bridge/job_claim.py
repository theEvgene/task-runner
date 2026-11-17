"""One shape check for the signed ``lenovo-job-claim/1`` lease record.

The Lenovo worker decides whether to honour a claim with it, and the
observer-side ``wait`` reader decides whether the same claim may be reported
as an owner. Two callers, one predicate, so the two sides cannot drift into a
second, weaker validator.

Callers pass only what their host can verify. ``resources=None`` means the
caller cannot derive the worker's resource set from the signed job (the
observer cannot: the set comes from worker-side workdir resolution and, when
present, a signed reservation), never that any resource set is acceptable.
The owning worker always passes the exact set it would admit.
"""

from __future__ import annotations

import re

CLAIM_SCHEMA = "lenovo-job-claim/1"
HELD_STATE = "owned-unknown-until-signed-result"

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def claim_matches(payload: object, *, job_id: str,
                  job_envelope_sha256: str,
                  resources: object = None) -> bool:
    """True only when this claim may hold this exact signed job.

    Checks every field both sides can verify: schema, kind, bound id, owner
    instance/pid types, a 32-hex generation, the exact job envelope digest
    and the held state. ``resources`` is compared only when the caller
    supplies the exact set it would admit.
    """
    if not isinstance(payload, dict):
        return False
    owner = payload.get("owner")
    generation = payload.get("generation")
    if not (payload.get("schema") == CLAIM_SCHEMA
            and payload.get("kind") == "claim"
            and payload.get("id") == job_id
            and isinstance(owner, dict)
            and isinstance(owner.get("instance"), str) and owner["instance"]
            and isinstance(owner.get("pid"), int)
            and not isinstance(owner["pid"], bool)
            and isinstance(generation, str)
            and _HEX32.fullmatch(generation) is not None
            and payload.get("state") == HELD_STATE
            and isinstance(job_envelope_sha256, str)
            and payload.get("job_envelope_sha256") == job_envelope_sha256):
        return False
    if resources is not None:
        return payload.get("resources") == list(resources)
    return True
