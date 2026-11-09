#!/usr/bin/env python3
"""Dell-side factual readback through the existing signed LAN queue.

Read-only. Observes the anchored copy HEAD, the task worktree registration
and its head, the staged git bundle (manifest hashes plus the bundle's own
tip), the sealed Lenovo result fetched from the immutable signed queue, and
the GitLab readback that result carries. No checkout is moved or cleaned,
nothing executes on a remote host, and a failed binding stops the run.

The verdict never exceeds its evidence. ``ready`` means transfer-ready only.
``published`` requires a MAC-verified result whose GitLab receipt binds
``head_sha`` to ``--task-commit`` and which is not marked simulated.
``simulated`` is the same chain labelled as a fixture. ``unknown`` (exit 3)
keeps the one queue slot: reconcile before any retry.

Evidence sources, composed as-is:
- ``lan-task-bridge/scripts/lan_bridge.py`` verifies the sealed result;
- ``gitlab-mr-flow/scripts/mr_identity.py capture`` produced the receipt;
- ``scripts/issue-517`` produced the staged bundle and its manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dell_readback_checks import (
    BUNDLE_TOOL, GITLAB_READBACK, JOB_RE, NEXT_ACTION, QUEUE_VERIFY, SCHEMA,
    SHA_RE, Stop, age_hours, check_bundle, check_result, git, inside,
    load_bridge, normalise_path, worktrees,
)
from dell_lan_common import default_bridge_dir


def parse(argv):
    parser = argparse.ArgumentParser(prog="dell_readback.py", description="Dell LAN local-dev readback")
    parser.add_argument("--repo", type=Path, required=True, help="anchored copy (read only)")
    parser.add_argument("--expected-base", required=True, help="40-hex base SHA the copy must have")
    parser.add_argument("--task-commit", required=True, help="40-hex task commit the bundle carries")
    parser.add_argument("--worktree", help="task worktree that must be registered at the task commit")
    parser.add_argument("--bundle-manifest", type=Path, help="staged git-bundle-manifest/1")
    parser.add_argument("--bundle-dir", type=Path, help="directory holding the staged bundle")
    parser.add_argument("--queue", help="signed queue directory (jobs/ and results/)")
    parser.add_argument("--shared",
                        help="absolute LAN share that holds the queue and the staged "
                             "bundle: a protected root for the queue key")
    parser.add_argument("--key-file", help="32-byte queue key outside the repository, queue, and shared folder")
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--job-id", help="32-hex job id bound into the sealed result")
    parser.add_argument("--fetch-timeout", type=float, default=660.0)
    parser.add_argument("--poll", type=float, default=10.0)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--provenance", default="fixture", choices=("fixture", "live"))
    parser.add_argument("--project", help="expected project_path pin")
    parser.add_argument("--source-branch", help="expected source_branch pin")
    parser.add_argument("--target-branch", help="expected target_branch pin")
    parser.add_argument("--bridge-dir", type=Path, help="lan-task-bridge scripts directory")
    parser.add_argument("--receipt", type=Path, required=True, help="readback receipt outside the checkout")
    return parser.parse_args(argv)


def usage(message: str) -> int:
    print(f"dell readback usage: {message}", file=sys.stderr)
    return 2


def blank_receipt(args) -> dict:
    return {
        "schema": SCHEMA,
        "provenance": args.provenance,
        "verdict": None,
        "reason": None,
        "detail": "",
        "jobId": args.job_id,
        "expectedBaseSha": args.expected_base,
        "actualHead": None,
        "taskCommit": args.task_commit,
        "baseMatch": False,
        "worktree": {"provided": bool(args.worktree), "registered": False,
                     "head": None, "headMatchesTaskCommit": False},
        "bundle": {"provided": bool(args.bundle_manifest), "manifestDigest": None,
                   "files": 0, "match": False, "tipBound": False},
        "queue": {"provided": bool(args.job_id), "jobVerified": False,
                  "jobCommandSha256": None, "resultVerified": False,
                  "status": None, "exitCode": None, "timedOut": None,
                  "outputTruncated": None, "envelopeHours": None, "stderr": None},
        "publication": {"readbackObserved": False, "bound": False,
                        "provenance": args.provenance, "source": GITLAB_READBACK,
                        "driver": None, "driverStage": None, "driverOutcome": None,
                        "authorized": None, "pushObservedSha": None, "mrCreated": None,
                        "gitlabHost": None, "projectPath": None, "iid": None,
                        "mrUrl": None, "state": None, "sourceBranch": None,
                        "targetBranch": None, "headSha": None, "targetSha": None,
                        "capturedAt": None, "localChecked": False,
                        "declaredSimulated": None, "simulated": None,
                        "capturedHours": None},
        "evidence": {"queue": QUEUE_VERIFY, "gitlab": GITLAB_READBACK, "bundle": BUNDLE_TOOL},
        "freshness": {"maxAgeHours": args.max_age_hours, "oldestHours": None, "stale": False},
        "nextAction": None,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def observe(args, receipt: dict, ages: dict) -> None:
    if not args.repo.is_dir():
        raise Stop("git-failed", detail=f"repository not found: {args.repo}")
    actual = git(args.repo, "rev-parse", "--verify", "HEAD").strip().lower()
    receipt["actualHead"] = actual
    receipt["baseMatch"] = actual == args.expected_base
    if not receipt["baseMatch"]:
        raise Stop("base-mismatch", detail=f"{actual} != {args.expected_base}")

    if args.worktree:
        entries = worktrees(git(args.repo, "worktree", "list", "--porcelain"))
        wanted = normalise_path(args.worktree)
        match = next((item for item in entries if normalise_path(item["path"]) == wanted), None)
        receipt["worktree"]["registered"] = match is not None
        if match is None:
            raise Stop("worktree-unregistered", detail=args.worktree)
        head = str(match.get("head") or "").lower()
        receipt["worktree"]["head"] = head or None
        receipt["worktree"]["headMatchesTaskCommit"] = head == args.task_commit
        if not receipt["worktree"]["headMatchesTaskCommit"]:
            raise Stop("worktree-head-mismatch", detail=f"{head} != {args.task_commit}")

    if args.bundle_manifest:
        bundle_dir = args.bundle_dir or args.bundle_manifest.parent
        receipt["bundle"] = check_bundle(args.bundle_manifest, bundle_dir, args.task_commit)
        ages["bundle"] = age_hours(args.bundle_manifest.stat().st_mtime)

    if args.job_id:
        bridge_dir = args.bridge_dir or default_bridge_dir()
        observed = check_result(args, load_bridge(bridge_dir))
        receipt["queue"] = observed["queue"]
        receipt["publication"] = observed["publication"]
        if observed["queue"]["envelopeHours"] is not None:
            ages["result"] = observed["queue"]["envelopeHours"]
        if observed["publication"]["capturedHours"] is not None:
            ages["readback"] = observed["publication"]["capturedHours"]


def write_receipt(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(handle)
    try:
        Path(temp_name).write_text(text + "\n", encoding="utf-8")
        os.replace(temp_name, path)
    finally:
        if Path(temp_name).exists():
            Path(temp_name).unlink()


def main(argv=None) -> int:
    args = parse(argv)
    for name in ("expected_base", "task_commit"):
        if not SHA_RE.match(getattr(args, name) or ""):
            return usage(f"--{name.replace('_', '-')} must be 40 hex")
    args.expected_base = args.expected_base.lower()
    args.task_commit = args.task_commit.lower()
    if args.job_id and not JOB_RE.match(args.job_id):
        return usage("--job-id must be 32 lowercase hex")
    if bool(args.job_id) != bool(args.queue and args.key_file):
        return usage("--job-id needs --queue and --key-file, and they need --job-id")
    if args.job_id and not args.shared:
        return usage("--job-id needs --shared: a queue readback must prove the key "
                     "is outside the shared LAN folder")
    for name in ("repo", "worktree", "queue", "shared", "bundle_dir",
                 "bundle_manifest"):
        value = getattr(args, name)
        if value is not None and not Path(value).is_absolute():
            return usage(f"--{name.replace('_', '-')} must be an absolute path: the "
                         "key is proven outside it")
    if args.max_age_hours < 0:
        return usage("--max-age-hours must be zero or positive")
    if args.fetch_timeout <= 0 or args.poll <= 0:
        return usage("--fetch-timeout and --poll must be positive")
    if inside(args.receipt, args.repo):
        return usage("--receipt must stay outside the checkout")
    if args.worktree and inside(args.receipt, Path(args.worktree)):
        return usage("--receipt must stay outside the task worktree")

    receipt = blank_receipt(args)
    ages: dict[str, float] = {}
    verdict, reason, code, detail = "ready", "clean", 0, ""
    try:
        observe(args, receipt, ages)
    except Stop as stop:
        verdict, reason, code, detail = stop.verdict, stop.reason, stop.code, stop.detail
        if stop.queue:
            receipt["queue"] = stop.queue
            if stop.queue.get("envelopeHours") is not None:
                ages["result"] = stop.queue["envelopeHours"]
        if stop.publication:
            receipt["publication"] = stop.publication
            if stop.publication.get("capturedHours") is not None:
                ages["readback"] = stop.publication["capturedHours"]
    except OSError as error:
        verdict, reason, code = "mismatch", "git-failed", 1
        detail = str(error)[:400]

    if reason == "clean":
        if ages and max(ages.values()) > args.max_age_hours:
            verdict, reason = "stale", "artifact-stale"
        elif receipt["publication"]["bound"]:
            verdict = "simulated" if receipt["publication"]["simulated"] else "published"
            reason = verdict
        else:
            verdict, reason = "ready", "ready"

    receipt["verdict"] = verdict
    receipt["reason"] = reason
    receipt["detail"] = detail
    if ages:
        receipt["freshness"]["oldestHours"] = max(ages.values())
        receipt["freshness"]["stale"] = receipt["freshness"]["oldestHours"] > args.max_age_hours
    receipt["nextAction"] = NEXT_ACTION.get(reason, "stop and reconcile before any retry")

    text = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    try:
        write_receipt(args.receipt, text)
    except OSError as error:
        print(f"dell readback {verdict} : receipt unwritable at {args.receipt}: {error}", file=sys.stderr)
        print(text)
        return 3
    print(text)
    print(f"dell readback {verdict} : {reason}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
