"""Dell side: publish historical revisions and reconcile the review PR.

Companion to `dell_mr_history_import.py` (the single entry point of the
merged-MR import job). This module holds the steps that prove the local
change, move the review-only refs, and find-or-create the GitHub PR:
diff-digest comparison against the authoritative export, proven
representation selection (original head, else an identical squash),
compare-and-swap history refs (never an overwrite), push with
`ls-remote` readback, and find-before-create PR adoption plus
equivalence readback.

Split out so both driver modules stay under the per-file token budget;
behavior is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dell_lan"))

from mirror_mr_common import DETAIL_MAX, Stop, check_branch  # noqa: E402
from dell_mirror_publish import (branch_checked_out, read_remote_refs,
                                 worktree_dirty)  # noqa: E402
from dell_lan_common import git_ok, sha256_bytes  # noqa: E402

ZERO_OID = "0" * 40


def fetch_into_mirror(mirror: Path, bundle: Path, exported: dict) -> None:
    ok, _ = git_ok(mirror, "bundle", "verify", str(bundle))
    if not ok:
        raise Stop("bundle-unreadable", detail="bundle does not verify in the mirror")
    ok, listed = git_ok(mirror, "bundle", "list-heads", str(bundle))
    heads = [line.split()[0].lower() for line in listed.splitlines()
             if line.strip()] if ok else []
    wants = [exported["baseSha"], exported["headSha"]]
    if exported.get("squashSha"):
        wants.append(exported["squashSha"])
    for want in wants:
        if want not in heads:
            raise Stop("history-unavailable",
                       detail=f"{want[:12]} is not in the bundle")
    for index, want in enumerate(wants):
        tmp = f"refs/tmp/mirror-mr/history-{index}"
        ok, failure = git_ok(mirror, "fetch", "-q", str(bundle), f"{want}:{tmp}")
        if not ok:
            raise Stop("bundle-unreadable",
                       detail=failure[:DETAIL_MAX] or f"cannot fetch {want[:12]}")
        ok, resolved = git_ok(mirror, "rev-parse", "--verify", f"{want}^{{commit}}")
        if not ok or resolved.strip().lower() != want:
            raise Stop("history-unavailable",
                       detail=f"{want[:12]} is not a commit in the mirror")
        git_ok(mirror, "update-ref", "-d", tmp)


def diff_digest(mirror: Path, base: str, head: str) -> tuple[str, list[str]]:
    ok, patch = git_ok(mirror, "diff", f"{base}..{head}", "--")
    if not ok:
        raise Stop("evidence-gap",
                   detail=f"cannot compare {base[:12]}..{head[:12]} in the mirror")
    ok, names = git_ok(mirror, "diff", "--name-only", f"{base}..{head}", "--")
    files = sorted(line.strip() for line in (names.splitlines() if ok else [])
                   if line.strip())
    return sha256_bytes(patch.encode("utf-8")), files


def select_representation(mirror: Path, exported: dict) -> tuple[str, str]:
    want = exported["authoritativeDigest"]
    want_files = sorted(exported["authoritativeFiles"])
    head_digest, head_files = diff_digest(mirror, exported["baseSha"],
                                          exported["headSha"])
    if head_digest == want and head_files == want_files:
        return exported["headSha"], "original-head"
    squash = exported.get("squashSha")
    if squash:
        squash_digest, squash_files = diff_digest(mirror, exported["baseSha"],
                                                  squash)
        if squash_digest == want and squash_files == want_files:
            return squash, "squash"
    raise Stop("evidence-gap", detail=(
        "no local representation matches the authoritative comparison; "
        "the squash and source histories differ and neither was proven"))


def history_refs(iid: int, base_sha: str, chosen_sha: str) -> tuple[str, str]:
    base_ref = f"mirror-mr/{iid}/base-{base_sha[:7]}"
    head_ref = f"mirror-mr/{iid}/head-{chosen_sha[:7]}"
    check_branch(base_ref, "history base ref")
    check_branch(head_ref, "history head ref")
    return base_ref, head_ref


def reconcile_history_refs(mirror: Path, base_ref: str, head_ref: str,
                           base_sha: str, head_sha: str) -> None:
    for ref, want in ((base_ref, base_sha), (head_ref, head_sha)):
        full = f"refs/heads/{ref}"
        ok, current = git_ok(mirror, "rev-parse", "--verify", full)
        current = current.strip().lower() if ok else None
        if current == want:
            continue
        checked = branch_checked_out(mirror, ref)
        if checked is None:
            raise Stop("worktree-unreadable", detail=f"cannot read worktree for {ref}")
        if checked:
            raise Stop("worktree-checked-out", detail=(
                f"{ref} is checked out; refusing to move it under a worktree"))
        dirty = worktree_dirty(mirror, ref)
        if dirty is None:
            raise Stop("worktree-unreadable", detail=f"cannot read worktree for {ref}")
        if dirty:
            raise Stop("worktree-dirty", detail=f"{ref} worktree has edits")
        if current is None:
            ok, failure = git_ok(mirror, "update-ref", full, want, ZERO_OID)
            if not ok:
                raise Stop("ref-owned", detail=failure[:DETAIL_MAX])
        else:
            raise Stop("history-ref-conflict", detail=(
                f"mirror {ref} is at {current[:12]}, historical {want[:12]}; "
                "refusing to overwrite"))


def push_history_refs(mirror: Path, base_ref: str, head_ref: str,
                      base_sha: str, head_sha: str) -> None:
    ok, failure = git_ok(mirror, "push", "origin", base_ref, head_ref)
    observed = read_remote_refs(mirror, {base_ref, head_ref})
    if not ok:
        raise Stop("push-failed", detail=(
            f"{failure[:DETAIL_MAX]}; remote now {observed}"))
    if observed.get(base_ref) != base_sha \
            or observed.get(head_ref) != head_sha:
        raise Stop("push-unknown", verdict="unknown", code=3, detail=(
            f"remote history refs {observed} != {(base_sha[:12], head_sha[:12])}"))


def history_title(state: dict) -> str:
    subject = state.get("title") or f"MR !{state.get('mrIid')}"
    return f"[Review copy] MR !{state.get('mrIid')} (merged): {subject}"


def history_body(state: dict, exported: dict) -> str:
    lines = [
        f"Review copy of {state.get('mrUrl') or ''} (merged, historical).",
        "",
        "Do not merge this PR and do not move the default branch: it replays",
        "the original change on uniquely named review-only refs.",
        "",
        f"Original MR: {state.get('mrUrl') or ''} (status merged, "
        f"!{state.get('mrIid')})",
        f"Original branches: {state.get('sourceBranch')} -> "
        f"{state.get('targetBranch')}",
        f"Historical base: {state.get('baseSha')}",
        f"Historical head (original): {state.get('headSha')}",
        f"Squash commit: {state.get('squashSha') or 'none recorded'}",
        f"Representation used: {state.get('representation')} "
        f"({state.get('chosenSha')})",
        f"Review refs: {state.get('baseRef')}..{state.get('headRef')}",
        f"Authoritative digest: {exported.get('authoritativeDigest')}",
        f"Authoritative files: {', '.join(exported.get('authoritativeFiles', []))}",
        "",
        f"{state.get('description') or ''}".strip(),
        "",
    ]
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def adopt_or_create_history(github, mirror: Path, repo: str, state: dict,
                            exported: dict) -> dict:
    head, base = state["headRef"], state["baseRef"]
    try:
        existing = github.find_open_pr(repo, head, base)
    except github.GithubError as error:
        raise Stop("pr-unreadable", detail=str(error)[:DETAIL_MAX])
    if existing is not None:
        record = read_and_compare(github, mirror, existing["url"], state,
                                  exported)
        record["reconciled"] = True
        return record
    if state.get("prUrl"):
        record = read_and_compare(github, mirror, state["prUrl"], state,
                                  exported)
        record["reconciled"] = True
        return record
    try:
        created = github.create_pr(repo, history_title(state),
                                   history_body(state, exported), base, head)
    except github.GithubError as error:
        raise Stop("pr-write-unknown", verdict="unknown", code=3,
                   detail=str(error)[:DETAIL_MAX])
    record = read_and_compare(github, mirror, created["url"], state, exported)
    record["number"] = created.get("number", record.get("number"))
    return record


def read_and_compare(github, mirror: Path, url: str, state: dict,
                     exported: dict) -> dict:
    try:
        record = github.read_pr(url)
    except github.GithubError as error:
        raise Stop("pr-unreadable", detail=str(error)[:DETAIL_MAX])
    if str(record.get("state") or "").lower() not in ("open", "opened"):
        raise Stop("pr-conflict", detail=f"{url} is {record.get('state')}")
    if record.get("headRefName") != state["headRef"] \
            or record.get("baseRefName") != state["baseRef"]:
        raise Stop("pr-conflict", detail=f"{url} names another direction")
    if str(record.get("headRefOid") or "").lower() != state["chosenSha"]:
        raise Stop("pr-conflict", detail=f"{url} head is not {state['chosenSha'][:12]}")
    if str(record.get("baseRefOid") or "").lower() != state["baseSha"]:
        raise Stop("pr-conflict", detail=f"{url} base is not {state['baseSha'][:12]}")
    refs = github.mirror_refs(mirror)
    if refs.get(state["baseRef"]) != state["baseSha"] \
            or refs.get(state["headRef"]) != state["chosenSha"]:
        raise Stop("pr-conflict", detail=(
            f"mirror history refs != {state['baseSha'][:12]}.."
            f"{state['chosenSha'][:12]}"))
    digest, files = diff_digest(mirror, state["baseSha"], state["chosenSha"])
    if digest != exported["authoritativeDigest"] \
            or sorted(files) != sorted(exported["authoritativeFiles"]):
        raise Stop("evidence-gap", detail=(
            "mirror history diff does not match the authoritative comparison"))
    return {"url": record.get("url") or url, "number": record.get("number"),
            "comparison": "exact"}
