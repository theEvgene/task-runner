"""Process helpers for the Lenovo single-shortcut launcher (stdlib only).

One owner per pid file: the launcher writes ``{"pid", "marker", "started"}``
atomically (exclusive create) and never kills anything. A pid file that
names a dead process, a foreign command line, or a different marker is
ambiguous: the caller must refuse, never guess, never kill, never replace.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def is_alive(pid: int) -> bool:
    """True when a process with this pid exists (no signal is sent)."""

    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        if os.name == "nt":
            # tasklist exits 0 and lists the row when the pid exists.
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            return proc.returncode == 0 and f'"{pid}"' in (proc.stdout or "")
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def cmdline(pid: int) -> str | None:
    """The command line of a process, or None when it cannot be proven."""

    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
                 "Select-Object -ExpandProperty CommandLine)"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            text = (proc.stdout or "").strip()
            return text or None
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
        return data.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_pid_file(path: Path) -> dict | None:
    """Parse a launcher pid file; None when absent. Raises on corrupt content."""

    import json

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        raise ValueError(f"corrupt pid file: {path}")
    return data


def write_pid_file(path: Path, pid: int, marker: str, started: str,
                   extra: dict | None = None) -> None:
    """Exclusive create; raises FileExistsError when another owner won.

    ``extra`` carries the identity the next launch must match exactly
    (full command array plus configuration binding such as queue/key);
    it is stored verbatim and never interpreted here.
    """

    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"pid": pid, "marker": marker, "started": started}
    if extra:
        record.update(dict(extra))
    payload = (json.dumps(record) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def spawn_detached(command: list[str], log_path: Path) -> int:
    """Start a daemon child with output to a log file; return its pid.

    The child inherits no secrets: callers pass paths only, never content.
    """

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "ab", buffering=0)
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            proc = subprocess.Popen([str(p) for p in command],
                                    stdout=handle, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=flags, close_fds=True)
        else:
            proc = subprocess.Popen([str(p) for p in command],
                                    stdout=handle, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
        return int(proc.pid)
    finally:
        try:
            handle.close()
        except OSError:
            pass


def python_executable() -> str:
    return sys.executable
