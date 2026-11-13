#!/usr/bin/env python3
"""Lenovo side: remote steps of the staged task-bundle publication.

Companion to ``lenovo_publish.py`` (the single command of a signed LAN job).
This module holds the steps that talk to the Lenovo remote once the bundle is
imported: the ``gitlab-mr-flow`` freshness preflight, the branch push, the
Draft merge-request publish, the consented ``remove_draft`` step of a
``ready`` request, and the read-only reconciliation probe, plus the
per-action consent gate (``require``) they share with the driver.

Split out so both driver modules stay under the 5500-token per-file budget;
behavior is unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, Stop, git_ok, run,
)
from lenovo_publish_flow import (
    capture, expected_ready_title, find_open_mr, load_flow, publish_draft,
    require_ready_identity, undraft_mr,
)


def require(request: dict, action: str) -> None:
    if action not in request["authorized"]:
        raise Stop("consent-missing", code=2, detail=(
            f"the user has not authorized the Lenovo {action} action for this run; "
            "ask once for that action and submit a new request that lists it"))


def preflight(request: dict, worktree: Path, flow_dir: Path, result: dict) -> None:
    proc = run([sys.executable, str(flow_dir / "mr_preflight.py"), "--repo", str(worktree),
                "--base", request["targetBranch"], "--remote", "origin",
                "--mode", "refresh", "--format", "json"])
    try:
        receipt = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        receipt = {}
    result["preflight"] = {
        "exitCode": proc.returncode,
        "freshness": receipt.get("freshness"),
        "targetSha": receipt.get("target_sha"),
        "stopReasons": receipt.get("stop_reasons"),
    }
    if proc.returncode != 0:
        raise Stop("preflight-blocked", detail=(proc.stderr or proc.stdout).strip()[:DETAIL_MAX])


def push(request: dict, worktree: Path, result: dict) -> str:
    require(request, "push")
    branch = request["branch"]
    proc = run(["git", "-C", str(worktree), "push", "origin", branch])
    observed = remote_head(worktree, branch)
    result["push"] = {"remote": "origin", "branch": branch,
                      "localSha": request["expectedCommit"], "observedRemoteSha": observed,
                      "exitCode": proc.returncode}
    if proc.returncode != 0:
        raise Stop("push-unknown", verdict="unknown", code=3,
                   detail=(proc.stderr or proc.stdout).strip()[:DETAIL_MAX])
    if observed != request["expectedCommit"]:
        raise Stop("push-unknown", verdict="unknown", code=3,
                   detail=f"remote head {observed} is not the task commit after a zero exit")
    return observed


def remote_head(worktree: Path, branch: str) -> str | None:
    ok, output = git_ok(worktree, "ls-remote", "--heads", "origin", branch)
    if not ok:
        return None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith(f"/{branch}"):
            return parts[0].lower()
    return None


def publish(request: dict, worktree: Path, flow_dir: Path, result: dict) -> dict:
    """Publish the Draft MR, and undraft it when the request consented to Ready.

    A ``draft`` request ends after the Draft publish and its readback, exactly
    as before: the ``remove_draft`` step never runs for it. A ``ready``
    request needs ``undraft`` in ``authorized`` (checked again here at the
    point of use, after the request-level gate): it then runs the external
    ``remove_draft.py`` once against the same MR, re-captures the identity,
    and proves the head SHA, the Ready title, and the input description still
    match. No pipeline is waited for or polled on any path; the result
    carries ``pipeline: {waited: false, polled: false}`` as the visible fact.
    """

    require(request, "mr")
    api, identity, publisher = load_flow(flow_dir)
    existing = find_open_mr(api, request["host"], request["project"], request["branch"])
    written = publish_draft(publisher, api, request, existing)
    result["mr"] = {"url": written["mr_url"], "iid": written["iid"],
                    "draft": written.get("draft"), "created": existing is None}
    receipt = capture(identity, written["mr_url"], worktree)
    result["identity"] = receipt
    result["pipeline"] = {"waited": False, "polled": False}
    if request.get("state") != "ready":
        result["undraft"] = None
        return receipt
    require(request, "undraft")
    undraft_mr(flow_dir, written["mr_url"], request["host"], request["project"])
    receipt = capture(identity, written["mr_url"], worktree)
    if str(receipt.get("head_sha") or "").lower() != request["expectedCommit"]:
        raise Stop("readback-mismatch", verdict="unknown", code=3,
                   detail=(f"{receipt.get('head_sha')} != {request['expectedCommit']}"
                           )[:DETAIL_MAX])
    check = require_ready_identity(api, request, written["iid"])
    result["mr"] = {"url": written["mr_url"], "iid": written["iid"],
                    "draft": False, "created": existing is None}
    result["identity"] = receipt
    result["undraft"] = {"removedDraft": True, "titleChecked": True,
                         "title": expected_ready_title(request),
                         "descriptionCheck": check, "draft": False}
    return receipt


def observe(request: dict, worktree: Path, flow_dir: Path, result: dict) -> dict | None:
    """Read-only reconciliation: the remote branch and the merge request, if any."""

    api, identity, _ = load_flow(flow_dir)
    checkout = Path(request["lenovoCheckout"])
    repo = worktree if worktree.is_dir() else checkout
    observed = remote_head(repo, request["branch"]) if repo.is_dir() else None
    result["push"] = {"remote": "origin", "branch": request["branch"],
                      "localSha": request["expectedCommit"], "observedRemoteSha": observed,
                      "exitCode": None}
    existing = find_open_mr(api, request["host"], request["project"], request["branch"])
    if not existing:
        result["mr"] = {"url": None, "iid": None, "draft": None, "created": False}
        return None
    iid = existing.get("iid")
    if isinstance(iid, bool) or not isinstance(iid, int):
        raise Stop("mr-readback-unavailable", detail="MR list row carries no IID")
    url = existing.get("web_url") or api.merge_request_url(
        request["host"], request["project"], iid)
    result["mr"] = {"url": url, "iid": iid,
                    "draft": bool(existing.get("draft")), "created": False}
    receipt = capture(identity, url, worktree if worktree.is_dir() else None)
    result["identity"] = receipt
    return receipt
