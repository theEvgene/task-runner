#!/usr/bin/env python3
"""Read-only strict gate for one MR head SHA with declared required checks.

Single-shot read of the pinned SHA: finds the newest pipeline for that
SHA, checks every required parent job and required child (bridge)
pipeline, and never infers success from an unchecked run. A green parent
with a missing required check is ``missing_required_check``, never
``passed``. A retried job is read by its highest numeric id, so the
newest attempt decides and a superseded attempt is never reported.

The receipt carries names, statuses, notes, and URLs only: no tokens
and no job-log text. All GitLab reads go through the injected ``get``
callable; there is no network or mutation here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


class GateInputError(ValueError):
    """Bad input or unusable profile; fail closed before any GitLab read."""


class GitlabUnavailable(RuntimeError):
    """A GitLab read failed; the caller reports ``unavailable``."""


PROFILE_SCHEMA = "pipeline-observer-checks/1"

SUCCESS = {"success"}
FAILURES = {"failed"}
CANCELED = {"canceled"}
SKIPPED = {"skipped"}
MANUAL = {"manual"}
TERMINAL = SUCCESS | FAILURES | CANCELED | SKIPPED | MANUAL
PENDING = {"created", "pending", "running", "preparing",
           "waiting_for_resource", "scheduled"}

MR_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>[^/]+)/(?P<project>.+?)"
    r"/-/merge_requests/(?P<iid>\d+)(?:/(?:diffs|pipelines|commits))?/?$"
)


def parse_mr_url(url: str) -> dict[str, Any]:
    match = MR_URL_RE.match((url or "").strip())
    if not match:
        raise GateInputError(
            "not a full MR URL; expected "
            "https://<host>/<group>/<project>/-/merge_requests/<iid>"
        )
    project = match.group("project")
    return {
        "scheme": match.group("scheme"),
        "host": match.group("host"),
        "project_path": project,
        "project_encoded": quote(project, safe=""),
        "iid": int(match.group("iid")),
    }


def canonical_url(url: str) -> str:
    part = parse_mr_url(url)
    return (
        f"{part['scheme']}://{part['host']}/{part['project_path']}"
        f"/-/merge_requests/{part['iid']}"
    )


def normalize_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    if profile is None:
        return {"schema": PROFILE_SCHEMA, "source": "none",
                "required_jobs": [], "child_pipelines": []}
    if not isinstance(profile, dict):
        raise GateInputError("required-check profile must be a JSON object")
    if profile.get("schema", PROFILE_SCHEMA) != PROFILE_SCHEMA:
        raise GateInputError(
            f"required-check profile schema must be {PROFILE_SCHEMA}")
    jobs = profile.get("required_jobs") or []
    if (not isinstance(jobs, list)
            or not all(isinstance(j, str) and j.strip() for j in jobs)):
        raise GateInputError("required_jobs must be a list of job names")
    children: list[dict[str, Any]] = []
    raw = profile.get("required_child_pipelines")
    if raw is None:
        raw = profile.get("child_pipelines") or []
    if not isinstance(raw, list):
        raise GateInputError("required_child_pipelines must be a list")
    for entry in raw:
        if isinstance(entry, str):
            entry = {"name": entry}
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not entry["name"].strip()):
            raise GateInputError(
                "required child entries need a non-empty name")
        cjobs = entry.get("required_jobs") or []
        if (not isinstance(cjobs, list)
                or not all(isinstance(j, str) and j.strip()
                           for j in cjobs)):
            raise GateInputError(
                f"required_jobs for child {entry.get('name')!r} "
                "must be a list of job names")
        children.append({"name": entry["name"].strip(),
                         "required_jobs": [j.strip() for j in cjobs]})
    return {"schema": PROFILE_SCHEMA,
            "source": profile.get("source") or "inline",
            "required_jobs": [j.strip() for j in jobs],
            "child_pipelines": children}


def load_profile(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise GateInputError(
            f"cannot read required-check profile {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise GateInputError(
            f"required-check profile {path} is not valid JSON: "
            f"{error}") from error
    if isinstance(payload, dict) and "schema" not in payload:
        payload = {**payload, "schema": PROFILE_SCHEMA}
    normalized = normalize_profile(payload)
    normalized["source"] = str(path)
    return normalized


def _wrap(get: Callable[[str, str], Any], host: str, path: str) -> Any:
    try:
        return get(host, path)
    except GateInputError:
        raise
    except Exception as error:
        raise GitlabUnavailable(str(error)[:300]) from error


def _newest_by_name(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    newest: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict) or not job.get("name"):
            continue
        prev = newest.get(str(job["name"]))
        try:
            cur_id = int(job.get("id", -1))
            prev_id = int(prev.get("id", -1)) if prev else -1
        except (TypeError, ValueError):
            continue
        if prev is None or cur_id > prev_id:
            newest[str(job["name"])] = job
    return newest


def _check_job_state(state: str) -> str:
    s = (state or "").lower()
    if s in SUCCESS:
        return "success"
    if s in FAILURES:
        return "failed"
    if s in PENDING:
        return "pending"
    if s in CANCELED:
        return "canceled"
    if s in SKIPPED:
        return "skipped"
    if s in MANUAL:
        return "policy_blocked"
    return "pending"


def observe(mr_url: str, profile: dict[str, Any] | None,
            get: Callable[[str, str], Any]) -> dict[str, Any]:
    """Observe the current head SHA pipeline once; never waits, never mutates."""
    ident = parse_mr_url(mr_url)
    host = ident["host"]
    proj = ident["project_encoded"]
    canonical = canonical_url(mr_url)
    required = normalize_profile(profile)
    try:
        mr = _wrap(get, host, f"/projects/{proj}/merge_requests/{ident['iid']}")
        if not isinstance(mr, dict) or not mr.get("sha"):
            raise GitlabUnavailable("merge request head unavailable")
        sha = str(mr["sha"]).lower()
        pipelines = _wrap(
            get, host, f"/projects/{proj}/merge_requests/{ident['iid']}/pipelines")
        if not isinstance(pipelines, list):
            raise GitlabUnavailable("pipeline list unavailable")
        same = [p for p in pipelines
                if isinstance(p, dict)
                and str(p.get("sha", "")).lower() == sha]
        if not same:
            return _receipt(canonical, sha, None, None, "pending",
                            "no_pipeline_for_sha",
                            "no pipeline exists for the current SHA yet",
                            required, [], None, None)
        root = max(same, key=lambda p: int(p.get("id", -1)))
        pid = root.get("id")
        detail = _wrap(get, host, f"/projects/{proj}/pipelines/{pid}")
        jobs_raw = _wrap(
            get, host, f"/projects/{proj}/pipelines/{pid}/jobs?per_page=100")
        bridges_raw = _wrap(
            get, host, f"/projects/{proj}/pipelines/{pid}/bridges?per_page=100")
        if not isinstance(jobs_raw, list) or not isinstance(bridges_raw, list):
            raise GitlabUnavailable("job or bridge list unavailable")
        newest = _newest_by_name([j for j in jobs_raw
                                  if isinstance(j, dict)])
        required_checks: list[dict[str, Any]] = []
        missing: list[str] = []
        failed: list[dict[str, Any]] = []
        outcome = "passed"
        for name in required["required_jobs"]:
            job = newest.get(name)
            if job is None:
                missing.append(name)
                required_checks.append({"name": name, "status": "missing"})
                continue
            state = _check_job_state(str(job.get("status")))
            required_checks.append({"name": name, "status": state,
                                    "job_id": job.get("id"),
                                    "url": job.get("web_url")})
            if state == "failed":
                failed.append({"name": name, "id": job.get("id"),
                               "url": job.get("web_url")})
            elif state == "pending":
                if outcome == "passed":
                    outcome = "pending"
            elif state != "success":
                if outcome in ("passed", "pending"):
                    outcome = state
        for child in required["child_pipelines"]:
            cname = child["name"]
            bridge = next((b for b in bridges_raw
                           if isinstance(b, dict) and b.get("name") == cname),
                          None)
            downstream = (bridge or {}).get("downstream_pipeline") or {}
            child_id = downstream.get("id")
            if bridge is None or child_id is None:
                missing.append(f"child:{cname}")
                required_checks.append({"name": f"child:{cname}",
                                        "status": "missing"})
                continue
            cproj = downstream.get("project_id", proj)
            cdetail = _wrap(get, host, f"/projects/{cproj}/pipelines/{child_id}")
            if (not isinstance(cdetail, dict)
                    or str(cdetail.get("sha", "")).lower() != sha):
                missing.append(f"child:{cname}")
                required_checks.append({"name": f"child:{cname}",
                                        "status": "missing",
                                        "note": "child pipeline "
                                                "does not prove this SHA"})
                continue
            cjobs_raw = _wrap(
                get, host,
                f"/projects/{cproj}/pipelines/{child_id}/jobs?per_page=100")
            if not isinstance(cjobs_raw, list):
                raise GitlabUnavailable("child job list unavailable")
            cnewest = _newest_by_name([j for j in cjobs_raw
                                       if isinstance(j, dict)])
            if not child["required_jobs"]:
                cstate = _check_job_state(str(cdetail.get("status")))
                required_checks.append({"name": f"child:{cname}",
                                        "status": cstate,
                                        "pipeline_id": child_id})
                if cstate == "failed":
                    failed.append({"name": f"child:{cname}",
                                   "id": child_id})
                elif cstate == "pending" and outcome == "passed":
                    outcome = "pending"
                elif cstate not in ("success", "pending") and outcome in (
                        "passed", "pending"):
                    outcome = cstate
                continue
            for cj in child["required_jobs"]:
                job = cnewest.get(cj)
                label = f"child:{cname}/{cj}"
                if job is None:
                    missing.append(label)
                    required_checks.append({"name": label,
                                            "status": "missing"})
                    continue
                state = _check_job_state(str(job.get("status")))
                required_checks.append({"name": label, "status": state,
                                        "job_id": job.get("id"),
                                        "url": job.get("web_url")})
                if state == "failed":
                    failed.append({"name": label, "id": job.get("id"),
                                   "url": job.get("web_url")})
                elif state == "pending" and outcome == "passed":
                    outcome = "pending"
                elif state != "success" and outcome in ("passed", "pending"):
                    outcome = state
        if failed:
            outcome = "failed"
        elif missing:
            outcome = "missing_required_check"
        pipeline_url = (detail.get("web_url")
                        if isinstance(detail, dict) else None)
        check = _wrap(
            get, host, f"/projects/{proj}/merge_requests/{ident['iid']}")
        if (not isinstance(check, dict)
                or str(check.get("sha", "")).lower() != sha):
            new_sha = str(check.get("sha", "")) if isinstance(
                check, dict) else ""
            return _receipt(canonical, sha, pid, pipeline_url,
                            "changed_head", "head_sha_changed",
                            "the MR head moved during the observation; "
                            "prior evidence is invalidated",
                            required, required_checks, None,
                            None, new_sha=new_sha or None)
        failed_job = failed[0] if failed else None
        if outcome == "passed":
            reason = "gate_passed"
            error = None
        elif outcome == "failed":
            reason = "required_check_failed"
            error = "a required check failed"
        elif outcome == "missing_required_check":
            reason = "required_check_missing"
            error = ("a declared required check is absent on a terminal "
                     "parent; a green parent alone is never passed")
        elif outcome == "pending":
            reason = "checks_pending"
            error = "checks are still running"
        else:
            reason = f"required_check_{outcome}"
            error = f"a required check is {outcome}"
        return _receipt(canonical, sha, pid, pipeline_url, outcome, reason,
                        error, required, required_checks, failed_job,
                        missing or [])
    except GitlabUnavailable as error:
        return {"schema": "pipeline-observer-gate/1", "read_only": True,
                "mr_url": canonical, "sha": None, "pipeline_id": None,
                "pipeline_url": None, "outcome": "unavailable",
                "reason": "gitlab_unavailable",
                "error": str(error)[:300], "required_checks": [],
                "missing_required_checks": [],
                "failed_job": None}


def _receipt(mr_url: str, sha: str | None, pid: Any, purl: Any,
             outcome: str, reason: str, error: str | None,
             required: dict[str, Any],
             checks: list[dict[str, Any]],
             failed_job: dict[str, Any] | None,
             missing: list[str] | None,
             new_sha: str | None = None) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": "pipeline-observer-gate/1", "read_only": True,
        "mr_url": mr_url, "sha": sha, "pipeline_id": pid,
        "pipeline_url": purl, "outcome": outcome, "reason": reason,
        "required_checks": checks,
        "missing_required_checks": list(missing or []),
        "failed_job": failed_job,
    }
    if error is not None:
        receipt["error"] = error
    if new_sha is not None:
        receipt["new_sha"] = new_sha
        receipt["invalidated"] = True
    return receipt
