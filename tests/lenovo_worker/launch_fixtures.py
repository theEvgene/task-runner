"""Shared fixture helpers for the Lenovo one-shortcut launch tests (stdlib).

No network, no live queue, no real SMB: the share is a local temp dir via
``smb.fixtureLocalDir`` and all secrets are fixture strings. Each case uses
a fresh temp root and kills the stub processes it starts. This module holds
no test case, so one launch test file stays inside the per-file token guard.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "lenovo_worker"
BRIDGE = ROOT / "scripts" / "lan_bridge"

sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(SCRIPTS))
import lan_bridge  # noqa: E402

WORKER = SCRIPTS / "worker.py"
CLEANER = SCRIPTS / "cleaner.py"
LAUNCH = SCRIPTS / "launch.py"
SECRET = "fixture-secret-pw-9z"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(p) for p in command], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=60, check=False, **kwargs)


class TempRoot:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lenovo-launch-"))
        self.pids: list[int] = []

    def cleanup(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for current, dirs, files in os.walk(self.root):
            for name in files:
                try:
                    os.chmod(os.path.join(current, name), stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def track_pids(self, state_dir: Path) -> None:
        for role in ("worker", "cleaner"):
            pid_file = state_dir / f"{role}.pid"
            if pid_file.is_file():
                try:
                    self.pids.append(int(json.loads(pid_file.read_text())["pid"]))
                except (ValueError, KeyError, OSError):
                    pass


def make_key(path: Path) -> bytes:
    key = os.urandom(32)
    path.write_bytes(key)
    return key


def make_queue(root: Path) -> Path:
    queue = root / "queue"
    (queue / "jobs").mkdir(parents=True)
    (queue / "results").mkdir(parents=True)
    return queue


def seal_job(queue: Path, key: bytes, command: str = "echo hi",
             workdir: str | None = None) -> str:
    job_id = os.urandom(16).hex()
    payload = lan_bridge.build_job_payload(
        job_id=job_id, command=command,
        working_directory=workdir or os.fspath(queue.parent))
    lan_bridge.publish(str(queue / "jobs"), job_id, lan_bridge.seal(payload, key))
    return job_id


def write_config(path: Path, **fields) -> Path:
    path.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def base_config(root: Path, queue: Path, shared: Path, key: Path,
                state: Path, trash: Path, **smb_extra) -> dict:
    pw = root / "pw.txt"
    pw.write_text(SECRET + "\n", encoding="utf-8")
    smb = {"fixtureLocalDir": os.fspath(shared), "passwordFile": os.fspath(pw)}
    smb.update(smb_extra)
    return {"queueDir": os.fspath(queue), "sharedDir": os.fspath(shared),
            "keyFile": os.fspath(key), "keyFormat": "raw",
            "python": sys.executable,
            "trashDir": os.fspath(trash), "stateDir": os.fspath(state),
            "smb": smb}
