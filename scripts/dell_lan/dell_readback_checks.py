"""Observation checks behind ``dell_readback.py`` (iteration 1).

Read-only helpers that re-read the anchored copy, the staged bundle, and the
sealed Lenovo result. The queue and GitLab sources are consumed as-is:
``lan-task-bridge`` verifies the result MAC and its kind/id binding, and the
carried GitLab receipt is the output of ``gitlab-mr-flow`` ``mr_identity.py
capture``. Nothing here executes on a remote host.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dell_lan_common import (
    Stop as CommonStop, manifest_digest, read_key as read_queue_key,
)

SCHEMA = "dell-lan-readback/2"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
BUNDLE_SCHEMA = "git-bundle-manifest/1"
PUBLISH_RESULT_SCHEMA = "dell-lan-publish-result/1"
GITLAB_READBACK = "gitlab-mr-flow/scripts/mr_identity.py capture"
QUEUE_VERIFY = "lan-task-bridge/scripts/lan_bridge.py fetch (open_envelope: MAC + kind/id)"
BUNDLE_TOOL = "scripts/dell_lan/stage_bundle.py"
PUBLISH_DRIVER = "scripts/dell_lan/lenovo_publish.py"
DETAIL_MAX = 400


def documented_command(command: str) -> bool:
    """True for the two commands this skill documents as publication evidence."""

    if "lenovo_publish.py" in command:
        return True
    return "mr_identity.py" in command and "capture" in command

NEXT_ACTION = {
    "clean": "transfer for Lenovo publication when authorized",
    "head-unreadable": "repair repository access and re-read HEAD; transfer nothing",
    "base-mismatch": "re-anchor the copy to the published base SHA and re-verify",
    "git-failed": "repair repository access and re-read the working tree state",
    "worktree-unregistered": "recreate the task worktree from the anchored base and re-verify",
    "worktree-head-mismatch": "re-cut the task commit in the task worktree and re-verify",
    "bundle-manifest-malformed": "re-stage the bundle and manifest with scripts/dell_lan/stage_bundle.py (docs/dell-lan.md step 4); the external issue-517 stager stays an accepted alternative",
    "bundle-path-refused": "rebuild the manifest with contained relative paths and re-verify",
    "bundle-missing": "re-stage the bundle from the intact origin and re-verify",
    "bundle-mismatch": "re-stage the bundle from the intact origin and re-verify",
    "bundle-unreadable": "re-stage the bundle from the intact origin and re-verify",
    "bundle-tip-mismatch": "re-stage the bundle from the task worktree tip and re-verify",
    "key-refused": "fix the key location or content outside the checkout, the queue, and the shared folder, then re-verify",
    "queue-job-missing": "keep the slot, reconcile the job id with the Lenovo owner; a result is never trusted without its job",
    "queue-job-unverifiable": "keep the slot, reconcile the job id with the Lenovo owner; a result is never trusted without its job",
    "queue-job-mismatch": "re-submit the documented mr_identity readback as a new job id",
    "queue-result-unverifiable": "keep the slot, ask the Lenovo owner for the sealed result of this job id, never trust a hand-edited file",
    "queue-result-unreadable": "keep the slot, re-fetch the sealed result of this job id with the bridge",
    "publication-job-failed": "read the carried exit code and stderr, fix on the anchored base, then a new job id",
    "publication-readback-malformed": "ask the Lenovo owner to re-run the mr_identity capture readback for this job id",
    "publication-head-mismatch": "reconcile: the observed MR head is not the task commit; do not claim publication",
    "publication-push-mismatch": "reconcile: the remote branch head is not the task commit; do not claim publication",
    "publication-absent": "nothing is published: with per-action consent submit a publish request, or stop",
    "publication-local-head-mismatch": "reconcile: the Lenovo checkout does not carry the task commit; re-transfer and re-read",
    "publication-project-mismatch": "reconcile: the observed project is not the expected project",
    "publication-branch-mismatch": "reconcile: the observed branch is not the expected branch",
    "publication-fields-missing": "ask the Lenovo owner to re-run the mr_identity capture readback for this job id",
    "outcome-unknown": "keep the slot occupied, re-fetch the same job id, do not start a second writer",
    "artifact-stale": "re-sync and re-read before transfer; offline work stays on the anchored base",
    "published": "published readback recorded; keep the receipt as the publication evidence",
    "simulated": "fixture chain recorded; this is never live publication evidence",
    "ready": "transfer for Lenovo publication when authorized",
}


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


def load_bridge(bridge_dir: Path):
    path = Path(bridge_dir) / "lan_bridge.py"
    if not path.is_file():
        raise Stop("queue-result-unreadable", detail=f"bridge not found: {path}")
    # lan_bridge imports its sibling syntax gate; make that directory importable.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("dell_lan_bridge", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["dell_lan_bridge"] = module
    spec.loader.exec_module(module)
    return module


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def git(repo: Path, *spec: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *spec], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise Stop(
            "head-unreadable" if spec and spec[0] == "rev-parse" else "git-failed",
            detail=(proc.stderr or "").strip()[:DETAIL_MAX],
        )
    return proc.stdout


def worktrees(text: str) -> list[dict]:
    entries: list[dict] = []
    current: dict = {}
    for line in text.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD ") and current:
            current["head"] = line[len("HEAD "):].strip()
        elif not line.strip() and current:
            entries.append(current)
            current = {}
    if current:
        entries.append(current)
    return entries


def normalise_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.strip()))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def age_hours(epoch_seconds: float) -> float:
    return round((datetime.now(timezone.utc).timestamp() - epoch_seconds) / 3600.0, 2)


def iso_age_hours(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return age_hours(parsed.timestamp())


def check_bundle(manifest_path: Path, bundle_dir: Path, task_commit: str) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Stop("bundle-manifest-malformed", detail=str(error)[:DETAIL_MAX])
    if not isinstance(manifest, dict) or manifest.get("version") != BUNDLE_SCHEMA:
        raise Stop("bundle-manifest-malformed", detail=f"expected {BUNDLE_SCHEMA}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise Stop("bundle-manifest-malformed", detail="no file entries")
    if manifest_digest(files) != manifest.get("manifestDigest"):
        raise Stop("bundle-manifest-malformed",
                   detail="manifestDigest does not cover the file entries")
    for entry in files:
        rel = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ":" in rel:
            raise Stop("bundle-path-refused", detail=str(rel)[:DETAIL_MAX])
        if ".." in re.split(r"[\\/]", rel):
            raise Stop("bundle-path-refused", detail=str(rel)[:DETAIL_MAX])
        target = bundle_dir / rel.replace("/", os.sep)
        if not target.is_file():
            raise Stop("bundle-missing", detail=str(target)[:DETAIL_MAX])
        if sha256_file(target) != str(entry.get("sha256", "")).lower():
            raise Stop("bundle-mismatch", detail=str(target)[:DETAIL_MAX])
    bundle_path = bundle_dir / str(manifest.get("bundle") or "transfer.bundle").replace("/", os.sep)
    if not bundle_path.is_file():
        raise Stop("bundle-missing", detail=str(bundle_path)[:DETAIL_MAX])
    proc = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle_path)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise Stop("bundle-unreadable", detail=(proc.stderr or "").strip()[:DETAIL_MAX])
    heads = [line.split()[0].lower() for line in proc.stdout.splitlines() if line.strip()]
    if task_commit not in heads:
        raise Stop("bundle-tip-mismatch", detail=", ".join(heads)[:DETAIL_MAX])
    return {
        "provided": True,
        "manifestDigest": manifest.get("manifestDigest"),
        "files": len(files),
        "match": True,
        "tipBound": True,
    }


def unwrap_driver(document: dict, args, queue: dict) -> tuple[dict | None, dict]:
    """Validate the Lenovo publication driver envelope before its identity receipt.

    The driver is the submission, not a second receipt shape: publication
    evidence stays the nested ``mr_identity`` receipt. This function only
    proves that the envelope is internally consistent and that the push it
    observed carried the task commit.
    """

    outcome = document.get("outcome")
    if outcome == "unknown":
        raise Stop("outcome-unknown", verdict="unknown", code=3, queue=queue, detail=json.dumps({
            "stage": document.get("stage"), "reason": (document.get("error") or {}).get("reason"),
        })[:DETAIL_MAX])
    if outcome not in ("ok", "observed"):
        raise Stop("publication-job-failed", verdict="rejected", queue=queue,
                   detail=f"driver outcome {outcome}")
    identity = document.get("identity")
    if identity is None:
        raise Stop("publication-absent", queue=queue, detail=(
            "the Lenovo observation found no merge request for this branch; nothing is published"))
    push = document.get("push") if isinstance(document.get("push"), dict) else {}
    observed = str(push.get("observedRemoteSha") or "").lower()
    if observed and observed != args.task_commit:
        raise Stop("publication-push-mismatch", queue=queue, detail=f"{observed} != {args.task_commit}")
    mr = document.get("mr") if isinstance(document.get("mr"), dict) else {}
    if isinstance(identity, dict):
        if mr.get("url") and mr["url"] != identity.get("mr_url"):
            raise Stop("publication-readback-malformed", queue=queue,
                       detail="driver MR URL differs from the identity receipt")
        if mr.get("iid") is not None and mr["iid"] != identity.get("iid"):
            raise Stop("publication-readback-malformed", queue=queue,
                       detail="driver MR IID differs from the identity receipt")
    return identity, document


def shared_roots(args) -> list[Path]:
    """Every directory Lenovo can read in this run.

    ``--shared`` is the explicit root a queue readback must name. The staged
    bundle location is a second one when it is named separately, so a key
    cannot hide on the share merely because the run skipped the bundle check.
    """

    roots = [Path(args.shared)] if args.shared else []
    if args.bundle_dir:
        roots.append(Path(args.bundle_dir))
    elif args.bundle_manifest:
        roots.append(Path(args.bundle_manifest).parent)
    return roots


def check_result(args, bridge) -> dict:
    """Fetch and verify the sealed Lenovo result; return the observed facts."""
    queue = Path(args.queue)
    key_path = Path(args.key_file)
    shared = shared_roots(args)
    repos = [Path(args.repo)]
    if args.worktree:
        repos.append(Path(args.worktree))
    try:
        key = read_queue_key(bridge, queue, key_path, args.key_format,
                             repos=repos, shared=shared)
    except CommonStop as stop:
        raise Stop(stop.reason, verdict=stop.verdict, code=stop.code,
                   detail=stop.detail)

    # The submission is verified too: the same id must carry the documented
    # readback command, or the result proves nothing about this task.
    try:
        raw_job = bridge.read(str(queue / "jobs"), args.job_id)
    except bridge.JobNotFoundError:
        raise Stop("queue-job-missing", detail=args.job_id)
    except (bridge.BridgeError, OSError) as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    try:
        job = bridge.open_envelope(raw_job, key, "job", args.job_id)
    except bridge.ProtocolError as error:
        raise Stop("queue-job-unverifiable", detail=str(error)[:DETAIL_MAX])
    command = str(job.get("command") or "")
    if not documented_command(command):
        raise Stop("queue-job-mismatch", detail=command[:DETAIL_MAX])

    try:
        payload = bridge.fetch_result(
            str(queue), key, args.job_id,
            timeout_seconds=args.fetch_timeout, poll_seconds=args.poll,
        )
    except bridge.FetchTimeout:
        raise Stop("outcome-unknown", verdict="unknown", code=3)
    except bridge.ProtocolError as error:
        raise Stop("queue-result-unverifiable", detail=str(error)[:DETAIL_MAX])
    except (bridge.BridgeError, OSError) as error:
        raise Stop("queue-result-unreadable", detail=str(error)[:DETAIL_MAX])

    envelope = queue / "results" / f"{args.job_id}.json"
    try:
        envelope_hours = age_hours(envelope.stat().st_mtime)
    except OSError:
        envelope_hours = None

    status = payload.get("status")
    exit_code = payload.get("exit_code")
    timed_out = bool(payload.get("timed_out"))
    truncated = bool(payload.get("output_truncated"))
    facts = {
        "provided": True,
        "jobVerified": True,
        "jobCommandSha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "resultVerified": True,
        "status": status,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "outputTruncated": truncated,
        "envelopeHours": envelope_hours,
        "stderr": str(payload.get("stderr") or "")[:DETAIL_MAX],
    }
    if status != "ok" or isinstance(exit_code, bool) or exit_code != 0 or timed_out or truncated:
        document = None
        try:
            parsed = json.loads(str(payload.get("stdout") or ""))
            if isinstance(parsed, dict):
                document = parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            document = None
        driver_unknown = (isinstance(document, dict)
                          and document.get("schema") == PUBLISH_RESULT_SCHEMA
                          and document.get("outcome") == "unknown")
        if driver_unknown or timed_out or truncated or exit_code == 3:
            # No proof of failure, so no rejected verdict and no new-job-id
            # advice: a timed-out job may have completed late, a truncated
            # envelope hides the driver verdict, and exit 3 is the driver's
            # own unknown contract. Advising a new job id here could
            # double-publish; keep the slot and reconcile the same job id.
            raise Stop("outcome-unknown", verdict="unknown", code=3, queue=facts, detail=json.dumps({
                "stage": (document or {}).get("stage"),
                "reason": ((document or {}).get("error") or {}).get("reason"),
                "timedOut": timed_out, "outputTruncated": truncated,
                "exitCode": exit_code,
            })[:DETAIL_MAX])
        raise Stop("publication-job-failed", verdict="rejected", queue=facts, detail=json.dumps({
            "status": status, "exitCode": exit_code,
            "timedOut": timed_out, "outputTruncated": truncated,
            "stderr": facts["stderr"],
        })[:DETAIL_MAX])

    try:
        document = json.loads(str(payload.get("stdout") or ""))
    except json.JSONDecodeError as error:
        raise Stop("publication-readback-malformed", queue=facts, detail=str(error)[:DETAIL_MAX])
    driver = isinstance(document, dict) and document.get("schema") == PUBLISH_RESULT_SCHEMA
    if driver:
        identity, envelope = unwrap_driver(document, args, queue=facts)
    else:
        identity, envelope = document, None
    if not isinstance(identity, dict) or identity.get("schema_version") != 1:
        raise Stop("publication-readback-malformed", queue=facts,
                   detail="not an mr_identity capture receipt")

    head = str(identity.get("head_sha") or "").lower()
    local = identity.get("local") if isinstance(identity.get("local"), dict) else None
    missing = [
        name for name in ("gitlab_host", "project_path", "source_branch",
                          "target_branch", "mr_url", "state", "captured_at")
        if not identity.get(name)
    ]
    if not SHA_RE.match(head) or not SHA_RE.match(str(identity.get("target_sha") or "")):
        missing.append("head_sha/target_sha")
    iid = identity.get("iid")
    if isinstance(iid, bool) or not isinstance(iid, int) or iid < 1:
        missing.append("iid")
    if missing:
        raise Stop("publication-fields-missing", queue=facts,
                   detail=", ".join(missing)[:DETAIL_MAX])

    marked = identity.get("simulated") is True
    publication = {
        "readbackObserved": True,
        "bound": False,
        "provenance": args.provenance,
        "source": GITLAB_READBACK,
        "driver": PUBLISH_DRIVER if driver else None,
        "driverStage": (envelope or {}).get("stage"),
        "driverOutcome": (envelope or {}).get("outcome"),
        "authorized": (envelope or {}).get("authorized"),
        "pushObservedSha": ((envelope or {}).get("push") or {}).get("observedRemoteSha"),
        "mrCreated": ((envelope or {}).get("mr") or {}).get("created"),
        "gitlabHost": identity["gitlab_host"],
        "projectPath": identity["project_path"],
        "iid": iid,
        "mrUrl": identity["mr_url"],
        "state": identity["state"],
        "sourceBranch": identity["source_branch"],
        "targetBranch": identity["target_branch"],
        "headSha": head,
        "targetSha": str(identity["target_sha"]).lower(),
        "capturedAt": identity["captured_at"],
        "localChecked": local is not None,
        "declaredSimulated": marked,
        "simulated": marked or args.provenance != "live",
        "capturedHours": iso_age_hours(str(identity["captured_at"])),
    }

    if head != args.task_commit:
        raise Stop("publication-head-mismatch", queue=facts, publication=publication,
                   detail=f"{head} != {args.task_commit}")
    if local is not None:
        local_head = str(local.get("source_branch_head") or "").lower()
        if local_head != args.task_commit or local.get("head_matches_receipt") is not True:
            raise Stop("publication-local-head-mismatch", queue=facts,
                       publication=publication, detail=local_head[:DETAIL_MAX])
    if args.project and identity["project_path"] != args.project:
        raise Stop("publication-project-mismatch", queue=facts, publication=publication,
                   detail=str(identity["project_path"])[:DETAIL_MAX])
    if args.source_branch and identity["source_branch"] != args.source_branch:
        raise Stop("publication-branch-mismatch", queue=facts, publication=publication,
                   detail=str(identity["source_branch"])[:DETAIL_MAX])
    if args.target_branch and identity["target_branch"] != args.target_branch:
        raise Stop("publication-branch-mismatch", queue=facts, publication=publication,
                   detail=str(identity["target_branch"])[:DETAIL_MAX])

    publication["bound"] = True
    return {"queue": facts, "publication": publication}
