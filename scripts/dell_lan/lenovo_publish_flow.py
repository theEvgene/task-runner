"""Compose the external publication flow for one Lenovo task.

Nothing here re-implements remote access: the MR write is
``publish_mr.publish`` and the readback is ``mr_identity.capture_identity``
from the external flow directory (``--flow-dir``). This module only adds the
two things the queue job needs around them:

- a *find* before a *create*, so a repeated run after an unknown outcome
  reconciles the existing merge request instead of opening a second one;
- a bounded, factual failure surface: an exception from a write is reported as
  ``unknown`` (the write may have landed), never as "nothing happened".

It performs no push and no Git mutation.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from dell_lan_common import DETAIL_MAX, Stop

PAGE_SIZE = 100
MAX_PAGES = 50


def load_flow(flow_dir: Path):
    """Import the existing flow modules from their checked-in directory."""

    directory = str(Path(flow_dir))
    if directory not in sys.path:
        sys.path.insert(0, directory)
    try:
        api = importlib.import_module("gitlab_api")
        identity = importlib.import_module("mr_identity")
        publisher = importlib.import_module("publish_mr")
    except Exception as error:  # noqa: BLE001 - any import failure is a stop
        raise Stop("flow-unavailable", detail=f"{flow_dir}: {error}"[:DETAIL_MAX])
    return api, identity, publisher


def _paged_fallback(api, host: str, path: str) -> list:
    """Read every page through ``api_get`` when the client has no reader.

    The legacy client only exposes ``api_get``; a single call would silently
    return page one. Pages are requested until a short page proves the end of
    the list. A failed page, a non-array page, or more than ``MAX_PAGES`` full
    pages proves nothing, so the call fails closed with
    ``mr-list-incomplete`` instead of treating the read as empty.
    """

    separator = "&" if "?" in path else "?"
    items: list = []
    for page_number in range(1, MAX_PAGES + 1):
        try:
            payload = api.api_get(
                host, f"{path}{separator}per_page={PAGE_SIZE}&page={page_number}")
        except api.GitlabUnavailable as error:
            raise Stop("mr-list-incomplete", detail=(
                f"page {page_number} unreadable: {error}")[:DETAIL_MAX])
        if not isinstance(payload, list):
            raise Stop("mr-list-incomplete", detail=(
                f"page {page_number} did not return a JSON array")[:DETAIL_MAX])
        items.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < PAGE_SIZE:
            return items
    raise Stop("mr-list-incomplete", detail=(
        f"open-MR list truncated after {MAX_PAGES} pages")[:DETAIL_MAX])


def find_open_mr(api, host: str, project: str, branch: str) -> dict | None:
    """The open merge request for this source branch, or None.

    The list endpoint is read through the flow client's own paged reader so
    a match past the first page is still found: creating while a match exists
    would open a second MR for one branch. A client without that reader is
    paged through ``api_get`` here, and only a short page proves completeness.
    More than one match is an ambiguity this flow refuses instead of guessing,
    and an incomplete list is refused instead of treated as empty.
    """

    base = api.project_api_base(host, project)
    path = f"{base}/merge_requests?state=opened"
    paginate = getattr(api, "paginate", None)
    if paginate is None:
        found = _paged_fallback(api, host, path)
    else:
        try:
            page = paginate(host, path)
        except api.GitlabUnavailable as error:
            raise Stop("mr-list-incomplete", detail=(
                f"open-MR list unreadable: {error}")[:DETAIL_MAX])
        if not isinstance(page, dict):
            raise Stop("mr-list-incomplete",
                       detail="paged reader returned no completeness record")
        if page.get("complete") is not True:
            raise Stop("mr-list-incomplete", detail=(
                page.get("error")
                or f"open-MR list truncated after {page.get('pages')} pages")[:DETAIL_MAX])
        found = page.get("items")
    if not isinstance(found, list):
        raise Stop("gitlab-unavailable", detail="MR list is not a JSON array")
    matching = [row for row in found
                if isinstance(row, dict) and row.get("source_branch") == branch]
    if len(matching) > 1:
        raise Stop("mr-ambiguous", detail=f"{len(matching)} open merge requests for {branch}")
    return matching[0] if matching else None


def publish_draft(publisher, api, request: dict, existing: dict | None) -> dict:
    """Create or update the Draft merge request through the existing writer."""

    description = Path(request["descriptionFile"]).read_text(encoding="utf-8")
    try:
        result = publisher.publish(
            host=request["host"],
            project=request["project"],
            change_type=request["changeType"],
            key=request["jiraKey"],
            subject=request["subject"],
            description=description,
            desired_state=request["state"],
            no_test_results=False,
            source_branch=request["branch"],
            target_branch=request["targetBranch"],
            mr_url=(existing or {}).get("web_url"),
            replace_description=False,
        )
    except (publisher.PublicationError, api.GitlabUnavailable, OSError, ValueError) as error:
        raise Stop("mr-write-unknown", verdict="unknown", code=3, detail=str(error)[:DETAIL_MAX])
    return result


def capture(identity, mr_url: str, worktree: Path) -> dict:
    """Read the pinned identity receipt for the written merge request."""

    try:
        return identity.capture_identity(mr_url, [], worktree, None)
    except (identity.IdentityError, identity.ContextMismatch) as error:
        raise Stop("mr-readback-unavailable", verdict="unknown", code=3,
                   detail=str(error)[:DETAIL_MAX])
