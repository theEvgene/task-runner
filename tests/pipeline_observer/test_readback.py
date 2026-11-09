"""Readback tests: MAC/kind/ID/status/exit/timeout/truncation binding."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testkit import SHA1, SHA2, URL, FixtureBridge, passed_probe_receipt
import readback


class ReadbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = str(Path(self.tmp.name) / "queue")
        self.key_file = str(Path(self.tmp.name) / "lane.key")
        Path(self.key_file).write_bytes(os.urandom(32))
        self.bridge = FixtureBridge()
        self.key = self.bridge.load_key(self.key_file, "raw", self.queue)

    def _plant(self, job_id: str, payload: dict) -> None:
        self.bridge.publish(str(Path(self.queue) / "results"), job_id,
                            self.bridge.seal(payload, self.key))

    def _result(self, job_id: str, stdout: str, exit_code: int = 0,
                status: str = "ok") -> dict:
        return self.bridge.build_result_payload(
            job_id, status, exit_code, stdout=stdout, stderr="")

    def test_verified_passed_round_trip(self) -> None:
        job_id = "c" * 32
        self._plant(job_id, self._result(
            job_id, json.dumps(passed_probe_receipt())))
        payload = self.bridge.fetch_result(
            self.queue, self.key, job_id, timeout_seconds=5,
            poll_seconds=0.05)
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "passed")
        self.assertEqual(receipt["probe"]["pipeline_id"], 1)

    def test_forged_mac_refused(self) -> None:
        job_id = "d" * 32
        self._plant(job_id, self._result(
            job_id, json.dumps(passed_probe_receipt())))
        raw_path = Path(self.queue) / "results" / f"{job_id}.json"
        raw = bytearray(raw_path.read_bytes())
        raw[-12] ^= 0xFF
        raw_path.write_bytes(bytes(raw))
        with self.assertRaises(FixtureBridge.ProtocolError):
            self.bridge.fetch_result(self.queue, self.key, job_id,
                                     timeout_seconds=5, poll_seconds=0.05)

    def test_result_for_another_id_never_verifies(self) -> None:
        planted = "e" * 31 + "0"
        self._plant(planted, self._result(
            planted, json.dumps(passed_probe_receipt())))
        with self.assertRaises((FixtureBridge.ProtocolError,
                                 FixtureBridge.FetchTimeout)):
            self.bridge.fetch_result(self.queue, self.key, "e" * 31 + "1",
                                     timeout_seconds=1, poll_seconds=0.05)

    def test_mr_mismatch_refused(self) -> None:
        job_id = "f" * 32
        other = URL.replace("/7", "/8")
        self._plant(job_id, self._result(
            job_id, json.dumps(passed_probe_receipt(mr_url=other))))
        payload = self.bridge.fetch_result(
            self.queue, self.key, job_id, timeout_seconds=5,
            poll_seconds=0.05)
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "mismatch")
        self.assertEqual(receipt["reason"], "mr-mismatch")

    def test_sha_mismatch_refused(self) -> None:
        job_id = "0" * 32
        self._plant(job_id, self._result(
            job_id, json.dumps(passed_probe_receipt())))
        payload = self.bridge.fetch_result(
            self.queue, self.key, job_id, timeout_seconds=5,
            poll_seconds=0.05)
        receipt = readback.verify(payload, job_id, URL, SHA2)
        self.assertEqual(receipt["verdict"], "mismatch")
        self.assertEqual(receipt["reason"], "sha-mismatch")

    def test_non_passed_probe_never_verifies(self) -> None:
        job_id = "1" * 32
        failed = dict(passed_probe_receipt())
        failed.update(verdict="failed", reason="required_check_failed",
                      failed_job={"name": "unit"})
        self._plant(job_id, self._result(job_id, json.dumps(failed),
                                         exit_code=1))
        payload = self.bridge.fetch_result(
            self.queue, self.key, job_id, timeout_seconds=5,
            poll_seconds=0.05)
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt.get("verdict"), "failed")
        self.assertNotEqual(receipt.get("verdict"), "passed")

    def test_timed_out_result_is_unknown(self) -> None:
        job_id = "3" * 32
        payload = self.bridge.build_result_payload(
            job_id, "ok", 0, stdout=json.dumps(passed_probe_receipt()))
        payload["timed_out"] = True
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "unknown")

    def test_truncated_result_is_unknown(self) -> None:
        job_id = "7" * 32
        payload = self.bridge.build_result_payload(
            job_id, "ok", 0, stdout=json.dumps(passed_probe_receipt()))
        payload["output_truncated"] = True
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "unknown")

    def test_malformed_stdout_with_nonzero_exit_is_unknown(self) -> None:
        job_id = "4" * 32
        self._plant(job_id, self._result(job_id, "not-json{{{",
                                         exit_code=1))
        payload = self.bridge.fetch_result(
            self.queue, self.key, job_id, timeout_seconds=5,
            poll_seconds=0.05)
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "unknown")
        self.assertEqual(receipt["reason"], "probe-unreadable")

    def test_non_integer_exit_is_unknown(self) -> None:
        job_id = "5" * 32
        payload = self.bridge.build_result_payload(
            job_id, "ok", 0, stdout=json.dumps(passed_probe_receipt()))
        payload["exit_code"] = None
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "unknown")

    def test_untrusted_status_never_verifies(self) -> None:
        job_id = "a" * 32
        payload = self.bridge.build_result_payload(
            job_id, "weird", 0, stdout=json.dumps(passed_probe_receipt()))
        receipt = readback.verify(payload, job_id, URL, SHA1)
        self.assertEqual(receipt["verdict"], "unknown")
        self.assertNotEqual(receipt.get("verdict"), "passed")


if __name__ == "__main__":
    unittest.main()
