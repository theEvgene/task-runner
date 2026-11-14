"""Fixture double for the remote read seam (tests only, never production).

Implements the narrow surface the Lenovo publish driver consumes from the
external publication flow: project URL helpers, a JSON state file read, and
the flow's own paged list reader. The state file and the bare origin
repository it points at are owned by the disposable fixture
(``DELL_FIXTURE_STATE`` / ``DELL_FIXTURE_ORIGIN``); the record shape mirrors
what the driver expects from the real flow's MR list.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, unquote


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
    """List open merge requests from the fixture state file, honoring paging.

    Rows carry the stored description when the record has one, which is the
    seam the driver's exact round-trip check reads; a row without it proves
    nothing about the write.
    """

    state = _state()
    if host != state.get("host"):
        raise GitlabUnavailable(f"unknown host: {host}")
    head, _, query = path.partition("?")
    if not head.startswith("projects/") or "/merge_requests" not in head:
        raise GitlabUnavailable(f"unsupported fixture path: {path}")
    encoded = head[len("projects/"):].split("/merge_requests")[0]
    if unquote(encoded) != state.get("project"):
        raise GitlabUnavailable(f"unknown project: {encoded}")
    suffix = head.split("/merge_requests", 1)[1]
    if suffix.startswith("/") and suffix[1:].isdigit():
        if os.environ.get("DELL_FIXTURE_FAIL_DESCRIPTION_READBACK") == "1":
            raise GitlabUnavailable("fixture description readback unavailable")
        record = state.get("mrs", {}).get(suffix[1:])
        if not isinstance(record, dict):
            raise GitlabUnavailable(f"unknown fixture MR: {path}")
        return {
            "iid": int(record["iid"]),
            "source_branch": record["source_branch"],
            "target_branch": record["target_branch"],
            "web_url": record["web_url"],
            "draft": bool(record.get("draft", True)),
            "title": record.get("title", ""),
            "description": record.get("description", ""),
            "state": "opened",
        }
    rows = []
    for record in state.get("mrs", {}).values():
        row = {
            "iid": int(record["iid"]),
            "source_branch": record["source_branch"],
            "target_branch": record["target_branch"],
            "web_url": record["web_url"],
            "draft": bool(record.get("draft", True)),
            "state": "opened",
        }
        if "description" in record:
            row["description"] = record["description"]
        rows.append(row)
    params = dict(parse_qsl(query))
    if "page" in params or "per_page" in params:
        try:
            per_page = int(params.get("per_page", 100))
            page = int(params.get("page", 1))
        except ValueError:
            raise GitlabUnavailable(f"bad fixture page: {query}")
        if per_page < 1 or page < 1:
            raise GitlabUnavailable(f"bad fixture page: {query}")
        rows = rows[(page - 1) * per_page:page * per_page]
    return rows


def paginate(host: str, path: str, *, per_page: int = 100,
             max_pages: int = 50,
             get: Callable[[str, str], Any] | None = None) -> dict[str, Any]:
    """Read every page of one fixture list endpoint (same contract as real)."""

    fetch = get or api_get
    separator = "&" if "?" in path else "?"
    items: list[dict[str, Any]] = []
    pages = 0
    while pages < max_pages:
        page_number = pages + 1
        try:
            payload = fetch(host, f"{path}{separator}per_page={per_page}&page={page_number}")
        except GitlabUnavailable as error:
            return {"items": items, "pages": pages, "complete": False,
                    "truncated": False, "error": str(error)}
        if not isinstance(payload, list):
            return {"items": items, "pages": pages, "complete": False,
                    "truncated": False,
                    "error": f"page {page_number} did not return a list"}
        pages = page_number
        items.extend(entry for entry in payload if isinstance(entry, dict))
        if len(payload) < per_page:
            return {"items": items, "pages": pages, "complete": True,
                    "truncated": False, "error": None}
    return {"items": items, "pages": pages, "complete": False,
            "truncated": True, "error": None}
