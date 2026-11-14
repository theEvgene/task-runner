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
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import lan_bridge as bridge  # noqa: E402

SCHEMA = "lan-readiness-config/1"
REPORT_SCHEMA = "lan-readiness-report/1"
SCENARIOS = ("lan-bridge", "dell-lan")

# Runs only on Lenovo.  The program performs local filesystem/process lookup
# and git object-database reads.  It has no network operation and writes no
# file.  Its input contains paths, but its JSON output contains component
# names and states only.
REMOTE_PROBE = r'''import hashlib,json,shutil,subprocess,sys
from pathlib import Path
cfg=json.loads(__import__("base64").b64decode(sys.argv[1]).decode("utf-8"))
out=[]
def add(name,state,reason,next_action):
 out.append({"name":name,"location":"lenovo","state":state,"reason":reason,"nextAction":next_action})
def path_check(item):
 p=Path(item["path"]); kind=item["type"]
 ok=p.is_file() if kind=="file" else p.is_dir()
 if not ok:
  add(item["name"],"missing",kind+" is absent","provide the required "+item["name"]); return
 expected=item.get("sha256")
 if expected:
  try: actual=hashlib.sha256(p.read_bytes()).hexdigest()
  except OSError:
   add(item["name"],"unconfirmed","file could not be read","restore readable access"); return
  if actual!=expected:
   add(item["name"],"stale","digest differs from the selected revision","update "+item["name"]); return
 add(item["name"],"confirmed",kind+" is present"+(" at the expected digest" if expected else ""),"none")
for item in cfg.get("components",[]): path_check(item)
for item in cfg.get("tools",[]):
 command=item["command"]
 found=(Path(command).is_file() if Path(command).is_absolute() else shutil.which(command) is not None)
 add(item["name"],"confirmed" if found else "missing","executable is available" if found else "executable is absent","none" if found else "install or select the required executable")
revision=cfg.get("revision")
if revision:
 try:
  run=subprocess.run([cfg["git"],"-C",revision["repository"],"rev-parse","HEAD"],capture_output=True,text=True,timeout=15,check=False)
  actual=run.stdout.strip().lower()
  if run.returncode!=0 or len(actual)!=40: add(revision["name"],"unconfirmed","revision could not be read","repair the local checkout")
  elif actual!=revision["expected"]: add(revision["name"],"stale","checkout revision differs from expected","update the Lenovo checkout")
  else: add(revision["name"],"confirmed","checkout is at the expected revision","none")
 except Exception: add(revision["name"],"unconfirmed","revision probe failed","repair the local checkout")
print(json.dumps({"schema":"lenovo-readiness-probe/1","scenario":cfg["scenario"],"components":out},separators=(",",":")))
'''


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
    revision = lenovo.get("revision")
    if revision is not None:
        if not isinstance(revision, dict):
            raise Refusal("lenovo revision must be an object")
        expected = _string(revision, "expected").lower()
        if len(expected) != 40 or any(c not in "0123456789abcdef" for c in expected):
            raise Refusal("lenovo revision expected value must be 40 hex")
        config["lenovo"]["revision"] = {
            "name": _string(revision, "name"),
            "repository": _string(revision, "repository"),
            "expected": expected,
        }
        config["lenovo"]["git"] = _string(lenovo, "git")
    if scenario == "dell-lan":
        if not config["local"]["components"] or not config["lenovo"]["components"]:
            raise Refusal("dell-lan requires explicit local and Lenovo components")
        if revision is None:
            raise Refusal("dell-lan requires the expected Lenovo checkout revision")
    for side in ("local", "lenovo"):
        names = [item["name"] for item in config[side]["components"]]
        names.extend(item["name"] for item in config[side]["tools"])
        if side == "lenovo" and "revision" in config[side]:
            names.append(config[side]["revision"]["name"])
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
            outcome: str | None = None) -> dict:
    states = [item["state"] for item in components]
    final = outcome or ("ready" if states and all(x == "confirmed" for x in states)
                        else "not-ready")
    failing = next((item for item in components if item["state"] != "confirmed"), None)
    action = "proceed with the selected scenario" if final == "ready" else (
        failing["nextAction"] if failing else
        f"re-read the SAME id {job_id}; never resubmit while outcome is unknown")
    return {"schema": REPORT_SCHEMA, "scenario": config["scenario"],
            "jobId": job_id, "outcome": final,
            "components": components, "nextAction": action}


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
    if not isinstance(body, dict) or body.get("schema") != "lenovo-readiness-probe/1" \
            or body.get("scenario") != config["scenario"] \
            or not isinstance(body.get("components"), list):
        raise Refusal("verified worker result is not bound to the selected readiness scenario")
    allowed = {item["name"] for item in config["lenovo"]["components"]}
    allowed.update(item["name"] for item in config["lenovo"]["tools"])
    if "revision" in config["lenovo"]:
        allowed.add(config["lenovo"]["revision"]["name"])
    raw_components = body["components"]
    if any(not isinstance(item, dict) or item.get("name") not in allowed
           or item.get("state") not in ("confirmed", "missing", "stale", "unconfirmed")
           for item in raw_components):
        raise Refusal("verified worker result contains an unexpected component")
    expected_count = (len(config["lenovo"]["components"])
                      + len(config["lenovo"]["tools"])
                      + (1 if "revision" in config["lenovo"] else 0))
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
    components = [
        _component(item["name"], "lenovo", item["state"],
                   state_messages[item["state"]][0],
                   state_messages[item["state"]][1])
        for item in raw_components
    ]
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
        print(json.dumps(_report(config, "not-submitted", local), ensure_ascii=False))
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
        report = _report(config, job_id, local, "unknown")
        report["nextAction"] = (f"re-run with --job-id {job_id}; "
                                "never resubmit while outcome is unknown")
        print(json.dumps(report, ensure_ascii=False))
        return 3
    components = [*local, *_validate_worker(payload, config)]
    if config["scenario"] == "dell-lan":
        components.append(_component(
            "corporate-access", "lenovo", "unconfirmed",
            "filesystem readiness does not prove credentials or service availability",
            "perform the separately authorized live corporate preflight on Lenovo"))
    report = _report(config, job_id, components)
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
