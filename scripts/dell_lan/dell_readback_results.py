"""Structured Lenovo result envelopes consumed by Dell readback."""

from __future__ import annotations

import json
import os
import re

from dell_standalone_common import (
    PUBLISH_EXISTING_RESULT_SCHEMA, TRANSFER_RESULT_SCHEMA,
)
from description_proof import description_proof
from publish_receipt_gate import is_published as _gate_published

SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
PUBLISH_RESULT_SCHEMA = "dell-lan-publish-result/1"
GITLAB_READBACK = "gitlab-mr-flow/scripts/mr_identity.py capture"
PUBLISH_DRIVER = "scripts/dell_lan/lenovo_publish.py"
TRANSFER_DRIVER = "scripts/dell_lan/lenovo_transfer.py"
PUBLISH_EXISTING_DRIVER = "scripts/dell_lan/lenovo_publish_existing.py"
DRIVER_SCHEMAS = {
    PUBLISH_RESULT_SCHEMA: PUBLISH_DRIVER,
    TRANSFER_RESULT_SCHEMA: TRANSFER_DRIVER,
    PUBLISH_EXISTING_RESULT_SCHEMA: PUBLISH_EXISTING_DRIVER,
}
PUBLICATION_DRIVER_SCHEMAS = {PUBLISH_RESULT_SCHEMA, PUBLISH_EXISTING_RESULT_SCHEMA}
DETAIL_MAX = 400


def normalise_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.strip()))


class Stop(Exception):
    """A fail-closed stop with a reason, a verdict and an exit code."""

    def __init__(self, reason: str, verdict: str = "mismatch", code: int = 1,
                 detail: str = "", queue: dict | None = None,
                 publication: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.verdict = verdict
        self.code = code
        self.detail = detail
        self.queue = queue
        self.publication = publication


def unwrap_driver(document: dict, args, queue: dict) -> tuple[dict | None, dict]:
    """Validate a driver envelope before its nested identity receipt."""

    outcome = document.get("outcome")
    if outcome == "unknown":
        raise Stop("outcome-unknown", verdict="unknown", code=3, queue=queue,
                   detail=json.dumps({
                       "stage": document.get("stage"),
                       "reason": (document.get("error") or {}).get("reason"),
                   })[:DETAIL_MAX])
    if outcome not in ("ok", "observed"):
        raise Stop("publication-job-failed", verdict="rejected", queue=queue,
                   detail=f"driver outcome {outcome}")
    if outcome == "ok" and not _gate_published(document, args.task_commit):
        if document.get("stage") != "readback":
            raise Stop("publication-readback-malformed", queue=queue,
                       detail=f"ok at {document.get('stage')}")
    identity = document.get("identity")
    if identity is None:
        raise Stop("publication-absent", queue=queue, detail=(
            "the Lenovo observation found no merge request for this branch; nothing is published"))
    push = document.get("push") if isinstance(document.get("push"), dict) else {}
    observed = str(push.get("observedRemoteSha") or "").lower()
    if observed and observed != args.task_commit:
        raise Stop("publication-push-mismatch", queue=queue,
                   detail=f"{observed} != {args.task_commit}")
    mr = document.get("mr") if isinstance(document.get("mr"), dict) else {}
    if mr.get("created") is not False and not description_proof(mr)["descriptionVerified"]:
        raise Stop("publication-readback-malformed", queue=queue, detail=(
            "driver carries no exact signed-description proof"))
    if isinstance(identity, dict):
        if mr.get("url") and mr["url"] != identity.get("mr_url"):
            raise Stop("publication-readback-malformed", queue=queue,
                       detail="driver MR URL differs from the identity receipt")
        if mr.get("iid") is not None and mr["iid"] != identity.get("iid"):
            raise Stop("publication-readback-malformed", queue=queue,
                       detail="driver MR IID differs from the identity receipt")
    return identity, document


def blank_publication(provenance: str) -> dict:
    return {
        "readbackObserved": False, "bound": False,
        "provenance": provenance, "source": GITLAB_READBACK,
        "driver": None, "driverStage": None, "driverOutcome": None,
        "authorized": None, "pushObservedSha": None, "mrCreated": None,
        "descriptionVerified": False,
        "descriptionExpectedSha256": None,
        "descriptionObservedSha256": None,
        "gitlabHost": None, "projectPath": None, "iid": None,
        "mrUrl": None, "state": None, "sourceBranch": None,
        "targetBranch": None, "headSha": None, "targetSha": None,
        "capturedAt": None, "localChecked": False,
        "declaredSimulated": None, "simulated": None,
        "capturedHours": None,
    }


def blank_transfer() -> dict:
    return {
        "readbackObserved": False, "bound": False, "driver": None,
        "driverStage": None, "driverOutcome": None, "authorized": None,
        "branch": None, "commit": None, "worktree": None,
        "clean": None, "matches": None,
    }


def unwrap_transfer(document: dict, args, queue: dict) -> dict:
    """Bind a sealed standalone transfer result to its requested identity."""

    outcome = document.get("outcome")
    if outcome == "unknown":
        raise Stop("outcome-unknown", verdict="unknown", code=3, queue=queue,
                   detail=json.dumps({
                       "stage": document.get("stage"),
                       "reason": (document.get("error") or {}).get("reason"),
                   })[:DETAIL_MAX])
    if outcome not in ("ok", "observed"):
        raise Stop("publication-job-failed", verdict="rejected", queue=queue,
                   detail=f"transfer driver outcome {outcome}")
    imported = document.get("import")
    if not isinstance(imported, dict):
        raise Stop("publication-readback-malformed", queue=queue,
                   detail="transfer result has no import observation")
    branch = str(imported.get("branch") or "")
    commit = str(imported.get("commit") or "").lower()
    worktree = str(imported.get("worktree") or "")
    clean = imported.get("clean") is True
    matches = imported.get("matches") is True
    if document.get("push") is not None or document.get("mr") is not None:
        raise Stop("publication-readback-malformed", queue=queue,
                   detail="transfer result unexpectedly contains publication state")
    if not branch or not worktree or not SHA_RE.match(commit):
        raise Stop("publication-readback-malformed", queue=queue,
                   detail="transfer branch/commit/worktree fields are missing")
    if commit != args.task_commit:
        raise Stop("publication-head-mismatch", queue=queue,
                   detail=f"{commit} != {args.task_commit}")
    if args.source_branch and branch != args.source_branch:
        raise Stop("publication-branch-mismatch", queue=queue, detail=branch[:DETAIL_MAX])
    expected_worktree = getattr(args, "lenovo_worktree", None)
    if not expected_worktree:
        raise Stop("transfer-worktree-unpinned", queue=queue,
                   detail="standalone transfer readback requires --lenovo-worktree")
    if normalise_path(worktree) != normalise_path(str(expected_worktree)):
        raise Stop("transfer-worktree-mismatch", queue=queue, detail=json.dumps({
            "expected": str(expected_worktree), "observed": worktree,
        })[:DETAIL_MAX])
    if not clean or not matches:
        raise Stop("outcome-unknown", verdict="unknown", code=3, queue=queue,
                   detail="transfer did not prove a clean matching branch worktree")
    return {
        "readbackObserved": True, "bound": True, "driver": TRANSFER_DRIVER,
        "driverStage": document.get("stage"), "driverOutcome": outcome,
        "authorized": document.get("authorized"), "branch": branch,
        "commit": commit, "worktree": worktree, "clean": clean,
        "matches": matches,
    }
