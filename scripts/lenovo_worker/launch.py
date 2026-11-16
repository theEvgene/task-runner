"""One Windows shortcut entry for the Lenovo side (stdlib only).

After one-time local setup, a single run: connects the existing SMB share
with credentials from a local file (never in args/log/output), verifies the
existing queue/key/share, then starts the worker (sibling ``worker.py``)
and the cleaner (sibling ``cleaner.py``) as detached processes.

Refuses without side effects when: the launcher cwd (or a configured
``workerCwd``) is a UNC path, a mapped network drive, or cannot be proven
local, config/password missing, SMB unreachable or credentials wrong, queue or
key unusable, the worker delivery is missing, trash/state misplaced, a pid
file is corrupt/foreign/unprovable or a live pid runs under a changed
command/binding, another launch holds the lock, or a second launch races.
The SMB ``unc`` value is the only allowed UNC path: it names the share, never
a working directory to run in.

Every refusal carries one reason code and one bounded next action; the receipt
reports the worker/cleaner launch only (``scope``/``jobOutcome``) and never
claims that any job ran or completed. Printed text is redacted, so no
credential, host name, or private absolute path leaves the launcher.
A provably dead pid (including a legacy record or a changed config under a
dead pid) is a safe reboot recovery: under the held launch lock the stale
file is removed and the role is started fresh. Both roles are inspected
before either is spawned, so an ambiguous role anywhere starts nothing and
deletes nothing. Never creates the queue,
never writes jobs or results, never kills or replaces a running process, never
prints secrets.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LAUNCH_SCHEMA = "lenovo-launch/1"
HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))
import proc  # noqa: E402  (sibling helper, same delivery)
import reasons  # noqa: E402  (single owner of refusal reasons)
import share_probe  # noqa: E402  (bounded SMB probe, observations only)


def _load_bridge():
    path = HERE.parent / "lan_bridge" / "lan_bridge.py"
    if not path.is_file():
        override = os.environ.get("DELL_LAN_BRIDGE_DIR")
        if override:
            path = Path(override) / "lan_bridge.py"
    if not path.is_file():
        raise reasons.Refusal("client-missing",
                              "bridge client not found (scripts/lan_bridge/lan_bridge.py)")
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("lenovo_launch_bridge", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["lenovo_launch_bridge"] = module
    spec.loader.exec_module(module)
    return module


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _abs(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise reasons.Refusal("config-invalid", f"{name} must be an absolute path")
    return path


def _load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise reasons.Refusal("config-unreadable") from error
    if not isinstance(data, dict):
        raise reasons.Refusal("config-invalid", "config must be a JSON object")
    return data


def _norm(path: Path | str) -> str:
    """Lexical absolute form for layout checks (no existence required)."""

    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _within_or_equal(child: Path | str, parent: Path | str) -> bool:
    child_n, parent_n = _norm(child), _norm(parent)
    return child_n == parent_n or child_n.startswith(parent_n + os.sep)


def _check_layout(queue: Path, shared: Path, trash: Path, state: Path) -> None:
    """Refuse overlapping placements BEFORE any directory is created.

    ``trash``/``state`` must live outside the queue and the shared folder,
    so ``mkdir(parents=True)`` can never materialize queue parents and the
    cleaner can never treat state as a cleaning target.
    """

    if queue == shared:
        raise reasons.Refusal("layout-overlap", "queueDir and sharedDir must differ")
    for label, first, second, second_label in (
            ("trashDir", trash, queue, "queueDir"),
            ("trashDir", trash, shared, "sharedDir"),
            ("stateDir", state, queue, "queueDir"),
            ("stateDir", state, shared, "sharedDir"),
            ("trashDir", trash, state, "stateDir")):
        if _within_or_equal(first, second):
            raise reasons.Refusal("layout-overlap",
                                  f"{label} must be outside {second_label}")


LOCK_NAME = ".launch.lock"
LOCK_STALE_S = 60.0


def _lock_wait_s() -> float:
    try:
        wait = float(os.environ.get("TASK_RUNNER_LAUNCH_LOCK_WAIT_S", "20"))
    except ValueError:
        return 20.0
    return min(max(wait, 0.0), 120.0)


def _lock_owner_dead(lock: Path) -> bool:
    try:
        data = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("pid"), int):
        return not proc.is_alive(int(data["pid"]))
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return True  # vanished mid-check: let the caller retry
    return age > LOCK_STALE_S


def _acquire_launch_lock(state_dir: Path) -> Path:
    """Hold an exclusive launch lock BEFORE any process is spawned.

    A second launcher waits (bounded) and then adopts the recorded pids
    instead of spawning duplicates. A lock whose owner is provably gone
    is taken over; anything else refuses without touching any pid file.
    """

    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / LOCK_NAME
    deadline = time.monotonic() + _lock_wait_s()
    while True:
        try:
            lock.mkdir()
            try:
                (lock / "owner.json").write_text(
                    json.dumps({"pid": os.getpid(), "started": _now()}),
                    encoding="utf-8")
            except OSError:
                pass
            return lock
        except FileExistsError:
            pass
        if _lock_owner_dead(lock):
            shutil.rmtree(lock, ignore_errors=True)
            continue
        if time.monotonic() >= deadline:
            raise reasons.Refusal("launch-busy",
                                  "another launch is in progress; re-run to adopt its pids")
        time.sleep(0.2)


def _release_launch_lock(lock: Path) -> None:
    shutil.rmtree(lock, ignore_errors=True)


def _inspect_role(state_dir: Path, role: str, command: list[str],
                  marker: str, binding: dict) -> tuple[str, int | None]:
    """Classify a role WITHOUT spawning: ("running", pid) or ("absent", None).

    A dead pid is a safe reboot recovery: it proves no live owner, so the
    caller may delete the file under the held launch lock and start fresh.
    Anything that could still be live (corrupt, foreign command line,
    unprovable command line, live pid under a changed config/binding, or a
    live legacy-unbound record) raises ValueError (ambiguous). Callers must
    refuse before spawning any role when any inspection is ambiguous, and
    must delete nothing when any role is ambiguous.
    """

    pid_file = state_dir / f"{role}.pid"
    try:
        existing = proc.read_pid_file(pid_file)
    except (ValueError, OSError) as error:
        raise reasons.Refusal("role-ambiguous",
                              f"ambiguous {role} state (corrupt pid file); verify no "
                              f"{role} runs, then remove {role}.pid in stateDir") from error
    if existing is None:
        return ("absent", None)
    pid = existing.get("pid")
    pid_alive = isinstance(pid, int) and proc.is_alive(pid)
    if "command" not in existing or "binding" not in existing:
        # Legacy record without identity: only a provably dead pid is
        # recoverable; a live one could be anything.
        if not pid_alive:
            return ("absent", None)
        raise reasons.Refusal("role-ambiguous",
                              f"ambiguous {role} state (unrecognized pid format, "
                              f"possibly written by an older launcher); verify no {role} "
                              f"runs, then remove {role}.pid in stateDir")
    if (existing.get("marker") != marker
            or existing.get("command") != [str(p) for p in command]
            or existing.get("binding") != binding):
        # Config changed under a recorded process: a live pid could be the
        # old owner still running, so refuse; a dead pid proves no live
        # owner, so the new config may take over under the launch lock.
        if not pid_alive:
            return ("absent", None)
        raise reasons.Refusal("role-ambiguous",
                              f"ambiguous {role} state (config changed under a recorded "
                              f"process); verify, then remove {role}.pid in stateDir")
    if not pid_alive:
        return ("absent", None)
    line = proc.cmdline(pid) or ""
    if not line:
        # Alive but command line unprovable (permissions/race): never adopt
        # and never overwrite; a rebooted pid is dead, so it never lands here.
        raise reasons.Refusal("role-ambiguous",
                              f"ambiguous {role} state (pid {pid} runs but its command "
                              f"line cannot be proven); verify no {role} runs, "
                              f"then remove {role}.pid in stateDir")
    if (marker and marker not in line) or any(
            str(token) in " ".join(str(p) for p in command)
            and str(token) not in line
            for token in binding.values() if token):
        raise reasons.Refusal("role-ambiguous",
                              f"ambiguous {role} state (pid {pid} runs foreign "
                              f"command); refusing to touch it")
    return ("running", pid)


def _ensure_role(state_dir: Path, role: str, command: list[str],
                 marker: str, binding: dict, cwd: Path | None = None) -> dict:
    """Start the role once, or report the running owner. Never kill/replace."""

    command = [str(p) for p in command]
    state, pid = _inspect_role(state_dir, role, command, marker, binding)
    if state == "running":
        return {"role": role, "state": "already-running", "pid": pid}
    pid_file = state_dir / f"{role}.pid"
    if pid_file.exists():
        # Stale reboot record classified absent above: remove under the held
        # launch lock so the exclusive create below cannot hit a leftover.
        try:
            pid_file.unlink()
        except OSError as error:
            raise reasons.Refusal("role-ambiguous",
                                  f"ambiguous {role} state (stale pid file cannot be "
                                  f"removed); verify, then remove {role}.pid in "
                                  f"stateDir") from error
    log_file = state_dir / f"{role}.log"
    new_pid = proc.spawn_detached(command, log_file, cwd)
    try:
        proc.write_pid_file(pid_file, new_pid, marker, _now(),
                            {"command": command, "binding": binding})
    except FileExistsError as error:
        raise reasons.Refusal("role-ambiguous",
                              f"ambiguous {role} state (pid file appeared mid-start); "
                              "re-run to adopt it") from error
    if not proc.is_alive(new_pid):
        raise reasons.Refusal("role-start-failed",
                              f"{role} exited at once; read {role}.log in stateDir")
    return {"role": role, "state": "started", "pid": new_pid}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="launch.py", description="Lenovo one-shortcut launcher")
    parser.add_argument("--config", required=True, help="absolute path of the local JSON config")
    return parser


def _launcher_cwd() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None


def _refuse(summary: dict, refusal: reasons.Refusal) -> int:
    report = reasons.describe(refusal)
    summary.update({"outcome": "refused", "reason": report["reason"],
                    "observed": report["state"], "detail": report["detail"],
                    "nextAction": report["nextAction"], "evidence": report["evidence"]})
    print(f"launch refused: {report['detail']}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False))
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary: dict = {"schema": LAUNCH_SCHEMA, "scope": "worker-launch",
                     "jobOutcome": "not-evaluated", "at": _now(), "roles": []}
    try:
        config = _load_config(Path(args.config))
        if any(name not in config for name in ("queueDir", "sharedDir", "keyFile")):
            raise reasons.Refusal("config-invalid", "config field is missing")
        queue = _abs(str(config["queueDir"]), "queueDir")
        shared = _abs(str(config["sharedDir"]), "sharedDir")
        key_file = _abs(str(config["keyFile"]), "keyFile")
        trash = _abs(str(config.get("trashDir", "") or ""), "trashDir")
        state = _abs(str(config.get("stateDir", "") or ""), "stateDir")
        # A remote/unprovable cwd refuses before any layout, SMB, lock, or spawn
        # work: the SMB ``unc`` names a share, never a working directory.
        worker_cwd: Path | None = None
        observations = {"cwd": reasons.cwd_safety(_launcher_cwd())}
        if config.get("workerCwd"):
            worker_cwd = _abs(str(config["workerCwd"]), "workerCwd")
            observations["workerCwd"] = reasons.cwd_safety(worker_cwd)
        verdict = reasons.classify_cwd(observations)
        if verdict is not None:
            raise verdict
        _check_layout(queue, shared, trash, state)
        observations.update(share_probe.connect_share(config.get("smb") or {}))
        if observations.get("shareVisible") is True and not shared.is_dir():
            observations["shareVisible"] = False
        observations["queuePresent"] = (
            queue.is_dir() and (queue / "jobs").is_dir() and (queue / "results").is_dir())
        python = str(config.get("python") or proc.python_executable())
        worker_dir = Path(str(config.get("workerDir") or str(HERE)))
        worker_cmd = [str(p) for p in config.get("workerCommand", [])] or [
            python, str(worker_dir / "worker.py"), "--queue", str(queue),
            "--key-file", str(key_file), "--key-format", str(config.get("keyFormat", "raw")),
            "--shared", str(shared), "--poll", str(config.get("workerPoll", 5))]
        cleaner_cmd = [str(p) for p in config.get("cleanerCommand", [])] or [
            python, str(_default_cleaner(worker_dir)), "--shared", str(shared),
            "--queue-dir", str(queue), "--trash-dir", str(trash),
            "--interval", str(config.get("cleanerInterval", 300))]
        if not config.get("workerCommand") and not config.get("cleanerCommand"):
            observations["workerPresent"] = (
                (worker_dir / "worker.py").is_file()
                and _default_cleaner(worker_dir).is_file())
        # Evidence-backed only: an unsupported or contradictory observation
        # refuses as unknown instead of concluding a cause.
        verdict = reasons.classify(observations)
        if verdict is not None:
            raise verdict
        bridge = _load_bridge()
        try:
            bridge.load_key(str(key_file), str(config.get("keyFormat", "raw")), str(queue))
        except Exception as error:
            raise reasons.Refusal("key-unusable", f"key unusable: {error}") from error
        try:
            key_resolved = key_file.resolve()
            for label, root in (("queue", queue), ("shared LAN folder", shared),
                                ("checkout", HERE.parent.parent)):
                try:
                    inside = key_resolved.relative_to(root.resolve()) is not None
                except ValueError:
                    inside = False
                if inside:
                    raise reasons.Refusal("key-misplaced", f"key inside the {label}")
        except reasons.Refusal:
            raise
        except OSError as error:
            raise reasons.Refusal("key-unusable", "key custody unprovable") from error
        lock = _acquire_launch_lock(state)
        try:
            trash.mkdir(parents=True, exist_ok=True)
            worker_marker = str(config.get("workerMarker") or (worker_dir / "worker.py").name)
            cleaner_marker = str(config.get("cleanerMarker") or "cleaner.py")
            worker_binding = {"queue": str(queue), "key_file": str(key_file)}
            cleaner_binding = {"shared": str(shared), "trash_dir": str(trash)}
            # Inspect BOTH roles before spawning either: an ambiguous role
            # anywhere refuses with nothing started or duplicated.
            planned = (("worker", worker_cmd, worker_marker, worker_binding),
                       ("cleaner", cleaner_cmd, cleaner_marker, cleaner_binding))
            for role, command, marker, binding in planned:
                _inspect_role(state, role, command, marker, binding)
            for role, command, marker, binding in planned:
                summary["roles"].append(
                    _ensure_role(state, role, command, marker, binding, worker_cwd))
        finally:
            _release_launch_lock(lock)
        summary.update({"outcome": "ready", "reason": "none", "nextAction": "none"})
    except reasons.Refusal as error:
        return _refuse(summary, error)
    except (ValueError, KeyError, RuntimeError) as error:
        return _refuse(summary, reasons.Refusal(reasons.UNKNOWN, str(error)))
    print(json.dumps(summary, ensure_ascii=False))
    states = ", ".join(f'{r["role"]}:{r["state"]}:{r["pid"]}' for r in summary["roles"])
    print(f"launch ready ({states})", file=sys.stderr)
    return 0


def _default_cleaner(worker_dir: Path) -> Path:
    return worker_dir / "cleaner.py"


if __name__ == "__main__":
    raise SystemExit(main())
