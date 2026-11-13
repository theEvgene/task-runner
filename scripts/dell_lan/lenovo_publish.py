#!/usr/bin/env python3
"""Lenovo side: import the staged task bundle and publish it through GitLab.

Runs as the single command of a signed LAN job, in one working directory and
one timeout. It reads a request file from the shared LAN path, re-hashes it
against the SHA-256 that travelled inside the MAC-bound job payload, verifies
the staged bundle against its manifest, imports it once into a dedicated Lenovo
task worktree, runs the existing ``gitlab-mr-flow`` freshness gate, pushes the
task branch, creates or updates the Draft merge request through
``publish_mr.py``, and reads the MR identity back through ``mr_identity.py``.
A ``ready`` request adds one consented step after the Draft publish: the
external ``remove_draft.py`` from ``--flow-dir`` marks the same MR Ready,
and the driver re-captures the identity and proves the title and
description still match the request input. The driver never waits for or
polls a pipeline; that read-only check stays with
``docs/pipeline-observer.md``.

Consent is per action: the request lists ``import``, ``push``, ``mr``,
``undraft``, and ``reconcile`` in ``authorized``, and the driver refuses an action that is not
listed. The reconciliation probe needs its own ``reconcile`` consent because
it reads the remote on Lenovo. A write whose outcome cannot be observed is
reported as ``unknown`` with exit 3 and is never retried here; ``--reconcile``
is the read-only probe that observes what actually happened before any retry.

Exit codes: ``0`` published or observed, ``1`` refused/mismatch, ``2`` usage or
missing consent, ``3`` unknown outcome.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, PUBLISH_REQUEST_SCHEMA, PUBLISH_RESULT_SCHEMA, Stop, default_flow_dir,
    git, git_ok, inside, load_json, manifest_digest, now_iso,
    safe_name, sha256_bytes, sha256_file,
)
from lenovo_remote import observe, preflight, publish, push, require

ZERO_OID = "0" * 40
ACTIONS = ("import", "push", "mr", "undraft", "reconcile")


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="lenovo_publish.py", description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--reconcile", action="store_true",
                        help="read-only probe: observe the remote branch and MR, never write")
    parser.add_argument("--flow-dir", type=Path, help="gitlab-mr-flow scripts dir (default: sibling)")
    return parser.parse_args(argv)


def validated(request_path: Path, expected_digest: str, reconcile: bool) -> dict:
    raw = request_path.read_bytes()
    actual = sha256_bytes(raw)
    if actual != (expected_digest or "").lower():
        raise Stop("request-digest-mismatch", detail=f"{actual} != {expected_digest}")
    request = load_json(request_path, "request-malformed")
    if request.get("schema") != PUBLISH_REQUEST_SCHEMA:
        raise Stop("request-malformed", detail=f"expected {PUBLISH_REQUEST_SCHEMA}")
    shared = Path(str(request.get("shared") or ""))
    for name in ("manifest", "bundle", "descriptionFile"):
        if not inside(Path(str(request.get(name) or "")), shared):
            raise Stop("request-malformed", detail=f"{name} must live on the shared path")
    if not safe_name(Path(str(request.get("bundle"))).name):
        raise Stop("request-malformed", detail="bundle must be one contained file")
    for name in ("branch", "targetBranch"):
        value = str(request.get(name) or "")
        if not value or " " in value or value.startswith("-") or ".." in value:
            raise Stop("request-malformed", detail=f"{name} is not a safe branch name")
    authorized = request.get("authorized")
    if not isinstance(authorized, list):
        raise Stop("request-malformed", detail="authorized must be a list")
    unknown = sorted(set(authorized) - set(ACTIONS))
    if unknown:
        raise Stop("request-malformed", detail=f"unknown authorization: {', '.join(unknown)}")
    if request.get("state") not in ("draft", "ready"):
        raise Stop("request-malformed", detail=(
            "the request state must be draft or ready"))
    if request.get("state") == "ready" and "undraft" not in (request.get("authorized") or []):
        raise Stop("consent-missing", code=2, detail=(
            "the user has not authorized the Lenovo undraft action for this run; "
            "ask once for that action and submit a new request that lists it"))
    previous = request.get("expectedPreviousCommit")
    if previous in (None, ""):
        request["expectedPreviousCommit"] = None
    else:
        text = str(previous).lower()
        if len(text) != 40 or any(char not in "0123456789abcdef" for char in text):
            raise Stop("request-malformed", detail="expectedPreviousCommit must be 40 hex")
        request["expectedPreviousCommit"] = text
    request["reconcileOnly"] = bool(reconcile or request.get("reconcileOnly"))
    return request


def verify_bundle(request: dict, result: dict) -> Path:
    bundle = Path(request["bundle"])
    manifest = load_json(Path(request["manifest"]), "manifest-mismatch")
    if manifest.get("version") != "git-bundle-manifest/1":
        raise Stop("manifest-mismatch", detail="not a git-bundle-manifest/1")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise Stop("manifest-mismatch", detail="no file entries")
    if manifest_digest(files) != manifest.get("manifestDigest"):
        raise Stop("manifest-mismatch", detail="manifestDigest does not cover the entries")
    for entry in files:
        path = Path(str((entry or {}).get("path") or ""))
        if not safe_name(path.name) or path.name != str(entry.get("path")):
            raise Stop("manifest-mismatch", detail="entry path must be one contained file")
        target = bundle.parent / path.name
        if not target.is_file():
            raise Stop("artifact-missing", detail=str(target)[:DETAIL_MAX])
        if target.stat().st_size != entry.get("bytes") or sha256_file(target) != entry.get("sha256"):
            raise Stop("artifact-mismatch", detail=str(target)[:DETAIL_MAX])
    if bundle.name != str(manifest.get("bundle")):
        raise Stop("manifest-mismatch", detail="manifest does not name this bundle")
    if not bundle.is_file():
        raise Stop("artifact-missing", detail=str(bundle)[:DETAIL_MAX])
    result["bundle"] = {
        "name": bundle.name,
        "sha256": sha256_file(bundle),
        "bytes": bundle.stat().st_size,
        "manifestDigest": manifest["manifestDigest"],
        "verified": True,
    }
    return bundle


def bundle_tip(checkout: Path, bundle: Path) -> str:
    ok, listed = git_ok(checkout, "bundle", "list-heads", str(bundle))
    if not ok:
        raise Stop("bundle-unreadable", detail=listed[:DETAIL_MAX])
    heads = [line.split()[0].lower() for line in listed.splitlines() if line.strip()]
    if not heads:
        raise Stop("bundle-unreadable", detail="bundle advertises no commit")
    return heads[0]


def _worktree_heads(checkout: Path) -> dict:
    entries = git(checkout, "worktree", "list", "--porcelain")
    registered = {}
    for block in entries.split("\n\n"):
        path = head = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("HEAD "):
                head = line[len("HEAD "):].strip().lower()
        if path:
            registered[str(Path(path).resolve())] = head
    return registered


def _worktree_clean(worktree: Path) -> str | None:
    ok, output = git_ok(worktree, "status", "--porcelain")
    if not ok:
        return None
    return output.strip()


def _sync_files(worktree: Path, previous: str, expected: str) -> tuple[bool, str]:
    """Move the index and the files from ``previous`` to ``expected``, refusing edits.

    ``git read-tree -m -u`` is a two-tree update, not a reset: an entry whose
    working-tree content differs from the index is never overwritten, and an
    untracked file that a new path would occupy aborts the update. It runs
    after the ref swap and is not atomic with it, so a refusal is reported by
    the caller as a recoverable partial state instead of a success.
    """

    ok, output = git_ok(worktree, "read-tree", "-m", "-u", previous, expected)
    return ok, output


def import_once(request: dict, bundle: Path, tip: str, result: dict) -> Path:
    """Import the bundle once into a dedicated task worktree; never move the checkout.

    First import creates ``refs/heads/<branch>`` with a compare-and-swap from
    zero. A repeat transfer moves the same branch only when the request names
    the explicit prior SHA (``expectedPreviousCommit``), the branch still
    points there, the owned worktree is registered there and clean, and the
    new commit descends from it. Every mismatch fails closed.

    The repeat move itself is a true atomic compare-and-swap on the ref:
    ``git update-ref refs/heads/<branch> <expected> <previous>`` takes the ref
    lock and writes only while the ref still holds exactly ``previous``. A
    concurrent commit ``Q`` that lands after the checks is therefore refused
    with nothing moved — including the case ``git merge --ff-only`` cannot
    see, a same-tree ``Q`` that is an ancestor of the new commit. Git merge
    provides no compare-and-swap and the driver never treats it as one.

    The index and files are then synced by ``git read-tree -m -u <previous>
    <expected>``: a refusing two-tree update that never overwrites an entry
    with local changes and aborts when an untracked file would be replaced.
    That file step is separate from the ref swap and is not atomic with it; a
    refusal, or any residue read back afterwards, is reported as
    ``import-unknown`` (exit 3) with the edits intact and the branch left at
    the new commit, so a partial state stays recoverable and is never a
    silent success. No ``reset --hard`` and no check-then-``reset`` appears on
    any path.

    A rerun when the branch is already at the new commit publishes only a
    clean worktree: it completes a sync that an interrupt left unfinished,
    and reports ``import-unknown`` when a real edit is in the way instead of
    accepting a dirty tree as a clean publication.
    """

    require(request, "import")
    checkout = Path(request["lenovoCheckout"])
    branch = request["branch"]
    expected = request["expectedCommit"].lower()
    previous = (request.get("expectedPreviousCommit") or None)
    previous = previous.lower() if previous else None
    if previous == expected:
        previous = None
    if tip != expected:
        raise Stop("artifact-mismatch", detail=f"bundle tip {tip} != expected {expected}")
    ok, _ = git_ok(checkout, "rev-parse", "--verify", f"{request['expectedBaseSha']}^{{commit}}")
    if not ok:
        raise Stop("base-mismatch", detail="Lenovo checkout does not carry the expected base commit")

    temp_ref = f"refs/tmp/dell-import/{request['token']}"
    ok, _ = git_ok(checkout, "fetch", "-q", str(bundle), f"HEAD:{temp_ref}")
    if not ok:
        ok, _ = git_ok(checkout, "fetch", "-q", str(bundle), f"{expected}:{temp_ref}")
    if not ok:
        raise Stop("bundle-unreadable", detail="cannot fetch the task commit from the bundle")
    try:
        fetched = git(checkout, "rev-parse", temp_ref).lower()
        if fetched != expected:
            raise Stop("artifact-mismatch", detail=f"fetched {fetched} != {expected}")

        ok, current = git_ok(checkout, "rev-parse", "--verify", f"refs/heads/{branch}")
        worktree = Path(request["lenovoWorktree"])
        registered = _worktree_heads(checkout)
        resolved = str(worktree.resolve())
        updated = False
        recovered = False
        files_synced = True

        if not ok:
            if previous is not None:
                raise Stop("previous-mismatch", detail=(
                    f"refs/heads/{branch} does not exist; the named prior "
                    f"{previous} names nothing to update"))
            old = ZERO_OID
            ok, output = git_ok(checkout, "update-ref", f"refs/heads/{branch}", expected, old)
            if not ok:
                raise Stop("branch-owned", detail=output[:DETAIL_MAX])
            if resolved in registered:
                if registered[resolved] != expected:
                    raise Stop("branch-owned", detail=f"{worktree} is registered at {registered[resolved]}")
                reused = True
            else:
                if worktree.exists() and any(worktree.iterdir()):
                    raise Stop("branch-owned", detail=f"{worktree} exists and is not empty")
                worktree.parent.mkdir(parents=True, exist_ok=True)
                git(checkout, "worktree", "add", "-q", str(worktree), branch)
                reused = False
        else:
            current = current.lower()
            if current == expected:
                if resolved not in registered or registered[resolved] != expected:
                    raise Stop("branch-owned", detail=(
                        f"{worktree} is not registered at {expected}"))
                # Idempotent rerun, or the completion of a move an interrupt
                # left half-applied: the ref is already at the recorded
                # commit, so this run only has to prove the files are there.
                # A dirty worktree is never published as a clean state.
                porcelain = _worktree_clean(worktree)
                if porcelain is None:
                    raise Stop("worktree-unreadable", detail=f"cannot read {worktree} status")
                if porcelain and previous is not None:
                    if _sync_files(worktree, previous, expected)[0]:
                        porcelain = _worktree_clean(worktree)
                        recovered = True
                if porcelain is None:
                    raise Stop("import-unknown", verdict="unknown", code=3,
                               detail=f"cannot re-read {worktree} status after the sync")
                if porcelain:
                    raise Stop("import-unknown", verdict="unknown", code=3, detail=(
                        f"refs/heads/{branch} is already at {expected} but {worktree} is "
                        f"dirty ({porcelain}); this run never publishes that as a clean "
                        f"state. Keep or commit the edits, then rerun the same request"
                        [:DETAIL_MAX]))
                reused = True
                updated = False
            else:
                if previous is None:
                    raise Stop("branch-owned", detail=(
                        f"refs/heads/{branch} already points at {current}; another task owns it"))
                if current != previous:
                    raise Stop("previous-mismatch", detail=(
                        f"refs/heads/{branch} is at {current}, not the named prior {previous}; "
                        "re-read the branch before any retry"))
                if resolved not in registered or registered[resolved] != previous:
                    raise Stop("branch-owned", detail=(
                        f"{worktree} is not the registered owner at {previous}"))
                ok, symbolic = git_ok(worktree, "rev-parse", "--abbrev-ref", "HEAD")
                if not ok or symbolic.strip() != branch:
                    raise Stop("branch-owned", detail=(
                        f"{worktree} does not own branch {branch}"))
                porcelain = _worktree_clean(worktree)
                if porcelain is None:
                    raise Stop("worktree-unreadable", detail=f"cannot read {worktree} status")
                if porcelain:
                    raise Stop("worktree-dirty", detail=(
                        f"{worktree} has uncommitted changes; clean it before a repeat transfer"))
                ok, _ = git_ok(checkout, "merge-base", "--is-ancestor", previous, expected)
                if not ok:
                    raise Stop("non-descendant", detail=(
                        f"{expected} does not descend from {previous}; refusing a rewrite"))
                # The move itself: one atomic compare-and-swap on the ref. The
                # old value is the exact prior SHA, so the write happens only
                # while the branch still holds it; a concurrent commit at any
                # other SHA fails the swap and moves nothing. A fast-forward
                # merge cannot make that guarantee — it accepts any ancestor
                # of the new commit, including a same-tree Q the request never
                # named.
                ok, swapped = git_ok(checkout, "update-ref", f"refs/heads/{branch}",
                                     expected, previous)
                if not ok:
                    ok2, current2 = git_ok(
                        checkout, "rev-parse", "--verify", f"refs/heads/{branch}")
                    raise Stop("branch-owned", detail=(
                        f"refs/heads/{branch} moved during the transfer "
                        f"({current2.lower()[:40] if ok2 else 'unreadable'}); the named "
                        f"prior {previous} is not the branch head, so nothing was "
                        f"moved: {swapped}"[:DETAIL_MAX]))
                updated = True
                reused = True
                files_synced, failure = _sync_files(worktree, previous, expected)
                if not files_synced:
                    # The ref is at the new commit and the files are not: a
                    # recoverable partial state with every edit intact, never
                    # a silent success. The ref swap is not rolled back —
                    # that would be a second unverified ref move over a tree
                    # that may already be partly synced.
                    raise Stop("import-unknown", verdict="unknown", code=3, detail=(
                        f"refs/heads/{branch} is at {expected} but the file sync from "
                        f"{previous} was refused; the edits stay in place and a rerun "
                        f"completes the sync when they are reconciled: {failure}"
                        [:DETAIL_MAX]))
    finally:
        git_ok(checkout, "update-ref", "-d", temp_ref)

    if git(worktree, "rev-parse", "--verify", "HEAD").lower() != expected:
        raise Stop("artifact-mismatch", detail="task worktree HEAD is not the task commit")
    if updated or recovered:
        porcelain = _worktree_clean(worktree)
        if porcelain is None:
            raise Stop("import-unknown", verdict="unknown", code=3,
                       detail=f"cannot re-read {worktree} status after the move")
        if porcelain:
            raise Stop("import-unknown", verdict="unknown", code=3,
                       detail=f"{worktree} is dirty after the move to {expected}")
    result["import"] = {"branch": branch, "commit": expected, "worktree": str(worktree),
                        "reused": reused, "previous": previous, "updated": updated,
                        "filesSynced": files_synced, "recovered": recovered}
    return worktree


def main(argv=None) -> int:
    args = parse(argv)
    result = {"schema": PUBLISH_RESULT_SCHEMA, "outcome": "refused", "stage": "request",
              "token": None, "authorized": [], "bundle": None, "import": None,
              "preflight": None, "push": None, "mr": None, "identity": None,
              "undraft": None, "pipeline": {"waited": False, "polled": False},
              "error": None, "finishedAt": now_iso()}
    code = 0
    try:
        request = validated(Path(args.request), args.request_sha256, args.reconcile)
        result["token"] = request["token"]
        result["authorized"] = request["authorized"]
        if request["reconcileOnly"]:
            require(request, "reconcile")
        result["stage"] = "bundle"
        bundle = verify_bundle(request, result)
        tip = bundle_tip(Path(request["lenovoCheckout"]), bundle)
        if request["reconcileOnly"]:
            result["stage"] = "reconcile"
            observe(request, Path(request["lenovoWorktree"]), args.flow_dir or default_flow_dir(), result)
            result["outcome"] = "observed"
        else:
            result["stage"] = "import"
            worktree = import_once(request, bundle, tip, result)
            result["stage"] = "preflight"
            preflight(request, worktree, args.flow_dir or default_flow_dir(), result)
            result["stage"] = "push"
            push(request, worktree, result)
            result["stage"] = "mr"
            receipt = publish(request, worktree, args.flow_dir or default_flow_dir(), result)
            result["stage"] = "readback"
            if str(receipt.get("head_sha") or "").lower() != request["expectedCommit"]:
                raise Stop("publication-head-mismatch",
                           detail=f"{receipt.get('head_sha')} != {request['expectedCommit']}")
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
    result["finishedAt"] = now_iso()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(f"lenovo publish {result['outcome']} : {result['stage']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
