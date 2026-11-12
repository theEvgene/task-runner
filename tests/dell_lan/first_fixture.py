"""Disposable fixture for the first verified import (no Lenovo checkout).

Everything is local: one bare repository stands in for the corporate Git
remote, the real signed queue (HMAC sealing, immutable publish), the real
Dell request writer, the real Lenovo first-export driver, and the real Dell
first-import driver. The worker stand-in executes the submitted command
exactly as the queue would. The remote is a local bare repository and is
never contacted over the network.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "dell_lan"
FIRST_IMPORT = SCRIPTS / "dell_first_import.py"
REQUEST_CLI = SCRIPTS / "dell_request.py"
BRIDGE_CLI = ROOT / "scripts" / "lan_bridge" / "lan_bridge.py"

sys.path.insert(0, str(BRIDGE_CLI.parent))

import lan_bridge  # noqa: E402  (reviewed library used as-is)

BRANCH = "main"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def git(*args: str, cwd: Path | None = None) -> str:
    proc = run(["git", *args], cwd=cwd)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class FirstWorkspace:
    """One bare remote, one share, one signed queue, one key, no checkouts."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-first-"))
        self.origin = self.root / "remote-origin.git"
        self.seed = self.root / "seed"
        self.shared = self.root / "lan-share"
        self.anchor = self.root / "dell-anchor"
        self.queue = self.root / "queue"
        self.key = self.root / "key.bin"
        self.key_bytes = os.urandom(32)
        self.base = ""
        self.remote_url = ""

    def build(self) -> "FirstWorkspace":
        for path in (self.shared, self.anchor):
            path.mkdir(parents=True, exist_ok=True)
        self.key.write_bytes(self.key_bytes)
        git("init", "-q", "--bare", "-b", BRANCH, str(self.origin))
        git("init", "-q", "-b", BRANCH, str(self.seed))
        git("config", "user.email", "first-fixture@example.invalid", cwd=self.seed)
        git("config", "user.name", "first-fixture", cwd=self.seed)
        (self.seed / "work.txt").write_text("first base\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.seed)
        git("commit", "-qm", "first base", cwd=self.seed)
        self.base = git("rev-parse", "--verify", "HEAD", cwd=self.seed)
        git("remote", "add", "origin", str(self.origin), cwd=self.seed)
        git("push", "-q", "-u", "origin", BRANCH, cwd=self.seed)
        self.remote_url = str(self.origin)
        return self

    def first_request(self, token: str = "0123456789abcdef",
                      remote_url: str | None = None,
                      branch: str = BRANCH) -> dict:
        command_file = self.root / f"first-command-{token}.txt"
        proc = run([
            sys.executable, str(REQUEST_CLI), "first", "--shared", str(self.shared),
            "--token", token, "--lenovo-skill-dir", str(ROOT),
            "--lenovo-driver-dir", str(SCRIPTS),
            "--python", sys.executable, "--remote-url", remote_url or self.remote_url,
            "--ref-full", f"refs/heads/{branch}",
            "--request-name", f"first-request-{token}.json",
            "--command-file", str(command_file), "--repo", str(self.anchor),
        ])
        if proc.returncode != 0:
            raise AssertionError(f"first request failed: {proc.stdout} {proc.stderr}")
        data = json.loads(proc.stdout)
        data["commandFile"] = command_file
        return data

    def submit(self, command_file: Path, return_file: Path | None = None,
               cwd: Path | None = None) -> str:
        command = [
            sys.executable, str(BRIDGE_CLI), "submit", "--queue", str(self.queue),
            "--key-file", str(self.key), "--command-file", str(command_file),
            "--cwd", str(cwd or self.root), "--timeout", "300", "--max-output", "65536",
        ]
        if return_file is not None:
            command += ["--return-file", str(return_file)]
        proc = run(command)
        if proc.returncode != 0:
            raise AssertionError(f"bridge submit failed: {proc.stderr}")
        return proc.stdout.strip().split("JOBID=", 1)[1].strip()

    def request_first(self, token: str = "0123456789abcdef", **kwargs) -> str:
        data = self.first_request(token, **kwargs)
        return self.submit(data["commandFile"], return_file=Path(data["returnFile"]))

    def run_job(self, job_id: str) -> tuple[dict, subprocess.CompletedProcess]:
        """Execute the sealed job exactly as the queue worker would, then seal."""

        raw = lan_bridge.read(str(self.queue / "jobs"), job_id)
        payload = lan_bridge.open_envelope(raw, self.key_bytes, "job", job_id)
        proc = subprocess.run(
            payload["command"], shell=True, cwd=self.root,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=payload["timeout_seconds"], check=False,
        )
        files = {}
        for declared in payload.get("return_files") or []:
            path = Path(declared)
            if path.is_file():
                files[str(path)] = base64.b64encode(path.read_bytes()).decode("ascii")
        limit = int(payload["max_output_bytes"])
        result = lan_bridge.build_result_payload(
            job_id, "ok" if proc.returncode == 0 else "error", proc.returncode,
            stdout=proc.stdout[:limit], stderr=proc.stderr[:limit],
            started_at=now_iso(), finished_at=now_iso(),
            output_truncated=len(proc.stdout) > limit, files=files,
        )
        lan_bridge.publish(str(self.queue / "results"), job_id,
                           lan_bridge.seal(result, self.key_bytes))
        return payload, proc

    def seal_result(self, job_id: str, stdout: str, status: str = "ok",
                    exit_code: int = 0, timed_out: bool = False,
                    truncated: bool = False, stderr: str = "") -> Path:
        payload = lan_bridge.build_result_payload(
            job_id, status, exit_code, stdout=stdout, stderr=stderr,
            started_at=now_iso(), finished_at=now_iso(),
            timed_out=timed_out, output_truncated=truncated,
        )
        return lan_bridge.publish(str(self.queue / "results"), job_id,
                                  lan_bridge.seal(payload, self.key_bytes))

    def import_to(self, repo_out: Path, job_id: str, *extra: str,
                  receipt: Path | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        receipt = receipt or (self.root / f"first-import-{os.urandom(3).hex()}.json")
        proc = run([
            sys.executable, str(FIRST_IMPORT), "--queue", str(self.queue),
            "--key-file", str(self.key), "--job-id", job_id, "--shared", str(self.shared),
            "--repo", str(self.anchor),
            "--repo-out", str(repo_out), "--remote-url", self.remote_url,
            "--branch", BRANCH, "--receipt", str(receipt), *extra,
        ])
        data = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else None
        return proc, data

    def cleanup(self) -> None:
        for current, directories, files in os.walk(self.root):
            for name in files:
                try:
                    os.chmod(os.path.join(current, name), stat.S_IWRITE)
                except OSError:
                    pass
            for name in directories:
                try:
                    os.chmod(os.path.join(current, name), stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)
