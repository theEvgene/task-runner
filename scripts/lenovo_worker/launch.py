"""One Windows shortcut entry for the Lenovo side (stdlib only).

After one-time local setup, a single run: connects the existing SMB share
with credentials from a local file (never in args/log/output), verifies the
existing queue/key/share, then starts the worker (sibling ``worker.py``)
and the cleaner (sibling ``cleaner.py``) as detached processes.

Refuses without side effects when: config/password missing, SMB unreachable
or credentials wrong, queue or key unusable, trash/state misplaced, a stale/foreign
pid file exists, the recorded command/binding changed, another launch holds the
lock, or a second launch races. Both roles are inspected before either is
spawned, so an ambiguous role anywhere starts nothing. Never creates the queue,
never writes jobs or results, never kills or replaces a running process, never
prints secrets.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

LAUNCH_SCHEMA = "lenovo-launch/1"
HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))
import proc  # noqa: E402  (sibling helper, same delivery)


def _load_bridge():
    path = HERE.parent / "lan_bridge" / "lan_bridge.py"
    if not path.is_file():
        override = os.environ.get("DELL_LAN_BRIDGE_DIR")
        if override:
            path = Path(override) / "lan_bridge.py"
    if not path.is_file():
        raise RuntimeError("bridge client not found (scripts/lan_bridge/lan_bridge.py)")
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
        raise ValueError(f"{name} must be an absolute path")
    return path


def _load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read config: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def _read_password(password_file: Path) -> str:
    try:
        text = password_file.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ValueError(f"credential file unreadable: {error}") from error
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise ValueError("credential file holds no password")
    if len(lines) > 1:
        raise ValueError("credential file must hold a single line")
    return lines[0]


def _connect_share(smb: dict, shared: Path) -> None:
    """Make the existing share visible. Fixture mode verifies a local dir."""

    if not isinstance(smb, dict):
        raise ValueError("smb must be an object")
    fixture = smb.get("fixtureLocalDir")
    password_file = smb.get("passwordFile")
    if fixture:
        local = Path(str(fixture))
        if not local.is_dir():
            raise ValueError(f"share unavailable at {fixture}: check path/mount")
        expected = smb.get("fixtureRequirePassword")
        if expected is not None:
            if not password_file:
                raise ValueError("bad credentials: no passwordFile in config")
            if _read_password(Path(str(password_file))) != str(expected):
                raise ValueError("bad credentials: password mismatch (check credential file)")
        return
    unc = str(smb.get("unc") or "")
    username = str(smb.get("username") or "")
    if not unc or not username or not password_file:
        raise ValueError("smb needs unc, username and passwordFile")
    secret = _read_password(Path(str(password_file)))
    if len(secret) > 256:
        raise ValueError("bad credentials: credential file malformed")
    script = (
        "$ErrorActionPreference='Stop'\n"
        f"$unc={json.dumps(unc)}\n"
        f"$user={json.dumps(username)}\n"
        f"$pwFile={json.dumps(str(password_file))}\n"
        "if (Test-Path -LiteralPath $unc) { 'ALREADY:'+$unc; exit 0 }\n"
        # Same normalization as _read_password (first line, trimmed) so a
        # trailing CRLF written by any editor matches the fixture check.
        # The plaintext lives only in this transient script, never in
        # process args, stdout, stderr, logs, or receipts.
        "$pwRaw=Get-Content -Raw -LiteralPath $pwFile\n"
        "if ($null -eq $pwRaw -or $pwRaw.Trim().Length -eq 0) { 'BAD-CREDENTIAL-FILE'; exit 12 }\n"
        "$pwPlain=(($pwRaw -split \"\\r?\\n\")[0]).Trim()\n"
        "try {\n"
        "  $sec=ConvertTo-SecureString -String $pwPlain -AsPlainText -Force\n"
        "  $pwPlain=$null\n"
        "  $cred=New-Object PSCredential($user,$sec)\n"
        "  $null=New-SmbMapping -RemotePath $unc -UserName $user -Password $cred.GetNetworkCredential().Password -Persistent $false\n"
        "} catch [System.UnauthorizedAccessException] { 'AUTH-DENIED'; exit 10 }\n"
        "catch { 'MAP-FAILED:'+$_.Exception.Message; exit 11 }\n"
        "if (Test-Path -LiteralPath $unc) { 'MAPPED:'+$unc; exit 0 }\n"
        "'MAP-FAILED:unreachable after mapping'; exit 11\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script_path],
            capture_output=True, text=True, timeout=120, check=False,
        )
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
    out = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()
    first = out[0].strip() if out else ""
    if first.startswith(("ALREADY:", "MAPPED:")):
        return
    if completed.returncode in (10, 12) or first in ("AUTH-DENIED", "BAD-CREDENTIAL-FILE"):
        raise ValueError("bad credentials: SMB refused the username/password (check credential file)")
    raise ValueError(f"share unavailable at {unc}: check host/path (detail: {first[:160]})")


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
        raise ValueError("queueDir, sharedDir and trashDir must differ")
    for label, first, second in (("trash", trash, queue), ("trash", trash, shared),
                                 ("state", state, queue), ("state", state, shared),
                                 ("trash/state", trash, state)):
        if _within_or_equal(first, second):
            raise ValueError(f"{label} dir must be outside {second}")


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
            raise ValueError("another launch is in progress; re-run to adopt its pids")
        time.sleep(0.2)


def _release_launch_lock(lock: Path) -> None:
    shutil.rmtree(lock, ignore_errors=True)


def _inspect_role(state_dir: Path, role: str, command: list[str],
                  marker: str, binding: dict) -> tuple[str, int | None]:
    """Classify a role WITHOUT spawning: ("running", pid) or ("absent", None).

    Anything else raises ValueError (ambiguous): corrupt, stale, foreign,
    legacy-unbound, or config-changed pid state. Callers must refuse before
    spawning any role when any inspection is ambiguous.
    """

    pid_file = state_dir / f"{role}.pid"
    try:
        existing = proc.read_pid_file(pid_file)
    except (ValueError, OSError) as error:
        raise ValueError(f"ambiguous {role} state ({error}); verify no {role} runs, "
                         f"then remove {pid_file}") from error
    if existing is None:
        return ("absent", None)
    if "command" not in existing or "binding" not in existing:
        raise ValueError(f"ambiguous {role} state (unrecognized pid format, "
                         f"possibly written by an older launcher); verify no {role} "
                         f"runs, then remove {pid_file}")
    if (existing.get("marker") != marker
            or existing.get("command") != [str(p) for p in command]
            or existing.get("binding") != binding):
        raise ValueError(f"ambiguous {role} state (config changed under a recorded "
                         f"process); verify, then remove {pid_file}")
    pid = existing.get("pid")
    if not isinstance(pid, int) or not proc.is_alive(pid):
        raise ValueError(f"ambiguous {role} state (stale pid {existing.get('pid')}); "
                         f"verify no {role} runs, then remove {pid_file}")
    line = proc.cmdline(pid) or ""
    if (marker and marker not in line) or any(
            str(token) in " ".join(str(p) for p in command)
            and str(token) not in line
            for token in binding.values() if token):
        raise ValueError(f"ambiguous {role} state (pid {pid} runs foreign "
                         f"command); refusing to touch it")
    return ("running", pid)


def _ensure_role(state_dir: Path, role: str, command: list[str],
                 marker: str, binding: dict) -> dict:
    """Start the role once, or report the running owner. Never kill/replace."""

    command = [str(p) for p in command]
    state, pid = _inspect_role(state_dir, role, command, marker, binding)
    if state == "running":
        return {"role": role, "state": "already-running", "pid": pid}
    pid_file = state_dir / f"{role}.pid"
    log_file = state_dir / f"{role}.log"
    new_pid = proc.spawn_detached(command, log_file)
    try:
        proc.write_pid_file(pid_file, new_pid, marker, _now(),
                            {"command": command, "binding": binding})
    except FileExistsError as error:
        raise ValueError(f"ambiguous {role} state (pid file appeared mid-start); "
                         "re-run to adopt it") from error
    if not proc.is_alive(new_pid):
        raise ValueError(f"{role} exited at once; read {log_file}")
    return {"role": role, "state": "started", "pid": new_pid}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="launch.py", description="Lenovo one-shortcut launcher")
    parser.add_argument("--config", required=True, help="absolute path of the local JSON config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary: dict = {"schema": LAUNCH_SCHEMA, "at": _now(), "roles": []}
    try:
        config = _load_config(Path(args.config))
        queue = _abs(str(config["queueDir"]), "queueDir")
        shared = _abs(str(config["sharedDir"]), "sharedDir")
        key_file = _abs(str(config["keyFile"]), "keyFile")
        trash = _abs(str(config.get("trashDir", "") or ""), "trashDir")
        state = _abs(str(config.get("stateDir", "") or ""), "stateDir")
        _check_layout(queue, shared, trash, state)
        _connect_share(config.get("smb") or {}, shared)
        if not shared.is_dir():
            raise ValueError(f"shared folder unavailable at {shared}: check path/mount")
        if not queue.is_dir() or not (queue / "jobs").is_dir() or not (queue / "results").is_dir():
            raise ValueError(f"queue unavailable at {queue}: check path/mount")
        bridge = _load_bridge()
        try:
            bridge.load_key(str(key_file), str(config.get("keyFormat", "raw")), str(queue))
        except Exception as error:
            raise ValueError(f"key unusable: {error}") from error
        try:
            key_resolved = key_file.resolve()
            for label, root in (("queue", queue), ("shared LAN folder", shared),
                                ("checkout", HERE.parent.parent)):
                try:
                    inside = key_resolved.relative_to(root.resolve()) is not None
                except ValueError:
                    inside = False
                if inside:
                    raise ValueError(f"key inside the {label}")
        except ValueError:
            raise
        except OSError as error:
            raise ValueError(f"key custody unprovable: {error}") from error
        lock = _acquire_launch_lock(state)
        try:
            trash.mkdir(parents=True, exist_ok=True)
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
                summary["roles"].append(_ensure_role(state, role, command, marker, binding))
        finally:
            _release_launch_lock(lock)
        summary["outcome"] = "ready"
    except (ValueError, KeyError, RuntimeError) as error:
        summary["outcome"] = "refused"
        summary["reason"] = str(error)[:300]
        print(f"launch refused: {error}", file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False))
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    states = ", ".join(f'{r["role"]}:{r["state"]}:{r["pid"]}' for r in summary["roles"])
    print(f"launch ready ({states})", file=sys.stderr)
    return 0


def _default_cleaner(worker_dir: Path) -> Path:
    return worker_dir / "cleaner.py"


if __name__ == "__main__":
    raise SystemExit(main())
