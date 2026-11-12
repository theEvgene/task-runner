"""One Windows shortcut entry for the Lenovo side (stdlib only).

After one-time local setup, a single run: connects the existing SMB share
with credentials from a local file (never in args/log/output), verifies the
existing queue/key/share, then starts the worker (sibling ``worker.py``)
and the cleaner (sibling ``cleaner.py``) as detached processes.

Refuses without side effects when: config/password missing, SMB unreachable
or credentials wrong, queue or key unusable, a stale/foreign pid file exists,
or a second launch races. Never creates the queue, never writes jobs or
results, never kills or replaces a running process, never prints secrets.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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
    secret = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not secret:
        raise ValueError("credential file holds no password")
    return secret


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
    if secret != secret.strip() or len(secret) > 256:
        raise ValueError("bad credentials: credential file malformed")
    script = (
        "$ErrorActionPreference='Stop'\n"
        f"$unc={json.dumps(unc)}\n"
        f"$user={json.dumps(username)}\n"
        f"$pwFile={json.dumps(str(password_file))}\n"
        "if (Test-Path -LiteralPath $unc) { 'ALREADY:'+$unc; exit 0 }\n"
        "try {\n"
        "  $sec=Get-Content -Raw -LiteralPath $pwFile | ConvertTo-SecureString -AsPlainText -Force\n"
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
    if completed.returncode == 10 or "AUTH-DENIED" in first:
        raise ValueError("bad credentials: SMB refused the username/password (check credential file)")
    raise ValueError(f"share unavailable at {unc}: check host/path (detail: {first[:160]})")


def _ensure_role(state_dir: Path, role: str, command: list[str], marker: str) -> dict:
    """Start the role once, or report the running owner. Never kill/replace."""

    pid_file = state_dir / f"{role}.pid"
    log_file = state_dir / f"{role}.log"
    try:
        existing = proc.read_pid_file(pid_file)
    except (ValueError, OSError) as error:
        raise ValueError(f"ambiguous {role} state ({error}); verify no {role} runs, "
                         f"then remove {pid_file}") from error
    if existing is not None:
        if existing.get("marker") != marker:
            raise ValueError(f"ambiguous {role} state (config changed under a recorded "
                             f"process); verify, then remove {pid_file}")
        pid = int(existing["pid"])
        if proc.is_alive(pid):
            line = proc.cmdline(pid) or ""
            if marker and marker not in line:
                raise ValueError(f"ambiguous {role} state (pid {pid} runs foreign "
                                 f"command); refusing to touch it")
            return {"role": role, "state": "already-running", "pid": pid}
        raise ValueError(f"ambiguous {role} state (stale pid {pid}); verify no {role} "
                         f"runs, then remove {pid_file}")
    try:
        pid = proc.spawn_detached(command, log_file)
    except FileExistsError as error:
        raise ValueError(f"ambiguous {role} state (lost the start race); re-run") from error
    try:
        proc.write_pid_file(pid_file, pid, marker, _now())
    except FileExistsError as error:
        raise ValueError(f"ambiguous {role} state (pid file appeared mid-start); "
                         "re-run to adopt it") from error
    if not proc.is_alive(pid):
        raise ValueError(f"{role} exited at once; read {log_file}")
    return {"role": role, "state": "started", "pid": pid}


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
        if queue == shared or trash == queue:
            raise ValueError("queueDir, sharedDir and trashDir must differ")
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
        state.mkdir(parents=True, exist_ok=True)
        trash.mkdir(parents=True, exist_ok=True)
        python = str(config.get("python") or proc.python_executable())
        worker_dir = Path(str(config.get("workerDir") or str(HERE)))
        worker_cmd = [str(p) for p in config.get("workerCommand", [])] or [
            python, str(worker_dir / "worker.py"), "--queue", str(queue),
            "--key-file", str(key_file), "--key-format", str(config.get("keyFormat", "raw")),
            "--shared", str(shared), "--poll", str(config.get("workerPoll", 5))]
        cleaner_cmd = [str(p) for p in config.get("cleanerCommand", [])] or [
            python, str(_default_cleaner(worker_dir)), "--shared", str(shared),
            "--trash-dir", str(trash), "--interval", str(config.get("cleanerInterval", 300))]
        worker_marker = str(config.get("workerMarker") or (worker_dir / "worker.py").name)
        cleaner_marker = str(config.get("cleanerMarker") or "cleaner.py")
        summary["roles"].append(_ensure_role(state, "worker", worker_cmd, worker_marker))
        summary["roles"].append(_ensure_role(state, "cleaner", cleaner_cmd, cleaner_marker))
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
