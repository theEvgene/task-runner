"""Fixture GitLab MR reader seam for the mirror-mr tests (never production).

Implements the narrow surface `lenovo_mr_export.py` consumes from
`--flow-dir` (`read_open_mr`) and the surface
`lenovo_mr_history_export.py` consumes (`read_merged_mr`). Both return
the MR record for one integer IID from the disposable fixture state
file (`MIRROR_FIXTURE_GITLAB`). Records carry snake_case fields; the
drivers normalise them. Raises `ReaderError` for anything unreadable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class ReaderError(Exception):
    """The fixture MR could not be read."""


def _state() -> dict:
    path = os.environ.get("MIRROR_FIXTURE_GITLAB", "")
    if not path:
        raise ReaderError("MIRROR_FIXTURE_GITLAB is not set")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReaderError(f"unreadable fixture MR state: {error}")


def read_open_mr(host: str, project: str, iid: int) -> dict:
    state = _state()
    if host != state.get("host") or project != state.get("project"):
        raise ReaderError(f"unknown fixture target: {host}/{project}")
    record = state.get("mrs", {}).get(str(iid))
    if not isinstance(record, dict):
        raise ReaderError(f"unknown fixture MR !{iid}")
    return record


def read_merged_mr(host: str, project: str, iid: int) -> dict:
    """The merged-MR record for the historical route (issue #143)."""

    state = _state()
    if host != state.get("host") or project != state.get("project"):
        raise ReaderError(f"unknown fixture target: {host}/{project}")
    record = state.get("mrs", {}).get(str(iid))
    if not isinstance(record, dict):
        raise ReaderError(f"unknown fixture MR !{iid}")
    return record
