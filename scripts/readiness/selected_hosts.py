"""Target-host preflight and execution for the selected check.

Dell checks are local (cwd, executable, git HEAD) and run one child with no
shell.  Lenovo checks reuse the existing readiness environment model and the
existing signed queue: the local preflight reads a MAC-verified readiness
result and binds the descriptor to it, then exactly one signed job carries the
fixed preamble from ``selected_probe`` that re-verifies cwd/tool/revision on
the target host before running the check.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parent / "lan_bridge"
sys.path[:0] = [str(item) for item in (BRIDGE_DIR, HERE)
                if str(item) not in sys.path]

import check as readiness  # noqa: E402
import lan_bridge as bridge  # noqa: E402
import syntax  # noqa: E402
from selected_model import (INCOMPLETE_EXIT, MAX_COMMAND_CHARS, NOT_RUN_EXIT,  # noqa: E402
                            NOT_RUN_REASONS, PROBE_SCHEMA, Refusal,
                            read_object)
from selected_probe import PROGRAM as REMOTE_PROGRAM  # noqa: E402


def _confirmed(observed: str | None = None) -> dict:
    return {"state": "confirmed", "reason": "confirmed", "observed": observed}


def _refused(reason: str, observed: str | None = None) -> dict:
    return {"state": "refused", "reason": reason, "observed": observed}


def _git_head(git: str, repository: str) -> str | None:
    try:
        run = subprocess.run([git, "-C", repository, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=15,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = (run.stdout or "").strip().lower()
    if (run.returncode != 0 or len(value) != 40
            or any(char not in "0123456789abcdef" for char in value)):
        return None
    return value


def _tool_available(command: str) -> bool:
    path = Path(command)
    return path.is_file() if path.is_absolute() else shutil.which(command) is not None


def preflight_dell(desc: dict) -> dict:
    check = desc["check"]
    if not Path(check["cwd"]).is_dir():
        return _refused("cwd-missing")
    if not _tool_available(check["argv"][0]):
        return _refused("tool-missing")
    repository = desc["dell"]["revisionRepository"]
    if not Path(repository).is_dir():
        return _refused("revision-unconfirmed")
    observed = _git_head(desc["dell"]["git"], repository)
    if observed is None:
        return _refused("revision-unconfirmed")
    if observed != check["expectedRevision"]:
        return _refused("revision-stale", observed)
    return _confirmed(observed)


def remote_command(desc: dict) -> str:
    block = desc["lenovo"]
    payload = {"argv": desc["check"]["argv"], "cwd": desc["check"]["cwd"],
               "expectedRevision": desc["check"]["expectedRevision"],
               "timeoutSeconds": desc["check"]["timeoutSeconds"],
               "git": block["git"]}
    data = base64.b64encode(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    program = base64.b64encode(REMOTE_PROGRAM.encode("utf-8")).decode("ascii")
    code = f"import base64;exec(base64.b64decode('{program}'))"
    return subprocess.list2cmdline([block["python"], "-c", code, data])


def preflight_lenovo(desc: dict) -> tuple[dict, bytes | None]:
    block = desc["lenovo"]
    check = desc["check"]
    if not Path(block["queue"]).is_dir():
        return _refused("queue-missing"), None
    try:
        key = bridge.load_key(block["keyFile"], block["keyFormat"],
                              block["queue"])
    except bridge.BridgeError:
        return _refused("key-unusable"), None
    try:
        config = readiness.validate_config(read_object(Path(block["readinessConfig"])))
    except readiness.Refusal as error:
        raise Refusal(f"readiness configuration refused: {error}") from error
    if config["lenovo"]["python"] != block["python"]:
        return _refused("python-unproven"), key
    required = block["requiredComponents"]
    cwd_item = next((item for item in config["lenovo"]["components"]
                     if item["name"] == required["cwd"]), None)
    if (cwd_item is None or cwd_item["type"] != "directory"
            or cwd_item["path"] != check["cwd"]):
        return _refused("cwd-unproven"), key
    tool_item = next((item for item in config["lenovo"]["tools"]
                      if item["name"] == required["tool"]), None)
    if tool_item is None or tool_item["command"] != check["argv"][0]:
        return _refused("tool-unproven"), key
    try:
        readiness.validate_submitted_checklist(config, key,
                                               block["readinessJobId"])
    except (readiness.Refusal, bridge.JobNotFoundError):
        return _refused("evidence-unbound"), key
    wait = block["evidenceWaitSeconds"]
    try:
        payload = bridge.fetch_result(block["queue"], key,
                                      block["readinessJobId"],
                                      timeout_seconds=wait,
                                      poll_seconds=min(0.25, wait))
    except bridge.FetchTimeout:
        return _refused("worker-unproven"), key
    try:
        components = readiness.worker_components(payload, config)
    except readiness.Refusal as error:
        raise Refusal(f"readiness evidence refused: {error}") from error
    if any(item["state"] != "confirmed" for item in components):
        return _refused("worker-unproven"), key
    command = remote_command(desc)
    if len(command) > MAX_COMMAND_CHARS or not syntax.validate(command, "cmd").ok:
        return _refused("command-refused"), key
    return _confirmed(None), key


def execute_dell(desc: dict) -> dict:
    check = desc["check"]
    base = {"kind": "local-child", "jobId": None, "verified": True,
            "exitCode": None, "timedOut": False, "truncated": False,
            "observedHost": "dell", "observedRevision": None,
            "revisionState": None, "reason": "completed"}
    try:
        proc = subprocess.run(check["argv"], cwd=check["cwd"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=check["timeoutSeconds"], check=False)
    except subprocess.TimeoutExpired:
        return {**base, "state": "fail", "timedOut": True, "reason": "timeout"}
    except OSError:
        return {**base, "state": "not_run", "observedHost": None,
                "reason": "command-unavailable"}
    code = proc.returncode
    if not isinstance(code, int) or isinstance(code, bool) or code < 0:
        return {**base, "state": "fail", "reason": "abnormal-exit"}
    observed = _git_head(desc["dell"]["git"], desc["dell"]["revisionRepository"])
    revision = ("confirmed" if observed == check["expectedRevision"]
                else "stale" if observed else "unconfirmed")
    return {**base, "state": "run" if code == 0 else "fail", "exitCode": code,
            "observedRevision": observed, "revisionState": revision,
            "reason": "completed" if code == 0 else "nonzero-exit"}


def execute_lenovo(desc: dict, key: bytes) -> dict:
    block = desc["lenovo"]
    check = desc["check"]
    job_id = bridge.submit_job(
        block["queue"], key, remote_command(desc), check["cwd"],
        timeout_seconds=min(3600, check["timeoutSeconds"] + 60),
        max_output_bytes=check["maxOutputBytes"], dialect="cmd")
    try:
        payload = bridge.fetch_result(block["queue"], key, job_id,
                                      timeout_seconds=block["waitSeconds"],
                                      poll_seconds=block["pollSeconds"])
    except bridge.FetchTimeout:
        return {"kind": "signed-queue", "jobId": job_id, "verified": False,
                "state": "unknown", "exitCode": None, "timedOut": False,
                "truncated": False, "observedHost": None,
                "observedRevision": None, "revisionState": None,
                "reason": "no-verified-result"}
    return lenovo_result(job_id, payload)


def lenovo_result(job_id: str, payload: dict) -> dict:
    for field in ("timed_out", "output_truncated"):
        if field not in payload or type(payload[field]) is not bool:
            raise Refusal(f"verified worker result field {field!r} must be a boolean")
    base = {"kind": "signed-queue", "jobId": job_id, "verified": True,
            "exitCode": payload.get("exit_code"),
            "timedOut": payload["timed_out"],
            "truncated": payload["output_truncated"],
            "observedHost": "lenovo", "observedRevision": None,
            "revisionState": None, "reason": "unknown"}
    if payload["timed_out"] or payload["output_truncated"]:
        return {**base, "verified": False, "state": "unknown",
                "reason": "worker-timeout" if payload["timed_out"]
                else "output-truncated"}
    if payload.get("status") not in ("ok", "completed", "error"):
        return {**base, "verified": False, "state": "unknown",
                "reason": "unrecognized-status"}
    try:
        receipt = json.loads(payload.get("stdout", ""))
    except (TypeError, json.JSONDecodeError):
        return {**base, "state": "not_run", "exitCode": None,
                "reason": "worker-refused"}
    if not isinstance(receipt, dict) or receipt.get("schema") != PROBE_SCHEMA:
        return {**base, "state": "not_run", "exitCode": None,
                "reason": "worker-refused"}
    state = receipt.get("state")
    reason = receipt.get("reason")
    code = receipt.get("exitCode")
    observed = receipt.get("observedRevision")
    if observed is not None and (
            not isinstance(observed, str) or len(observed) != 40
            or any(char not in "0123456789abcdef" for char in observed)):
        return {**base, "state": "unknown", "reason": "unreadable-receipt"}
    base["observedRevision"] = observed
    base["revisionState"] = (
        "stale" if reason == "revision-stale"
        else "confirmed" if observed is not None else "unconfirmed")
    if state == "RUN":
        if (type(code) is not int or code < 0
                or payload.get("exit_code") != code):
            return {**base, "state": "unknown", "reason": "exit-disagreement"}
        return {**base, "state": "run" if code == 0 else "fail",
                "exitCode": code,
                "reason": "completed" if code == 0 else "nonzero-exit"}
    if state == "NOT_RUN":
        if payload.get("exit_code") != NOT_RUN_EXIT or code is not None:
            return {**base, "state": "unknown", "reason": "receipt-disagreement"}
        return {**base, "state": "not_run", "exitCode": None,
                "reason": reason if reason in NOT_RUN_REASONS else "worker-refused"}
    if state == "FAIL":
        if payload.get("exit_code") != INCOMPLETE_EXIT:
            return {**base, "state": "unknown", "reason": "receipt-disagreement"}
        return {**base, "state": "fail", "exitCode": None,
                "timedOut": reason == "timeout",
                "reason": reason if reason in ("timeout", "abnormal-exit")
                else "abnormal-exit"}
    return {**base, "state": "unknown", "reason": "unreadable-receipt"}
