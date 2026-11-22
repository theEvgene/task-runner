"""Shared fake-worker fixture for the Lenovo selected check and its receipts."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "readiness" / "selected.py"
READINESS_DIR = ROOT / "scripts" / "readiness"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(READINESS_DIR))
import check as readiness  # noqa: E402
import lan_bridge as bridge  # noqa: E402
import selected  # noqa: E402
import selected_hosts  # noqa: E402


def _head() -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()


class LenovoSelectedFixture(unittest.TestCase):
    """One temporary queue, key, target checkout and descriptor per test."""

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="selected-lenovo-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.queue = self.temp / "queue"
        self.queue.mkdir()
        self.key_path = self.temp / "fixture-secret.key"
        self.key = bytes(range(32))
        self.key_path.write_bytes(self.key)
        self.target = self.temp / "target"
        self.target.mkdir()
        self.head = _head()
        self.config_path = self.temp / "readiness.json"
        self.config_path.write_text(json.dumps({
            "schema": "lan-readiness-config/1",
            "scenario": "lan-bridge",
            "queue": str(self.queue),
            "keyFile": str(self.key_path),
            "local": {"components": [], "tools": []},
            "lenovo": {
                "python": sys.executable,
                "cwd": str(self.temp),
                "components": [{"name": "checkout", "type": "directory",
                                "path": str(self.target)}],
                "tools": [{"name": "python", "command": sys.executable}],
            },
        }), encoding="utf-8")
        self.config = readiness.validate_config(
            json.loads(self.config_path.read_text(encoding="utf-8")))
        self.evidence_job = bridge.submit_job(
            str(self.queue), self.key, readiness.probe_command(self.config),
            self.config["lenovo"]["cwd"], timeout_seconds=60,
            max_output_bytes=65536, dialect="cmd")
        self._publish_evidence()
        self.descriptor_path = self.temp / "descriptor.json"
        self.descriptor = {
            "schema": "lan-selected-check/1",
            "check": {"name": "fixture", "host": "lenovo",
                      "effect": "read-only",
                      "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                      "cwd": str(self.target), "expectedRevision": self.head,
                      "timeoutSeconds": 30, "maxOutputBytes": 65536},
            "lenovo": {
                "queue": str(self.queue), "keyFile": str(self.key_path),
                "keyFormat": "raw", "python": sys.executable, "git": "git",
                "readinessConfig": str(self.config_path),
                "readinessJobId": self.evidence_job,
                "requiredComponents": {"cwd": "checkout", "tool": "python"},
                "waitSeconds": 15.0, "pollSeconds": 0.05,
                "evidenceWaitSeconds": 0.5,
            },
        }

    _DEFAULT = object()

    def _publish_evidence(self, tamper: bool = False,
                          schema: str = "lenovo-readiness-probe/2",
                          revision: object = _DEFAULT,
                          cwd_digest: str | None = None) -> None:
        if revision is self._DEFAULT:
            revision = self.head
        checkout = {"name": "checkout", "state": "confirmed"}
        if schema == "lenovo-readiness-probe/2":
            checkout["observedRevision"] = revision
            checkout["cwdDigest"] = cwd_digest or hashlib.sha256(
                str(self.target).encode("utf-8")).hexdigest()
        body = {
            "schema": schema, "scenario": "lan-bridge",
            "components": [checkout, {"name": "python", "state": "confirmed"}],
        }
        result = bridge.build_result_payload(
            self.evidence_job, "completed", exit_code=0,
            stdout=json.dumps(body, separators=(",", ":")))
        sealed = bytearray(bridge.seal(result, self.key))
        if tamper:
            sealed[-3] ^= 1
        bridge.publish(self.queue / "results", self.evidence_job, bytes(sealed))

    def _republish_evidence(self, **kwargs: object) -> None:
        (self.queue / "results" / f"{self.evidence_job}.json").unlink()
        self._publish_evidence(**kwargs)

    def _job_ids(self) -> list[str]:
        jobs = self.queue / "jobs"
        return sorted(path.stem for path in jobs.glob("*.json")) \
            if jobs.exists() else []

    def _run(self, descriptor: dict | None = None,
             extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        self.descriptor_path.write_text(
            json.dumps(descriptor or self.descriptor), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--descriptor",
             str(self.descriptor_path), *extra],
            capture_output=True, text=True, check=False)

    def _fake_worker(self, receipt: dict, status: str = "completed",
                     exit_code: int | None = 0, tamper: bool = False,
                     timed_out: bool = False,
                     job_id: str | None = None) -> threading.Thread:
        def run() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                pending = [job for job in self._job_ids()
                           if job != self.evidence_job
                           and (job_id is None or job == job_id)]
                if pending:
                    job_id_found = pending[0]
                    bridge.open_envelope(
                        bridge.read(self.queue / "jobs", job_id_found),
                        self.key, "job", job_id_found)
                    payload = bridge.build_result_payload(
                        job_id_found, status, exit_code=exit_code,
                        timed_out=timed_out,
                        stdout=json.dumps(receipt, separators=(",", ":")))
                    sealed = bytearray(bridge.seal(payload, self.key))
                    if tamper:
                        sealed[-3] ^= 1
                    bridge.publish(self.queue / "results", job_id_found,
                                   bytes(sealed))
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _receipt(self, proc: subprocess.CompletedProcess) -> dict:
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1, proc.stderr)
        return json.loads(proc.stdout)

    def _check_job_id(self, receipt: dict) -> str:
        return receipt["execution"]["jobId"]

    def _descriptor_copy(self, timeout: int) -> dict:
        data = json.loads(json.dumps(self.descriptor))
        data["check"]["timeoutSeconds"] = timeout
        return data

    def _seal_signed_job(self, job_id: str, timeout_seconds: int,
                         *, descriptor: dict | None = None,
                         command: str | None = None,
                         cwd: str | None = None,
                         max_output_bytes: int = 65536) -> None:
        """Publish a genuinely signed job, optionally with a legacy timeout."""
        source = descriptor or self.descriptor
        payload = {
            "kind": "job", "id": job_id,
            "command": command or selected_hosts.remote_command(source),
            "working_directory": cwd or source["check"]["cwd"],
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
            "return_files": [], "dialect": "cmd",
        }
        bridge.publish(self.queue / "jobs", job_id,
                       bridge.seal(payload, self.key))

    def _resume(self, descriptor: dict,
                job_id: str) -> tuple[dict, bytes | None]:
        return selected_hosts.resume_lenovo(
            selected.validate_descriptor(descriptor), job_id)
