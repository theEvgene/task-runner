"""Worker round-trip tests: synthetic worker, late answers, parallel files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testkit import (SHA1, SHA2, URL, FixtureBridge, passed_probe_receipt)
import readback
import request as request_mod


class SyntheticWorkerTests(unittest.TestCase):
    """Subprocess worker -> sealed result -> fetch -> readback (all local)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = str(Path(self.tmp.name) / "queue")
        self.key_file = str(Path(self.tmp.name) / "lane.key")
        Path(self.key_file).write_bytes(os.urandom(32))
        self.bridge = FixtureBridge()
        self.key = self.bridge.load_key(self.key_file, "raw", self.queue)
        self.counter = 0

    def _next_id(self, prefix: str) -> str:
        self.counter += 1
        return (prefix * 32)[:30] + f"{self.counter:02x}"

    def _run_worker(self, job_id: str, argv: list[str],
                    max_output: int = 65536) -> dict:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              check=False, timeout=60)
        stdout = (proc.stdout or "")[:max_output]
        truncated = len(proc.stdout or "") > max_output
        status = "ok" if proc.returncode == 0 else "error"
        payload = self.bridge.build_result_payload(
            job_id, status, proc.returncode, stdout=stdout, stderr="",
            output_truncated=truncated)
        self.bridge.publish(str(Path(self.queue) / "results"), job_id,
                            self.bridge.seal(payload, self.key))
        return self.bridge.fetch_result(
            self.queue, self.key, job_id, timeout_seconds=5,
            poll_seconds=0.05)

    def _probe_command(self, receipt: dict, exit_code: int) -> list[str]:
        code = ("import json,sys;"
                f"sys.stdout.write({json.dumps(json.dumps(receipt))});"
                f"sys.exit({int(exit_code)})")
        return [sys.executable, "-c", code]

    def test_running_then_completed_pipeline_round_trip(self) -> None:
        pending = dict(passed_probe_receipt())
        pending.update(verdict="pending", reason="checks_pending",
                       pipeline_id=2)
        job_id = self._next_id("b")
        payload = self._run_worker(job_id,
                                   self._probe_command(pending, 1))
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt.get("verdict"), "pending")
        job_id2 = self._next_id("b")
        payload2 = self._run_worker(
            job_id2, self._probe_command(passed_probe_receipt(), 0))
        receipt2 = readback.verify(payload2, job_id2, URL, SHA1)
        self.assertEqual(receipt2["verdict"], "passed")

    def test_synthetic_worker_failed_reports_failed_with_evidence(self) -> None:
        failed = dict(passed_probe_receipt())
        failed.update(verdict="failed", reason="required_check_failed",
                      failed_job={"name": "unit", "id": 11})
        job_id = self._next_id("c")
        payload = self._run_worker(job_id,
                                   self._probe_command(failed, 1))
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt.get("verdict"), "failed")
        self.assertEqual(receipt["probe"]["failed_job"], {"name": "unit",
                                                          "id": 11})

    def test_synthetic_worker_truncated_is_unknown(self) -> None:
        job_id = self._next_id("d")
        payload = self._run_worker(
            job_id, self._probe_command(passed_probe_receipt(), 0),
            max_output=16)
        self.assertTrue(payload["output_truncated"])
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "unknown")

    def test_late_answer_rereads_same_id_without_resubmit(self) -> None:
        job_id = self._next_id("e")
        with self.assertRaises(FixtureBridge.FetchTimeout):
            self.bridge.fetch_result(self.queue, self.key, job_id,
                                     timeout_seconds=1, poll_seconds=0.05)
        payload = self._run_worker(
            job_id, self._probe_command(passed_probe_receipt(), 0))
        again = self.bridge.fetch_result(
            self.queue, self.key, job_id, timeout_seconds=5,
            poll_seconds=0.05)
        self.assertEqual(again["stdout"], payload["stdout"])
        receipt = readback.verify(again, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "passed")

    def test_stale_sha_is_mismatch(self) -> None:
        job_id = self._next_id("f")
        payload = self._run_worker(
            job_id, self._probe_command(passed_probe_receipt(), 0))
        receipt = readback.verify(payload, job_id, URL, SHA2)
        self.assertEqual(receipt["verdict"], "mismatch")

    def test_parallel_calls_use_separate_files(self) -> None:
        root = Path(self.tmp.name)
        first_req = root / "first.request.json"
        first_cmd = root / "first.command.txt"
        second_req = root / "second.request.json"
        second_cmd = root / "second.command.txt"
        for req_file, cmd_file in ((first_req, first_cmd),
                                   (second_req, second_cmd)):
            self.assertEqual(request_mod.main([
                "--mr-url", URL, "--expected-sha", SHA1,
                "--profile", "profile-literal",
                "--python", "/usr/bin/python3" if os.name != "nt"
                else "C:\\Python\\python.exe",
                "--lenovo-flow-dir", "/srv/flow" if os.name != "nt"
                else "C:\\flow",
                "--lenovo-checkout", "/srv/checkout" if os.name != "nt"
                else "C:\\checkout",
                "--request-file", str(req_file),
                "--command-file", str(cmd_file)]), 0)
        self.assertNotEqual(first_req.read_text(encoding="utf-8"),
                            "")
        self.assertTrue(second_req.is_file() and second_cmd.is_file())
        first = json.loads(first_req.read_text(encoding="utf-8"))
        self.assertEqual(first["mr_url"], URL)
        self.assertEqual(first["expected_sha"], SHA1)


class RequestTests(unittest.TestCase):
    def test_rejects_relative_lenovo_paths(self) -> None:
        with self.assertRaises(Exception):
            request_mod.build_request(URL, SHA1, "profile.json",
                                      "python.exe", "C:\\flow" if os.name
                                      == "nt" else "/srv/flow",
                                      "C:\\out" if os.name == "nt"
                                      else "/srv/out")

    def test_rejects_bad_sha(self) -> None:
        with self.assertRaises(Exception):
            request_mod.build_request(URL, "xyz", "p",
                                      "C:\\py" if os.name == "nt"
                                      else "/usr/bin/python3",
                                      "C:\\flow" if os.name == "nt"
                                      else "/srv/flow",
                                      "C:\\out" if os.name == "nt"
                                      else "/srv/out")


if __name__ == "__main__":
    unittest.main()
