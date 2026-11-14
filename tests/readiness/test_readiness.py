"""Fast fake-worker tests for the selected LAN readiness command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "readiness" / "check.py"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE_DIR))
import lan_bridge as bridge  # noqa: E402


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="readiness-fixture-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.queue = self.temp / "queue"
        self.queue.mkdir()
        self.key_path = self.temp / "fixture-secret-name.key"
        self.key = bytes(range(32))
        self.key_path.write_bytes(self.key)
        self.marker = self.temp / "component.txt"
        self.marker.write_text("fixture", encoding="utf-8")
        self.config_path = self.temp / "config.json"
        self.config = {
            "schema": "lan-readiness-config/1",
            "scenario": "lan-bridge",
            "queue": str(self.queue),
            "keyFile": str(self.key_path),
            "local": {"components": [], "tools": []},
            "lenovo": {
                "python": sys.executable,
                "cwd": str(self.temp),
                "components": [{"name": "bridge-client", "type": "file",
                                "path": str(self.marker)}],
                "tools": [],
            },
        }
        self._write_config()

    def _write_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def _command(self, *extra: str) -> list[str]:
        return [sys.executable, str(SCRIPT), "--config", str(self.config_path),
                "--wait", "0.35", "--poll", "0.03", *extra]

    def _result(self, job_id: str, state: str = "confirmed") -> dict:
        body = {"schema": "lenovo-readiness-probe/1", "scenario": "lan-bridge",
                "components": [{"name": "bridge-client", "location": "lenovo",
                                "state": state,
                                "reason": "fixture component result",
                                "nextAction": "none" if state == "confirmed"
                                else "update bridge-client"}]}
        return bridge.build_result_payload(
            job_id, "completed", exit_code=0,
            stdout=json.dumps(body, separators=(",", ":")))

    def _wait_job_ids(self, count: int = 1) -> list[str]:
        deadline = time.monotonic() + 5
        jobs = self.queue / "jobs"
        while time.monotonic() < deadline:
            ids = sorted(path.stem for path in jobs.glob("*.json")) if jobs.exists() else []
            if len(ids) >= count:
                return ids
            time.sleep(0.01)
        self.fail("fake worker did not receive the expected jobs")

    def _worker(self, state: str = "confirmed", tamper: bool = False,
                count: int = 1, result_changes: dict | None = None) -> threading.Thread:
        def run() -> None:
            for job_id in self._wait_job_ids(count):
                raw = bridge.read(self.queue / "jobs", job_id)
                bridge.open_envelope(raw, self.key, "job", job_id)
                result = self._result(job_id, state)
                result.update(result_changes or {})
                sealed = bytearray(bridge.seal(result, self.key))
                if tamper:
                    sealed[-3] ^= 1
                bridge.publish(self.queue / "results", job_id, bytes(sealed))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_success_is_mac_bound_and_secret_free(self):
        worker = self._worker()
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "ready")
        self.assertRegex(report["jobId"], r"^[0-9a-f]{32}$")
        self.assertNotIn(str(self.key_path), proc.stdout + proc.stderr)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    def test_stale_component_is_not_success(self):
        worker = self._worker("stale")
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "not-ready")
        self.assertIn("stale", {item["state"] for item in report["components"]})

    def test_bad_mac_is_refused(self):
        worker = self._worker(tamper=True)
        proc = subprocess.run(self._command("--authorize-lenovo-check"),
                              capture_output=True, text=True, check=False)
        worker.join(2)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("bad mac", proc.stderr)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    def test_worker_exit_timeout_and_truncation_are_never_ready(self):
        cases = ({"exit_code": 7}, {"timed_out": True},
                 {"output_truncated": True}, {"status": "unknown"})
        for changes in cases:
            with self.subTest(changes=changes):
                shutil.rmtree(self.queue / "jobs", ignore_errors=True)
                shutil.rmtree(self.queue / "results", ignore_errors=True)
                worker = self._worker(result_changes=changes)
                proc = subprocess.run(self._command("--authorize-lenovo-check"),
                                      capture_output=True, text=True, check=False)
                worker.join(2)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["outcome"], "not-ready")

    def test_timeout_then_same_id_retrieval_never_resubmits(self):
        first = subprocess.run(self._command("--authorize-lenovo-check"),
                               capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 3, first.stderr)
        unknown = json.loads(first.stdout)
        job_id = unknown["jobId"]
        self.assertEqual(len(self._wait_job_ids()), 1)
        bridge.publish(self.queue / "results", job_id,
                       bridge.seal(self._result(job_id), self.key))
        resumed = subprocess.run(self._command("--job-id", job_id),
                                 capture_output=True, text=True, check=False)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["jobId"], job_id)
        self.assertEqual(len(self._wait_job_ids()), 1)

    def test_parallel_checks_have_distinct_immutable_jobs(self):
        worker = self._worker(count=2)
        first = subprocess.Popen(self._command("--authorize-lenovo-check"),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        second = subprocess.Popen(self._command("--authorize-lenovo-check"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        out1, err1 = first.communicate(timeout=10)
        out2, err2 = second.communicate(timeout=10)
        worker.join(2)
        self.assertEqual((first.returncode, second.returncode), (0, 0), err1 + err2)
        ids = {json.loads(out1)["jobId"], json.loads(out2)["jobId"]}
        self.assertEqual(len(ids), 2)
        self.assertEqual(set(self._wait_job_ids(2)), ids)
        self.assertEqual({path.stem for path in (self.queue / "results").glob("*.json")}, ids)


if __name__ == "__main__":
    unittest.main()
