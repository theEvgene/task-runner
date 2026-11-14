"""Shape and validate exact signed-description proof from a driver MR result."""

from __future__ import annotations

import re

DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def description_proof(mr: dict) -> dict:
    expected = str(mr.get("descriptionExpectedSha256") or "").lower()
    observed = str(mr.get("descriptionObservedSha256") or "").lower()
    return {
        "descriptionVerified": (mr.get("descriptionVerified") is True
                                and DIGEST_RE.match(expected) is not None
                                and observed == expected),
        "descriptionExpectedSha256": expected or None,
        "descriptionObservedSha256": observed or None,
    }
