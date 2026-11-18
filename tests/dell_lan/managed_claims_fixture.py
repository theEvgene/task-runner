"""Disposable harness for the Dell managed-write claim cases.

No Git, no live host, no network: one LAN share, one signed queue, one
absolute "Lenovo" checkout plus task worktree per target, and the real
request writer and managed-write submit entry point. The queue is the real
immutable file queue with the real reservation referee, so every admission,
conflict, and release below is produced by the shipped code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "dell_lan"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
REQUEST_CLI = SCRIPTS / "dell_request.py"
MANAGED_SUBMIT = SCRIPTS / "dell_managed_submit.py"
WORKER = ROOT / "scripts" / "lenovo_worker" / "worker.py"

sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(SCRIPTS))
import lan_bridge  # noqa: E402  (the reviewed client used as-is)

SHA_A = "a" * 40
SHA_B = "b" * 40


def run(command: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)


class ManagedClaimsCase(unittest.TestCase):
    """One queue, one writer target, and the shared request inputs."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-claims-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = self.root / "lan-share"
        self.shared.mkdir()
        self.queue = self.shared / "queue"
        (self.queue / "jobs").mkdir(parents=True)
        (self.queue / "results").mkdir()
        self.key = os.urandom(32)
        self.key_file = self.root / "key.bin"
        self.key_file.write_bytes(self.key)
        self.repo = self.root / "dell-copy"
        self.repo.mkdir()
        self.lenovo = self.root / "lenovo-checkout"
        self.lenovo.mkdir()
        self.worktree = self.root / "lenovo-task-worktree"
        self.drivers = self.root / "lenovo-drivers"
        self.command_file = self.root / "command.txt"
        (self.shared / "task.manifest.json").write_text("{}", encoding="utf-8")
        (self.shared / "task.bundle").write_bytes(b"bundle")
        (self.shared / "mr-description.md").write_text(
            "Fixture change.\n", encoding="utf-8")

    # -- request writing ------------------------------------------------
    def request_args(self, route: str = "publish", *, token: str,
                     name: str, checkout: Path | None = None,
                     worktree: Path | None = None, reconcile: bool = False,
                     authorize: str | None = None,
                     command_file: Path | None = None) -> list[str]:
        common = [
            "--shared", str(self.shared), "--token", token,
            "--lenovo-skill-dir", str(ROOT),
            "--lenovo-driver-dir", str(self.drivers),
            "--python", sys.executable, "--dialect", "cmd",
            "--lenovo-checkout", str(checkout or self.lenovo),
            "--lenovo-worktree", str(worktree or self.worktree),
            "--request-name", name,
            "--command-file", str(command_file or self.command_file),
            "--repo", str(self.repo),
        ]
        published = ["--expected-base", SHA_A, "--expected-commit", SHA_B]
        if route == "publish":
            args = ["--manifest", str(self.shared / "task.manifest.json"),
                    "--bundle", str(self.shared / "task.bundle"),
                    "--description-file", str(self.shared / "mr-description.md"),
                    "--branch", "task/iteration-1", "--target-branch", "main",
                    "--host", "gitlab.fixture.invalid", "--project", "group/project",
                    "--change-type", "feat", "--jira-key", "ABC-1",
                    "--subject", "fixture task", "--state", "draft",
                    "--authorize", authorize or "import,push,mr"]
        elif route == "transfer":
            args = ["--manifest", str(self.shared / "task.manifest.json"),
                    "--bundle", str(self.shared / "task.bundle"),
                    "--branch", "task/iteration-1",
                    "--authorize", authorize or "import"]
        elif route == "publish-existing":
            args = ["--branch", "task/iteration-1", "--target-branch", "main",
                    "--host", "gitlab.fixture.invalid", "--project", "group/project",
                    "--state", "draft", "--authorize", authorize or "push,mr"]
        else:
            raise AssertionError(f"unknown route {route}")
        argv = [sys.executable, str(REQUEST_CLI), route, *common, *published, *args]
        if reconcile:
            argv.append("--reconcile")
        return argv

    def write_request(self, route: str = "publish",
                      **kwargs) -> tuple[dict, subprocess.CompletedProcess]:
        proc = run(self.request_args(route, **kwargs))
        if proc.returncode != 0:
            raise AssertionError(f"request failed: {proc.stdout} {proc.stderr}")
        return json.loads(proc.stdout), proc

    def request_document(self, writer: dict) -> dict:
        return json.loads(Path(writer["requestPath"]).read_text(encoding="utf-8"))

    # -- managed submit -------------------------------------------------
    def submit_command(self, writer: dict,
                       output: Path | None = None) -> list[str]:
        path = output or (self.root / f"writer-{os.urandom(4).hex()}.json")
        path.write_text(json.dumps(writer), encoding="utf-8")
        return [sys.executable, str(MANAGED_SUBMIT), "--writer-output", str(path),
                "--queue", str(self.queue), "--key-file", str(self.key_file),
                "--repo", str(self.repo)]

    def submit(self, writer: dict,
               output: Path | None = None) -> subprocess.CompletedProcess:
        return run(self.submit_command(writer, output))

    def submit_ok(self, writer: dict, output: Path | None = None) -> str:
        proc = self.submit(writer, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().split("JOBID=", 1)[1].strip()

    # -- queue inspection -----------------------------------------------
    def job_files(self) -> list[str]:
        return sorted(path.name for path in (self.queue / "jobs").glob("*.json"))

    def reservation_files(self) -> list[str]:
        return sorted(path.name for path in
                      (self.queue / "reservations").glob("*.json"))

    def job_payload(self, job_id: str) -> dict:
        return lan_bridge.open_envelope(
            lan_bridge.read(str(self.queue / "jobs"), job_id),
            self.key, "job", job_id)

    def reservation(self, job_id: str) -> dict:
        return lan_bridge.open_envelope(
            lan_bridge.read(str(self.queue / "reservations"), job_id),
            self.key, "reservation", job_id)

    def run_worker(self) -> subprocess.CompletedProcess:
        return run([sys.executable, str(WORKER), "--queue", str(self.queue),
                    "--key-file", str(self.key_file), "--max-active", "1",
                    "--once"])
