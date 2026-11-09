"""Fixture double for the MR write seam (tests only, never production).

Implements the narrow ``publish`` surface the Lenovo publish driver consumes:
create the MR record for a source branch or update the existing one, recording
the pushed head SHA observed in the fixture bare origin. Records persist in
the fixture JSON state file; setting ``DELL_FIXTURE_FAIL_AFTER_WRITE=1``
persists the write and then raises, proving the driver's unknown-outcome path.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import gitlab_api


class PublicationError(Exception):
    """The simulated MR write failed or its response was lost."""


def _state_path() -> Path:
    path = os.environ.get("DELL_FIXTURE_STATE", "")
    if not path:
        raise PublicationError("DELL_FIXTURE_STATE is not set")
    return Path(path)


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError(f"unreadable fixture state: {error}")


def _save(state: dict) -> None:
    _state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _origin_head(source_branch: str) -> str:
    origin = os.environ.get("DELL_FIXTURE_ORIGIN", "")
    proc = subprocess.run(
        ["git", "--git-dir", origin, "rev-parse", "--verify",
         f"refs/heads/{source_branch}"],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PublicationError(f"fixture origin has no {source_branch}")
    return proc.stdout.strip().lower()


def _origin_target(target_branch: str) -> str:
    origin = os.environ.get("DELL_FIXTURE_ORIGIN", "")
    proc = subprocess.run(
        ["git", "--git-dir", origin, "rev-parse", "--verify",
         f"refs/heads/{target_branch}"],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PublicationError(f"fixture origin has no {target_branch}")
    return proc.stdout.strip().lower()


def publish(*, host: str, project: str, change_type: str, key: str,
            subject: str, description: str, desired_state: str,
            no_test_results: bool, source_branch: str, target_branch: str,
            mr_url: str | None, replace_description: bool) -> dict:
    """Create or update the Draft MR record for one source branch."""

    state = _load()
    if host != state.get("host") or project != state.get("project"):
        raise PublicationError(f"unknown fixture target: {host}/{project}")
    head_sha = _origin_head(source_branch)
    target_sha = _origin_target(target_branch)
    title = f"Draft: {change_type}({key}): {subject}"
    record = None
    if mr_url:
        for candidate in state.get("mrs", {}).values():
            if candidate.get("web_url") == mr_url:
                record = candidate
                break
        if record is None:
            raise PublicationError(f"unknown fixture MR: {mr_url}")
        record["title"] = title
        if replace_description:
            record["description"] = description
        else:
            record["description"] = (record.get("description") or "") + "\n" + description
        record["head_sha"] = head_sha
        record["target_sha"] = target_sha
    else:
        iid = int(state.get("nextIid", 1))
        state["nextIid"] = iid + 1
        record = {
            "iid": iid,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "draft": desired_state == "draft",
            "head_sha": head_sha,
            "target_sha": target_sha,
            "web_url": gitlab_api.merge_request_url(host, project, iid),
        }
        state.setdefault("mrs", {})[str(iid)] = record
    _save(state)
    if os.environ.get("DELL_FIXTURE_FAIL_AFTER_WRITE") == "1":
        raise PublicationError("simulated write loss after persist")
    return {"mr_url": record["web_url"], "iid": record["iid"],
            "draft": bool(record.get("draft", True))}
