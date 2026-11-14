#!/usr/bin/env python3
"""Lenovo side: standalone publication of an already existing branch.

No bundle is imported here: the branch must already exist in the Lenovo
checkout at exactly ``expectedCommit``. The driver reuses the remote steps
(``lenovo_remote`` preflight/push, ``lenovo_publish_flow`` find/publish
helpers) and the existing unknown-recovery contract:

- push the branch, then find the open MR by project and source branch;
- an existing MR is never duplicated and its Draft/Ready status is never
  changed: the driver only captures and verifies it;
- without an MR, title (subject) and description are mandatory. When they are
  missing the run is refused with ``missing-data`` and names the absent
  fields; Dell resubmits the same branch with the data (a new job id) and the
  driver then creates one Draft MR and verifies project, branches, status
  and URL;
- an ambiguous MR list or an unknown write is never reported as success and
  never retried blindly: the run reconciles through the read-only probe.

SHA protection (expected base/commit, head readback) and per-action consent
(``push``, ``mr``, ``undraft`` for Ready, ``reconcile`` for the probe) are
the same gates the combined driver enforces.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, Stop, git_ok, inside, load_json, safe_name, sha256_bytes,
)
from dell_standalone_common import (
    PUBLISH_EXISTING_REQUEST_SCHEMA, PUBLISH_EXISTING_RESULT_SCHEMA,
)
from lenovo_remote import observe, preflight, push, require
from lenovo_publish_flow import (
    capture, expected_ready_title, find_open_mr, load_flow, publish_draft,
    require_ready_identity, undraft_mr,
)

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="lenovo_publish_existing.py", description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--reconcile", action="store_true",
                        help="read-only probe: observe the remote branch and MR, never write")
    parser.add_argument("--flow-dir", type=Path, help="gitlab-mr-flow scripts dir")
    return parser.parse_args(argv)


def validated(request_path: Path, expected_digest: str, reconcile: bool) -> dict:
    raw = request_path.read_bytes()
    if sha256_bytes(raw) != (expected_digest or "").lower():
        raise Stop("request-digest-mismatch", detail="request digest mismatch")
    request = load_json(request_path, "request-malformed")
    if request.get("schema") != PUBLISH_EXISTING_REQUEST_SCHEMA:
        raise Stop("request-malformed", detail=f"expected {PUBLISH_EXISTING_REQUEST_SCHEMA}")
    shared = Path(str(request.get("shared") or ""))
    for name in ("branch", "targetBranch"):
        value = str(request.get(name) or "")
        if not value or not BRANCH_RE.match(value) or ".." in value or value.startswith("-"):
            raise Stop("request-malformed", detail=f"{name} is not a safe branch name")
    for name in ("expectedBaseSha", "expectedCommit"):
        value = str(request.get(name) or "").lower()
        if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise Stop("request-malformed", detail=f"{name} must be 40 hex")
        request[name] = value
    if not request.get("host") or any(c in str(request["host"]) for c in "/\\@"):
        raise Stop("request-malformed", detail="host must be the bare GitLab hostname")
    if not request.get("project") or "/" not in str(request.get("project")):
        raise Stop("request-malformed", detail="project must be group/project")
    authorized = request.get("authorized")
    if not isinstance(authorized, list) or not authorized:
        raise Stop("request-malformed", detail="authorized must be a non-empty list")
    unknown = sorted(set(authorized) - {"import", "push", "mr", "undraft", "reconcile"})
    if unknown:
        raise Stop("request-malformed", detail=f"unknown authorization: {', '.join(unknown)}")
    if request.get("state") not in ("draft", "ready"):
        raise Stop("request-malformed", detail="state must be draft or ready")
    if request.get("state") == "ready" and "undraft" not in authorized:
        raise Stop("consent-missing", code=2, detail="ready needs undraft in authorized")
    desc = request.get("descriptionFile")
    if desc not in (None, ""):
        if not inside(Path(str(desc)), shared):
            raise Stop("request-malformed", detail="descriptionFile must live on the shared path")
        if not safe_name(Path(str(desc)).name):
            raise Stop("request-malformed", detail="descriptionFile must be one contained file")
    request["reconcileOnly"] = bool(reconcile or request.get("reconcileOnly"))
    return request


def check_existing_branch(request: dict) -> Path:
    checkout = Path(request["lenovoCheckout"])
    worktree = Path(request["lenovoWorktree"])
    expected = request["expectedCommit"]
    ok, _ = git_ok(checkout, "rev-parse", "--verify", f"{request['expectedBaseSha']}^{{commit}}")
    if not ok:
        raise Stop("base-mismatch", detail="Lenovo checkout lacks the expected base commit")
    ok, current = git_ok(checkout, "rev-parse", "--verify", f"refs/heads/{request['branch']}")
    if not ok:
        raise Stop("branch-missing", detail=f"refs/heads/{request['branch']} does not exist")
    if current.lower() != expected:
        raise Stop("artifact-mismatch", detail=f"branch is at {current.lower()} != {expected}")
    if not worktree.is_dir():
        raise Stop("branch-owned", detail=f"{worktree} is not a readable worktree")
    ok, head = git_ok(worktree, "rev-parse", "--verify", "HEAD")
    if not ok or head.lower() != expected:
        raise Stop("artifact-mismatch", detail="task worktree HEAD is not the task commit")
    return worktree


def missing_fields(request: dict) -> list:
    missing = []
    if request.get("changeType") not in ("feat", "fix"):
        missing.append("changeType")
    if not request.get("jiraKey") or not KEY_RE.match(str(request.get("jiraKey") or "")):
        missing.append("jiraKey")
    if not str(request.get("subject") or "").strip() or "\n" in str(request.get("subject")):
        missing.append("subject")
    desc = request.get("descriptionFile")
    if not desc:
        missing.append("descriptionFile")
    else:
        try:
            text = Path(str(desc)).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            missing.append("descriptionFile")
        else:
            if not text.strip():
                missing.append("descriptionFile")
    return missing


def main(argv=None) -> int:
    from dell_lan_common import default_flow_dir

    args = parse(argv)
    result = {"schema": PUBLISH_EXISTING_RESULT_SCHEMA, "outcome": "refused",
              "stage": "request", "token": None, "authorized": [], "bundle": None,
              "import": None, "preflight": None, "push": None, "mr": None,
              "identity": None, "undraft": None,
              "pipeline": {"waited": False, "polled": False},
              "missing": None, "error": None, "finishedAt": None}
    code = 0
    try:
        request = validated(Path(args.request), args.request_sha256, args.reconcile)
        result["token"] = request["token"]
        result["authorized"] = request["authorized"]
        flow_dir = args.flow_dir or default_flow_dir()
        if request["reconcileOnly"]:
            require(request, "reconcile")
            result["stage"] = "reconcile"
            worktree = Path(request["lenovoWorktree"])
            observe(request, worktree, flow_dir, result)
            result["outcome"] = "observed"
        else:
            result["stage"] = "branch"
            worktree = check_existing_branch(request)
            result["import"] = {"branch": request["branch"], "commit": request["expectedCommit"],
                               "worktree": str(worktree), "reused": True, "existing": True}
            result["stage"] = "preflight"
            preflight(request, worktree, flow_dir, result)
            result["stage"] = "push"
            push(request, worktree, result)
            result["stage"] = "mr"
            require(request, "mr")
            api, identity, publisher = load_flow(flow_dir)
            existing = find_open_mr(api, request["host"], request["project"], request["branch"])
            if existing is not None:
                url = existing.get("web_url") or api.merge_request_url(
                    request["host"], request["project"], existing.get("iid"))
                receipt = capture(identity, url, worktree)
                result["mr"] = {"url": url, "iid": existing.get("iid"),
                                "draft": bool(existing.get("draft")), "created": False}
                result["identity"] = receipt
                result["undraft"] = None
            else:
                absent = missing_fields(request)
                if absent:
                    result["missing"] = absent
                    result["stage"] = "mr"
                    raise Stop("missing-data", detail=f"absent MR data: {', '.join(absent)}; "
                               "resubmit the same branch with subject/description")
                written = publish_draft(publisher, api, request, None)
                result["mr"] = {"url": written["mr_url"], "iid": written["iid"],
                                "draft": written.get("draft"), "created": True}
                receipt = capture(identity, written["mr_url"], worktree)
                result["identity"] = receipt
                if request.get("state") == "ready":
                    require(request, "undraft")
                    undraft_mr(flow_dir, written["mr_url"], request["host"], request["project"])
                    receipt = capture(identity, written["mr_url"], worktree)
                    result["identity"] = receipt
                    check = require_ready_identity(api, request, written["iid"])
                    result["mr"] = {"url": written["mr_url"], "iid": written["iid"],
                                    "draft": False, "created": True}
                    result["undraft"] = {
                        "removedDraft": True,
                        "titleChecked": True,
                        "title": expected_ready_title(request),
                        "descriptionCheck": check,
                        "draft": False,
                    }
            receipt = result["identity"] or {}
            if str(receipt.get("head_sha") or "").lower() != request["expectedCommit"]:
                raise Stop("publication-head-mismatch", verdict="unknown", code=3,
                           detail="MR head is not the task commit")
            if receipt.get("project_path") != request["project"]:
                raise Stop("publication-project-mismatch", verdict="unknown", code=3,
                           detail=str(receipt.get("project_path"))[:DETAIL_MAX])
            if receipt.get("source_branch") != request["branch"]:
                raise Stop("publication-branch-mismatch", verdict="unknown", code=3,
                           detail=str(receipt.get("source_branch"))[:DETAIL_MAX])
            if receipt.get("target_branch") != request["targetBranch"]:
                raise Stop("publication-target-mismatch", verdict="unknown", code=3,
                           detail=str(receipt.get("target_branch"))[:DETAIL_MAX])
            result["outcome"] = "ok"
    except Stop as stop:
        result["outcome"] = stop.verdict if stop.verdict in ("unknown", "refused") else "refused"
        result["error"] = {"reason": stop.reason, "detail": stop.detail}
        code = stop.code
    except OSError as error:
        result["outcome"] = "unknown"
        result["error"] = {"reason": "io-error", "detail": str(error)[:DETAIL_MAX]}
        code = 3
    except Exception as error:  # noqa: BLE001 - an unattended driver always answers
        result["outcome"] = "unknown"
        result["error"] = {"reason": "driver-error",
                           "detail": f"{type(error).__name__}: {error}"[:DETAIL_MAX]}
        code = 3
    from dell_lan_common import now_iso as _now

    result["finishedAt"] = _now()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(f"lenovo publish-existing {result['outcome']} : {result['stage']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
