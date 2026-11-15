"""Managed-write reservation adapter for the signed submit path.

`lan_bridge.submit_job(..., resource_claims=...)` is the supported
managed-write producer entry: the explicit footprint is reserved and the
signed job is published together under the queue-local referee, so two
producers with the same or nested footprint cannot both publish. The
legacy claim-less submit path never loads this module, and a read-only
caller keeps its unchanged one-publish semantics.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _referee() -> object:
    """Load the queue-side referee next to the worker package.

    The hop is local and one-way (the referee never imports the bridge),
    so the signed-queue client still works without the parallel
    prerequisite package as long as no managed-write claim is declared.
    """

    directory = Path(__file__).resolve().parent.parent / "lenovo_worker"
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    try:
        import job_reservations
    except Exception as error:
        raise ValueError(
            f"reservation referee unavailable: {error}") from error
    return job_reservations


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def publish_reserved(queue: Path, key: bytes, bridge: object, payload: dict,
                     claims: object) -> dict:
    """Reserve the explicit footprint, then publish the signed job.

    A refusal is raised as the bridge's own error, so every caller of the
    submit seam sees the same refusal type.
    """

    referee = _referee()
    try:
        return referee.reserve_and_submit(
            Path(queue), key, bridge, payload, claims,
            f"lan-bridge:{os.getpid()}", os.getpid(), _now_iso)
    except referee.ReservationRefused as error:
        raise bridge.BridgeError(f"reservation refused: {error}") from error
