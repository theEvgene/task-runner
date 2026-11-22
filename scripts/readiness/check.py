#!/usr/bin/env python3
"""Check one Dell-to-Lenovo scenario through the canonical signed queue.

The initial invocation performs local checks, publishes exactly one read-only
Lenovo probe, and fetches its MAC-bound result.  A timed-out invocation is
resumed with ``--job-id``; the resume path never publishes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parent / "lan_bridge"
WORKER_DIR = HERE.parent / "lenovo_worker"
sys.path[:0] = [str(item) for item in (BRIDGE_DIR, WORKER_DIR, HERE)
                if str(item) not in sys.path]

import lan_bridge as bridge  # noqa: E402
import installer_env  # noqa: E402
import reasons  # noqa: E402  (single owner of local refusal reasons)
import wait as waitmod  # noqa: E402  (single owner of job-state wording)
# Only the Lenovo-side probe program text lives in ``remote_probe``; this
# Dell-side orchestration keeps one module per job, inside the token guard.
from remote_probe import PROGRAM as REMOTE_PROBE  # noqa: E402

SCHEMA = "lan-readiness-config/1"
REPORT_SCHEMA = "lan-readiness-report/1"
PROBE_SCHEMAS = ("lenovo-readiness-probe/1", "lenovo-readiness-probe/2")
SCENARIOS = ("lan-bridge", "dell-lan")


class Refusal(Exception):
    pass


def _object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal("configuration is unreadable or invalid JSON") from error
    if not isinstance(value, dict):
        raise Refusal("configuration must be a JSON object")
    return value


def _string(obj: dict, name: str) -> str:
    value = obj.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Refusal(f"configuration field {name!r} is required")
    return value


def _check_definition(item: object) -> dict:
    if not isinstance(item, dict):
        raise Refusal("every component check must be an object")
    name = _string(item, "name")
    kind = item.get("type")
    if kind not in ("file", "directory"):
        raise Refusal(f"component {name!r} has unsupported type")
    path = _string(item, "path")
    result = {"name": name, "type": kind, "path": path}
    digest = item.get("sha256")
    if digest is not None:
        if kind != "file" or not isinstance(digest, str) or len(digest) != 64 \
                or any(char not in "0123456789abcdef" for char in digest):
            raise Refusal(f"component {name!r} has invalid sha256")
        result["sha256"] = digest
    return result


def _tool_definition(item: object) -> dict:
    if not isinstance(item, dict):
        raise Refusal("every tool check must be an object")
    return {"name": _string(item, "name"),
            "command": _string(item, "command")}


def validate_config(raw: dict) -> dict:
    if raw.get("schema") != SCHEMA:
        raise Refusal(f"configuration schema must be {SCHEMA}")
    scenario = raw.get("scenario")
    if scenario not in SCENARIOS:
        raise Refusal("scenario must be lan-bridge or dell-lan")
    local = raw.get("local", {})
    lenovo = raw.get("lenovo")
    if not isinstance(local, dict) or not isinstance(lenovo, dict):
        raise Refusal("local and lenovo configuration objects are required")
    environment = installer_env.validate_environment(lenovo)
    config = {
        "scenario": scenario,
        "queue": _string(raw, "queue"),
        "keyFile": _string(raw, "keyFile"),
        "keyFormat": raw.get("keyFormat", "raw"),
        "local": {
            "components": [_check_definition(x) for x in local.get("components", [])],
            "tools": [_tool_definition(x) for x in local.get("tools", [])],
        },
        "lenovo": {
            "python": _string(lenovo, "python"),
            "cwd": _string(lenovo, "cwd"),
            "components": [_check_definition(x) for x in lenovo.get("components", [])],
            "tools": [_tool_definition(x) for x in lenovo.get("tools", [])],
        },
    }
    if config["keyFormat"] not in ("raw", "base64"):
        raise Refusal("keyFormat must be raw or base64")
    if environment is not None:
        config["lenovo"]["environment"] = environment
    if "revision" in (lenovo or {}):
        # Removed blocker: the stale HEAD-vs-configured Task Runner pin no
        # longer gates LAN operation. Old configs may still carry the field;
        # it is ignored and never probed.
        pass
    if scenario == "dell-lan":
        if not config["local"]["components"] or not config["lenovo"]["components"]:
            raise Refusal("dell-lan requires explicit local and Lenovo components")
    for side in ("local", "lenovo"):
        names = [item["name"] for item in config[side]["components"]]
        names.extend(item["name"] for item in config[side]["tools"])
        if side == "lenovo":
            names.extend(installer_env.all_names(
                config["lenovo"].get("environment")))
        if len(names) != len(set(names)):
            raise Refusal(f"{side} component names must be unique")
    return config


def _component(name: str, location: str, state: str, reason: str,
               action: str) -> dict:
    return {"name": name, "location": location, "state": state,
            "reason": reason, "nextAction": action}


def local_checks(config: dict) -> list[dict]:
    results = []
    queue = Path(config["queue"])
    results.append(_component(
        "signed-queue", "dell", "confirmed" if queue.is_dir() else "missing",
        "queue directory is available" if queue.is_dir() else "queue directory is absent",
        "none" if queue.is_dir() else "restore the owner-provided queue mount"))
    try:
        bridge.load_key(config["keyFile"], config["keyFormat"], config["queue"])
        results.append(_component("queue-key", "dell", "confirmed",
                                  "key is readable and valid", "none"))
    except bridge.BridgeError:
        results.append(_component("queue-key", "dell", "missing",
                                  "key is unavailable or invalid",
                                  "restore the owner-provided key outside the queue and checkout"))
    results.append(_component("python", "dell", "confirmed",
                              "current Python is running", "none"))
    for item in config["local"]["components"]:
        path = Path(item["path"])
        present = path.is_file() if item["type"] == "file" else path.is_dir()
        if not present:
            results.append(_component(item["name"], "dell", "missing",
                                      f"{item['type']} is absent",
                                      f"provide the required {item['name']}"))
            continue
        digest = item.get("sha256")
        if digest:
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                results.append(_component(item["name"], "dell", "unconfirmed",
                                          "file could not be read", "restore readable access"))
                continue
            if actual != digest:
                results.append(_component(item["name"], "dell", "stale",
                                          "digest differs from the selected revision",
                                          f"update {item['name']}"))
                continue
        results.append(_component(item["name"], "dell", "confirmed",
                                  f"{item['type']} is present" +
                                  (" at the expected digest" if digest else ""), "none"))
    for item in config["local"]["tools"]:
        command = item["command"]
        found = Path(command).is_file() if Path(command).is_absolute() else shutil.which(command)
        results.append(_component(item["name"], "dell",
                                  "confirmed" if found else "missing",
                                  "executable is available" if found else "executable is absent",
                                  "none" if found else "install or select the required executable"))
    return results


def _remote_command(config: dict) -> str:
    probe = dict(config["lenovo"])
    probe["scenario"] = config["scenario"]
    probe.pop("python", None)
    probe.pop("cwd", None)
    data = base64.b64encode(json.dumps(
        probe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
    program = base64.b64encode(REMOTE_PROBE.encode("utf-8")).decode("ascii")
    code = f"import base64;exec(base64.b64decode('{program}'))"
    return subprocess.list2cmdline([config["lenovo"]["python"], "-c", code, data])


def _report(config: dict, job_id: str, components: list[dict],
            outcome: str | None = None, status: dict | None = None) -> dict:
    states = [item["state"] for item in components]
    final = outcome or ("ready" if states and all(x == "confirmed" for x in states)
                        else "not-ready")
    failing = next((item for item in components if item["state"] != "confirmed"), None)
    if final == "ready":
        action = "proceed with the selected scenario"
    elif failing is not None:
        action = failing["nextAction"]
    elif status is not None:
        # The job-state wording lives in wait.py only; never restated here.
        action = status["nextAction"]
    else:
        action = "stop and report the readiness blocker to the owner"
    report = {"schema": REPORT_SCHEMA, "scenario": config["scenario"],
              "jobId": job_id, "outcome": final,
              "components": components, "nextAction": action}
    if status is not None:
        report["status"] = status
    return report


def _validate_worker(payload: dict, config: dict) -> list[dict]:
    for field in ("timed_out", "output_truncated"):
        if field not in payload or type(payload[field]) is not bool:
            raise Refusal(f"verified worker result field {field!r} must be a boolean")
    if payload["timed_out"]:
        return [_component("lenovo-probe", "lenovo", "unconfirmed",
                           "worker reported a terminal timeout",
                           "fix the worker limit and start a new readiness check")]
    if payload["output_truncated"]:
        return [_component("lenovo-probe", "lenovo", "unconfirmed",
                           "worker output was truncated",
                           "increase the output bound and start a new readiness check")]
    exit_code = payload.get("exit_code")
    if (payload.get("status") not in ("ok", "completed")
            or type(exit_code) is not int or exit_code != 0):
        return [_component("lenovo-probe", "lenovo", "unconfirmed",
                           "worker did not complete successfully",
                           "repair the worker-side probe and start a new readiness check")]
    try:
        body = json.loads(payload.get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as error:
        raise Refusal("verified worker result has invalid readiness JSON") from error
    if not isinstance(body, dict) or body.get("schema") not in PROBE_SCHEMAS \
            or body.get("scenario") != config["scenario"] \
            or not isinstance(body.get("components"), list):
        raise Refusal("verified worker result is not bound to the selected readiness scenario")
    cmd_defs, rt_defs = installer_env.env_definitions(config)
    allowed = {item["name"] for item in config["lenovo"]["components"]}
    allowed.update(item["name"] for item in config["lenovo"]["tools"])
    allowed.update(cmd_defs)
    allowed.update(rt_defs)
    raw_components = body["components"]
    if any(not isinstance(item, dict) or item.get("name") not in allowed
           or item.get("state") not in ("confirmed", "missing", "stale", "unconfirmed")
           for item in raw_components):
        raise Refusal("verified worker result contains an unexpected component")
    expected_count = (len(config["lenovo"]["components"])
                      + len(config["lenovo"]["tools"])
                      + len(cmd_defs) + len(rt_defs))
    if (len(raw_components) != expected_count
            or len({x["name"] for x in raw_components}) != expected_count):
        raise Refusal("verified worker result omitted or duplicated a required component")
    # Do not relay arbitrary worker text.  The fixed probe emits only these
    # states, and the Dell report reconstructs bounded, path-free messages.
    state_messages = {
        "confirmed": ("required condition is confirmed", "none"),
        "missing": ("required condition is absent", "provide the required component"),
        "stale": ("required component differs from the selected revision",
                  "update the required component"),
        "unconfirmed": ("required condition could not be confirmed",
                        "repair or separately verify the required condition"),
    }
    definitions = {item["name"]: item for item in config["lenovo"]["components"]}
    components = []
    for item in raw_components:
        resolved = installer_env.check_item(item, cmd_defs, rt_defs)
        if resolved is not None:
            entry = _component(item["name"], "lenovo", item["state"],
                               resolved[0], resolved[1])
        else:
            entry = _component(item["name"], "lenovo", item["state"],
                               state_messages[item["state"]][0],
                               state_messages[item["state"]][1])
            definition = definitions.get(item["name"])
            if definition is not None and definition["type"] == "directory":
                entry.update(installer_env.revision_evidence(
                    body["schema"], item, definition))
        components.append(entry)
    return [_component("lenovo-worker", "lenovo", "confirmed",
                       "MAC-bound worker result verified", "none"), *components]


def _validate_submitted_checklist(config: dict, key: bytes, job_id: str) -> None:
    """Bind same-ID recovery to the exact checklist submitted originally."""
    raw = bridge.read(Path(config["queue"]) / "jobs", job_id)
    payload = bridge.open_envelope(raw, key, "job", job_id)
    try:
        canonical_payload = bridge.build_job_payload(
            payload.get("id"), payload.get("command"),
            payload.get("working_directory"),
            timeout_seconds=payload.get("timeout_seconds"),
            max_output_bytes=payload.get("max_output_bytes"),
            return_files=payload.get("return_files"),
            dialect=payload.get("dialect"),
            # A readiness job signed before the 600 s cap carries a legacy
            # timeout up to 3600; the worker clamps it. Same-id recovery must
            # still bind that exact signed job instead of resubmitting.
            allow_legacy_timeout=True,
        )
    except (TypeError, ValueError) as error:
        raise Refusal(
            "existing job is not bound to the exact selected readiness checklist"
        ) from error
    expected_shape = {
        "kind", "id", "command", "working_directory", "timeout_seconds",
        "max_output_bytes", "return_files", "dialect",
    }
    if (payload != canonical_payload
            or set(payload) != expected_shape
            or payload.get("command") != _remote_command(config)
            or payload.get("working_directory") != config["lenovo"]["cwd"]
            or payload.get("max_output_bytes") != 65536
            or payload.get("return_files") != []
            or payload.get("dialect") != "cmd"):
        raise Refusal(
            "existing job is not bound to the exact selected readiness checklist")


def probe_command(config: dict) -> str:
    """Public handle: the exact signed readiness probe command."""
    return _remote_command(config)


def validate_submitted_checklist(config: dict, key: bytes, job_id: str) -> None:
    """Public handle: bind a same-ID readiness job to this configuration."""
    _validate_submitted_checklist(config, key, job_id)


def worker_components(payload: dict, config: dict) -> list[dict]:
    """Public handle: validate one MAC-bound readiness result."""
    return _validate_worker(payload, config)


def _local_observations(config: dict, components: list[dict]) -> dict:
    state = {item["name"]: item["state"] for item in components}

    def confirmed(items: list[dict]) -> bool:
        return all(state.get(item["name"]) == "confirmed" for item in items)

    return {"queuePresent": state.get("signed-queue") == "confirmed",
            "keyPresent": state.get("queue-key") == "confirmed",
            "componentPresent": confirmed(config["local"]["components"]),
            "toolPresent": confirmed(config["local"]["tools"])}


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    repo_root = bridge.find_repo_root()
    if repo_root is not None:
        try:
            config_path.relative_to(repo_root.resolve())
        except ValueError:
            pass
        else:
            raise Refusal("configuration must stay outside the checkout")
    config = validate_config(_object(config_path))
    local = local_checks(config)
    if any(item["state"] != "confirmed" for item in local):
        # An uncertain preflight never submits a signed job.
        observations = _local_observations(config, local)
        described = reasons.describe(reasons.classify_local(observations)
                                     or reasons.Refusal("preflight-incomplete",
                                                        observations=observations))
        report = _report(config, "not-submitted", local)
        report["reason"] = described["reason"]
        report["nextAction"] = described["nextAction"]
        print(json.dumps(report, ensure_ascii=False))
        return 1
    key = bridge.load_key(config["keyFile"], config["keyFormat"], config["queue"])
    if args.job_id:
        job_id = args.job_id
        _validate_submitted_checklist(config, key, job_id)
    else:
        if not args.authorize_lenovo_check:
            raise Refusal("submission needs --authorize-lenovo-check for this read-only run")
        job_id = bridge.submit_job(
            config["queue"], key, _remote_command(config),
            config["lenovo"]["cwd"], timeout_seconds=int(args.worker_timeout),
            max_output_bytes=65536, dialect="cmd")
    try:
        payload = bridge.fetch_result(config["queue"], key, job_id,
                                      timeout_seconds=float(args.wait),
                                      poll_seconds=float(args.poll))
    except bridge.FetchTimeout:
        # The timeout report carries the one verified job readback, so its
        # overall outcome is that job state (waiting/unknown/refused), never a
        # second, contradicting projection.  `reason` stays a preflight field:
        # job-state wording lives in wait.py only and is never restated here.
        status = waitmod.status_for_absence(config["queue"], key, job_id)
        report = _report(config, job_id, local, status["outcome"], status=status)
        print(json.dumps(report, ensure_ascii=False))
        if report["outcome"] in (waitmod.WAITING, waitmod.UNKNOWN):
            return 3
        return 2
    components = [*local, *_validate_worker(payload, config)]
    if config["scenario"] == "dell-lan":
        components.append(_component(
            "corporate-access", "lenovo", "unconfirmed",
            "filesystem readiness does not prove credentials or service availability",
            "perform the separately authorized live corporate preflight on Lenovo"))
    report = _report(config, job_id, components,
                     status=waitmod.status_from_result(payload))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["outcome"] == "ready" else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Check a selected Dell-to-Lenovo scenario without changing it")
    result.add_argument("--config", required=True,
                        help="owner-provided JSON outside the checkout")
    result.add_argument("--authorize-lenovo-check", action="store_true",
                        help="authorize this one read-only Lenovo probe")
    result.add_argument("--job-id",
                        help="retrieve this existing id; never submit another job")
    result.add_argument("--wait", type=float, default=240.0)
    result.add_argument("--poll", type=float, default=30.0)
    result.add_argument("--worker-timeout", type=int, default=60)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (Refusal, bridge.BridgeError, ValueError, TypeError) as error:
        print(f"readiness refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
