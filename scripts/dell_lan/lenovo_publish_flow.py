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

from dell_lan_common import DETAIL_MAX, Stop, run

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
    """Create or update the Draft merge request through the existing writer.

    Preserves the existing Draft behavior exactly (``replace_description=False``,
    the append-on-update contract from the base): a ``draft`` request is unchanged
    by #37, and a ``ready`` request first publishes the same Draft and only then
    lifts it through the consented ``remove_draft`` step. The writer is never
    asked to silently overwrite an existing description; when an update leaves
    the served description different from the input, the Ready readback below
    fails closed (``readback-mismatch``) instead of forcing equality.
    """

    description = Path(request["descriptionFile"]).read_text(encoding="utf-8")
    try:
        result = publisher.publish(
            host=request["host"],
            project=request["project"],
            change_type=request["changeType"],
            key=request["jiraKey"],
            subject=request["subject"],
            description=description,
            desired_state="draft",
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


def undraft_mr(flow_dir: Path, mr_url: str, host: str, project: str) -> None:
    """Mark the published MR Ready through the external ``remove_draft.py``.

    The script is owner-provided in ``flow_dir`` and is invoked with its real
    CLI (``remove_draft.py --mr-url <url> --host <host> --project <project>``;
    the real script accepts ``--mr-url``/``--url`` plus optional ``--host`` /
    ``--project`` and has no ``--repo`` flag). A zero exit means the same MR
    was marked Ready, anything else (missing script, non-zero exit) is
    ``undraft-unknown`` (exit 3): the Draft write has landed, so a failed
    undraft is never reported as success and never retried blindly.
    """

    script = Path(flow_dir) / "remove_draft.py"
    if not script.is_file():
        raise Stop("undraft-unknown", verdict="unknown", code=3,
                   detail=f"remove_draft step unavailable: {script}"[:DETAIL_MAX])
    proc = run([sys.executable, str(script),
                "--mr-url", mr_url, "--host", host, "--project", project])
    if proc.returncode != 0:
        raise Stop("undraft-unknown", verdict="unknown", code=3,
                   detail=(proc.stderr or proc.stdout).strip()[:DETAIL_MAX])


def expected_ready_title(request: dict) -> str:
    """The Ready title the writer produced, without the ``Draft: `` prefix."""

    return f"{request['changeType']}({request['jiraKey']}): {request['subject']}"


def descriptions_match(served: str, expected: str) -> bool:
    """The one shared description check (#11 criterion, reused by #37).

    Byte-for-byte equality of the served MR description against the request
    input (both already UTF-8 strings, no normalization). #11 is still open on
    the base with no helper to import, so this is the single place that states
    the exactness criterion; the Ready path reuses it instead of duplicating
    it. A suffix/``endswith`` check is not used because it would accept an
    arbitrary preserved prefix.
    """

    return served == expected


def require_ready_identity(api, request: dict, iid: int) -> str:
    """Prove the undrafted MR still carries the exact input title/description.

    Re-reads the single MR through the flow client's own ``api_get`` and
    compares it against the request input: the Ready title built the same way
    the writer builds the Draft title, and the description file bytes
    (UTF-8, byte-for-byte). Exact equality is required: a create carries the
    input exactly, while an update that preserved earlier text (the base
    append contract, ``replace_description=False``) fails here with
    ``readback-mismatch`` instead of being silently overwritten. Any mismatch
    is ``readback-mismatch`` (exit 3): the undraft write may have landed, so
    the run reconciles instead of retrying blindly, and a later publish
    updates the same MR. Returns ``exact``.
    """

    try:
        record = api.api_get(
            request["host"],
            f"{api.project_api_base(request['host'], request['project'])}/merge_requests/{iid}")
    except api.GitlabUnavailable as error:
        raise Stop("mr-readback-unavailable", verdict="unknown", code=3,
                   detail=str(error)[:DETAIL_MAX])
    if not isinstance(record, dict):
        raise Stop("readback-mismatch", verdict="unknown", code=3,
                   detail="single-MR read did not return a JSON object")
    if record.get("draft") is not False:
        raise Stop("readback-mismatch", verdict="unknown", code=3,
                   detail="the MR is not marked Ready after the undraft step")
    if str(record.get("title") or "") != expected_ready_title(request):
        raise Stop("readback-mismatch", verdict="unknown", code=3,
                   detail=(f"undrafted title {record.get('title')!r} "
                           f"!= {expected_ready_title(request)!r}")[:DETAIL_MAX])
    try:
        description = Path(request["descriptionFile"]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Stop("readback-mismatch", verdict="unknown", code=3,
                   detail=f"cannot re-read the input description: {error}"[:DETAIL_MAX])
    served = record.get("description")
    if not isinstance(served, str):
        raise Stop("readback-mismatch", verdict="unknown", code=3,
                   detail="the MR carries no description to compare")
    if not descriptions_match(served, description):
        raise Stop("readback-mismatch", verdict="unknown", code=3,
                   detail=(f"undrafted description failed the exact check "
                           f"({len(served)} vs {len(description)} chars)")[:DETAIL_MAX])
    return "exact"
