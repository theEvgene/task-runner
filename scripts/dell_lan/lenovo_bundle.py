"""Lenovo bundle intake: request validation, bundle verification, one-time import.

Split from ``lenovo_publish.py`` to stay under the repo file-token guard;
behavior is unchanged. The thin driver keeps only arg parsing and ``main``.
"""

from __future__ import annotations

from pathlib import Path

from dell_lan_common import (
    DETAIL_MAX, PUBLISH_REQUEST_SCHEMA, Stop, git, git_ok, inside, load_json,
    manifest_digest, safe_name, sha256_bytes, sha256_file,
)
from lenovo_remote import require
from dell_standalone_common import TRANSFER_REQUEST_SCHEMA

ZERO_OID = "0" * 40
ACTIONS = ("import", "push", "mr", "undraft", "reconcile")


def validated(request_path: Path, expected_digest: str, reconcile: bool) -> dict:
    raw = request_path.read_bytes()
    actual = sha256_bytes(raw)
    if actual != (expected_digest or "").lower():
        raise Stop("request-digest-mismatch", detail=f"{actual} != {expected_digest}")
    request = load_json(request_path, "request-malformed")
    if request.get("schema") not in (PUBLISH_REQUEST_SCHEMA, TRANSFER_REQUEST_SCHEMA):
        raise Stop("request-malformed",
                   detail=f"expected {PUBLISH_REQUEST_SCHEMA} or {TRANSFER_REQUEST_SCHEMA}")
    shared = Path(str(request.get("shared") or ""))
    for name in ("manifest", "bundle"):
        if not inside(Path(str(request.get(name) or "")), shared):
            raise Stop("request-malformed", detail=f"{name} must live on the shared path")
    if request.get("schema") == PUBLISH_REQUEST_SCHEMA:
        if not inside(Path(str(request.get("descriptionFile") or "")), shared):
            raise Stop("request-malformed", detail="descriptionFile must live on the shared path")
    if not safe_name(Path(str(request.get("bundle"))).name):
        raise Stop("request-malformed", detail="bundle must be one contained file")
    branch_fields = ["branch"]
    if request.get("schema") == PUBLISH_REQUEST_SCHEMA:
        branch_fields.append("targetBranch")
    for name in branch_fields:
        value = str(request.get(name) or "")
        if not value or " " in value or value.startswith("-") or ".." in value:
            raise Stop("request-malformed", detail=f"{name} is not a safe branch name")
    authorized = request.get("authorized")
    if not isinstance(authorized, list):
        raise Stop("request-malformed", detail="authorized must be a list")
    unknown = sorted(set(authorized) - set(ACTIONS))
    if unknown:
        raise Stop("request-malformed", detail=f"unknown authorization: {', '.join(unknown)}")
    if request.get("schema") == PUBLISH_REQUEST_SCHEMA:
        if request.get("state") not in ("draft", "ready"):
            raise Stop("request-malformed", detail="the request state must be draft or ready")
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
        path = head = branch = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("HEAD "):
                head = line[len("HEAD "):].strip().lower()
            elif line.startswith("branch "):
                branch = line[len("branch "):].strip()
        if path:
            registered[str(Path(path).resolve())] = {"head": head, "branch": branch}
    return registered


def _worktree_clean(worktree: Path) -> str | None:
    ok, output = git_ok(worktree, "status", "--porcelain")
    if not ok:
        return None
    return output.strip()


def _sync_files(worktree: Path, previous: str, expected: str) -> tuple[bool, str]:
    """Move index/files from ``previous`` to ``expected``, refusing edits."""
    ok, output = git_ok(worktree, "read-tree", "-m", "-u", previous, expected)
    return ok, output


def import_once(request: dict, bundle: Path, tip: str, result: dict) -> Path:
    """Import the bundle once into a dedicated task worktree; never move checkout.

    First import creates ``refs/heads/<branch>`` with a compare-and-swap from
    zero. A repeat transfer moves the same branch only when the request names
    the explicit prior SHA, the branch still points there, the owned worktree
    is registered there and clean, and the new commit descends from it.
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
        raise Stop("artifact-mismatch", detail=f"bundle tip {tip} != {expected}")
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
            if resolved in registered:
                state = registered[resolved]
                raise Stop("branch-owned", detail=(
                    f"{worktree} is registered to {state.get('branch')} at "
                    f"{state.get('head')}"))
            if worktree.exists() and any(worktree.iterdir()):
                raise Stop("branch-owned", detail=f"{worktree} exists and is not empty")
            old = ZERO_OID
            ok, output = git_ok(checkout, "update-ref", f"refs/heads/{branch}", expected, old)
            if not ok:
                raise Stop("branch-owned", detail=output[:DETAIL_MAX])
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(checkout, "worktree", "add", "-q", str(worktree), branch)
            reused = False
        else:
            current = current.lower()
            if current == expected:
                state = registered.get(resolved)
                if (not state or state.get("head") != expected
                        or state.get("branch") != f"refs/heads/{branch}"):
                    raise Stop("branch-owned", detail=(
                        f"{worktree} is not registered to branch {branch} at {expected}"))
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
                        f"dirty; this run never publishes that as a clean state"[:DETAIL_MAX]))
                reused = True
                updated = False
            else:
                if previous is None:
                    raise Stop("branch-owned", detail=(
                        f"refs/heads/{branch} already points at {current}; another task owns it"))
                if current != previous:
                    raise Stop("previous-mismatch", detail=(
                        f"refs/heads/{branch} is at {current}, not the named prior {previous}"))
                state = registered.get(resolved)
                if (not state or state.get("head") != previous
                        or state.get("branch") != f"refs/heads/{branch}"):
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
                ok, swapped = git_ok(checkout, "update-ref", f"refs/heads/{branch}",
                                     expected, previous)
                if not ok:
                    ok2, current2 = git_ok(
                        checkout, "rev-parse", "--verify", f"refs/heads/{branch}")
                    raise Stop("branch-owned", detail=(
                        f"refs/heads/{branch} moved during the transfer "
                        f"({current2.lower()[:40] if ok2 else 'unreadable'}); nothing was "
                        f"moved: {swapped}"[:DETAIL_MAX]))
                updated = True
                reused = True
                files_synced, failure = _sync_files(worktree, previous, expected)
                if not files_synced:
                    raise Stop("import-unknown", verdict="unknown", code=3, detail=(
                        f"refs/heads/{branch} is at {expected} but the file sync from "
                        f"{previous} was refused; edits stay intact: {failure}"[:DETAIL_MAX]))
    finally:
        git_ok(checkout, "update-ref", "-d", temp_ref)

    final_state = _worktree_heads(checkout).get(resolved)
    if (not final_state or final_state.get("head") != expected
            or final_state.get("branch") != f"refs/heads/{branch}"):
        raise Stop("branch-owned", detail=(
            f"{worktree} does not own branch {branch} at {expected}"))
    ok, symbolic = git_ok(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok or symbolic.strip() != branch:
        raise Stop("branch-owned", detail=f"{worktree} does not own branch {branch}")
    if git(worktree, "rev-parse", "--verify", "HEAD").lower() != expected:
        raise Stop("artifact-mismatch", detail="task worktree HEAD is not the task commit")
    porcelain = _worktree_clean(worktree)
    if porcelain is None:
        raise Stop("import-unknown", verdict="unknown", code=3,
                   detail=f"cannot re-read {worktree} status before success")
    if porcelain:
        raise Stop("import-unknown", verdict="unknown", code=3,
                   detail=f"{worktree} is dirty before success at {expected}")
    result["import"] = {"branch": branch, "commit": expected, "worktree": str(worktree),
                        "reused": reused, "previous": previous, "updated": updated,
                        "filesSynced": files_synced, "recovered": recovered,
                        "clean": True, "matches": True}
    return worktree
