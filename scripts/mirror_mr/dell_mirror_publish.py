"""Dell side: publish the observed revisions and reconcile the GitHub PR.

Companion to `dell_mirror_import.py` (the single entry point of the Dell
import job). This module holds the steps that mutate the mirror or GitHub:
the compare-and-swap ref reconciliation (fast-forward target only, never a
force-push), the push with immediate `ls-remote` readback, and the
find-before-create PR adoption plus equivalence readback.

Split out so both driver modules stay under the per-file token budget;
behavior is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dell_lan"))

from mirror_mr_common import DETAIL_MAX, Stop  # noqa: E402
from dell_lan_common import git_ok  # noqa: E402

ZERO_OID = "0" * 40


def worktree_dirty(mirror: Path, branch: str) -> bool | None:
    """True when a worktree checked out at `branch` has edits, False when
    clean or absent, None when unreadable (fail closed by the caller)."""

    ok, output = git_ok(mirror, "worktree", "list", "--porcelain")
    if not ok:
        return None
    for block in output.split("\n\n"):
        path = branch_name = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("branch "):
                branch_name = line[len("branch "):].strip()
                if branch_name.startswith("refs/heads/"):
                    branch_name = branch_name[len("refs/heads/"):]
        if branch_name == branch and path:
            status, text = git_ok(Path(path), "status", "--porcelain")
            if not status:
                return None
            return bool(text.strip())
    return False


def branch_checked_out(mirror: Path, branch: str) -> bool | None:
    """True when any worktree has `branch` checked out, False when absent.

    Returns None when the worktree list is unreadable (fail closed). A clean
    checkout still counts as present: moving its branch with `update-ref`
    would leave the working tree behind, so the caller refuses even a clean
    checkout rather than surprising the operator.
    """

    ok, output = git_ok(mirror, "worktree", "list", "--porcelain")
    if not ok:
        return None
    for block in output.split("\n\n"):
        branch_name = None
        for line in block.splitlines():
            if line.startswith("branch "):
                branch_name = line[len("branch "):].strip()
                if branch_name.startswith("refs/heads/"):
                    branch_name = branch_name[len("refs/heads/"):]
        if branch_name == branch:
            return True
    return False


def reconcile_refs(mirror: Path, exported: dict) -> dict:
    """Move the two mirror refs to the observed SHAs; never force-push.

    Target: create or fast-forward only. Source: create or confirm equal;
    any other existing source ref is a conflict that stops the run.
    """

    moved = {}
    for role, branch_key, sha_key in (
            ("target", "targetBranch", "targetSha"),
            ("source", "sourceBranch", "sourceSha")):
        branch = exported[branch_key]
        want = exported[sha_key].lower()
        ref = f"refs/heads/{branch}"
        ok, current = git_ok(mirror, "rev-parse", "--verify", ref)
        current = current.strip().lower() if ok else None
        if current == want:
            moved[role] = {"updated": False, "previous": current}
            continue
        checked_out = branch_checked_out(mirror, branch)
        if checked_out is None:
            raise Stop("worktree-unreadable", detail=f"cannot read worktree for {branch}")
        if checked_out:
            raise Stop("worktree-checked-out", detail=(
                f"{branch} is checked out; refusing to move it under a worktree"))
        dirty = worktree_dirty(mirror, branch)
        if dirty is None:
            raise Stop("worktree-unreadable", detail=f"cannot read worktree for {branch}")
        if dirty:
            raise Stop("worktree-dirty", detail=f"{branch} worktree has edits")
        if current is None:
            ok, failure = git_ok(mirror, "update-ref", ref, want, ZERO_OID)
            if not ok:
                raise Stop("ref-owned", detail=failure[:DETAIL_MAX])
            moved[role] = {"updated": True, "previous": None}
        elif role == "target":
            ok, _ = git_ok(mirror, "merge-base", "--is-ancestor", current, want)
            if not ok:
                raise Stop("target-not-fast-forward", detail=(
                    f"mirror {branch} is at {current[:12]}, observed {want[:12]} "
                    "is not its descendant"))
            ok, failure = git_ok(mirror, "update-ref", ref, want, current)
            if not ok:
                raise Stop("ref-owned", detail=failure[:DETAIL_MAX])
            moved[role] = {"updated": True, "previous": current}
        else:
            raise Stop("source-ref-conflict", detail=(
                f"mirror {branch} is at {current[:12]}, observed {want[:12]}; "
                "refusing to overwrite"))
    return moved


def read_remote_refs(mirror: Path, branches: set[str]) -> dict:
    ok, output = git_ok(mirror, "ls-remote", "--heads", "origin")
    if not ok:
        raise Stop("remote-unreadable", detail=output[:DETAIL_MAX])
    refs: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            name = parts[1][len("refs/heads/"):]
            if name in branches:
                refs[name] = parts[0].lower()
    return refs


def push_refs(mirror: Path, exported: dict) -> dict:
    branches = [exported["targetBranch"], exported["sourceBranch"]]
    ok, failure = git_ok(mirror, "push", "origin", *branches)
    observed = read_remote_refs(mirror, set(branches))
    if not ok:
        raise Stop("push-failed", detail=(
            f"{failure[:DETAIL_MAX]}; remote now {observed}"))
    for branch, want_key in ((exported["targetBranch"], "targetSha"),
                             (exported["sourceBranch"], "sourceSha")):
        if observed.get(branch) != exported[want_key].lower():
            raise Stop("push-unknown", verdict="unknown", code=3, detail=(
                f"remote {branch} is {observed.get(branch)} "
                f"!= {exported[want_key].lower()} after push"))
    return observed


def adopt_or_create(github, mirror: Path, mirror_repo: str, state: dict) -> dict:
    """Find the existing PR or open exactly one, then prove equivalence."""

    head, base = state["sourceBranch"], state["targetBranch"]
    try:
        existing = github.find_open_pr(mirror_repo, head, base)
    except github.GithubError as error:
        raise Stop("pr-unreadable", detail=str(error)[:DETAIL_MAX])
    if existing is not None:
        record = read_and_compare(github, mirror, existing["url"], state)
        record["reconciled"] = True
        return record
    if state.get("prUrl"):
        record = read_and_compare(github, mirror, state["prUrl"], state)
        record["reconciled"] = True
        return record
    body = (f"Mirror of {state.get('mrUrl') or ''}\n\n"
            f"Source: {head}@{state['sourceSha']}\n"
            f"Target: {base}@{state['targetSha']}\n\n"
            f"{state.get('description') or ''}").strip() + "\n"
    try:
        created = github.create_pr(mirror_repo, state.get("title") or
                                   f"Mirror {state.get('mrUrl') or ''}",
                                   body, base, head)
    except github.GithubError as error:
        raise Stop("pr-write-unknown", verdict="unknown", code=3,
                   detail=str(error)[:DETAIL_MAX])
    record = read_and_compare(github, mirror, created["url"], state)
    record["number"] = created.get("number", record.get("number"))
    return record


def read_and_compare(github, mirror: Path, url: str, state: dict) -> dict:
    try:
        record = github.read_pr(url)
    except github.GithubError as error:
        raise Stop("pr-unreadable", detail=str(error)[:DETAIL_MAX])
    if str(record.get("state") or "").lower() not in ("open", "opened"):
        raise Stop("pr-conflict", detail=f"{url} is {record.get('state')}")
    if record.get("headRefName") != state["sourceBranch"] \
            or record.get("baseRefName") != state["targetBranch"]:
        raise Stop("pr-conflict", detail=f"{url} names another direction")
    if str(record.get("headRefOid") or "").lower() != state["sourceSha"]:
        raise Stop("pr-conflict", detail=f"{url} head is not {state['sourceSha'][:12]}")
    if str(record.get("baseRefOid") or "").lower() != state["targetSha"]:
        raise Stop("pr-conflict", detail=f"{url} base is not {state['targetSha'][:12]}")
    refs = github.mirror_refs(mirror)
    base_head = refs.get(state["targetBranch"])
    if base_head != state["targetSha"]:
        raise Stop("pr-conflict", detail=(
            f"mirror {state['targetBranch']} is {base_head} "
            f"!= {state['targetSha'][:12]}"))
    return {"url": record.get("url") or url, "number": record.get("number"),
            "comparison": "exact"}
