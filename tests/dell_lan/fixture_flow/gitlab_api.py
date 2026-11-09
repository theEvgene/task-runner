"""Fixture double for the remote read seam (tests only, never production).

Implements the narrow surface the Lenovo publish driver consumes from the
external publication flow: project URL helpers plus a JSON state file read.
The state file and the bare origin repository it points at are owned by the
disposable fixture (``DELL_FIXTURE_STATE`` / ``DELL_FIXTURE_ORIGIN``); the
record shape mirrors what the driver expects from the real flow's MR list.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, unquote


class GitlabUnavailable(Exception):
    """The simulated remote could not be read."""


def _state() -> dict:
    path = os.environ.get("DELL_FIXTURE_STATE", "")
    if not path:
        raise GitlabUnavailable("DELL_FIXTURE_STATE is not set")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GitlabUnavailable(f"unreadable fixture state: {error}")


def project_api_base(host: str, project: str) -> str:
    """Stable project prefix the double's ``api_get`` parses back."""

    return f"projects/{quote(project, safe='')}"


def merge_request_url(host: str, project: str, iid: int) -> str:
    """Canonical MR URL for one integer IID."""

    return f"https://{host}/{project}/-/merge_requests/{iid}"


def api_get(host: str, path: str):
    """List open merge requests from the fixture state file."""

    state = _state()
    if host != state.get("host"):
        raise GitlabUnavailable(f"unknown host: {host}")
    head, _, query = path.partition("?")
    if not head.startswith("projects/") or "/merge_requests" not in head:
        raise GitlabUnavailable(f"unsupported fixture path: {path}")
    encoded = head[len("projects/"):].split("/merge_requests")[0]
    if unquote(encoded) != state.get("project"):
        raise GitlabUnavailable(f"unknown project: {encoded}")
    rows = []
    for record in state.get("mrs", {}).values():
        rows.append({
            "iid": int(record["iid"]),
            "source_branch": record["source_branch"],
            "target_branch": record["target_branch"],
            "web_url": record["web_url"],
            "draft": bool(record.get("draft", True)),
            "state": "opened",
        })
    if "state=opened" in query:
        return rows
    return rows
