"""Probe tests: running vs completed pipelines, SHA binding, strict checks."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testkit import (SHA1, SHA2, URL, FakeGitlab, bridge_entry, job, pipe,
                      prof, run_probe)
import gate
import probe as probe_mod


class ProbeTests(unittest.TestCase):
    def test_completed_pipeline_passed_for_expected_sha(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "success")],
                          details={1: pipe(1, SHA1, "success")},
                          jobs={1: [job("unit", "success", 11)]})
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "passed")
        self.assertEqual(receipt["mr_url"], URL)
        self.assertEqual(receipt["observed_sha"], SHA1)
        self.assertEqual(receipt["pipeline_id"], 1)
        self.assertTrue(receipt["read_only"])

    def test_running_pipeline_is_pending_not_passed(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "running")],
                          details={1: pipe(1, SHA1, "running")},
                          jobs={1: [job("unit", "running", 11)]})
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "pending")
        self.assertNotEqual(receipt.get("verdict"), "passed")

    def test_new_sha_invalidates_old_result(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "success")],
                          details={1: pipe(1, SHA1, "success")},
                          jobs={1: [job("unit", "success", 11)]})
        receipt = run_probe(fake, SHA2, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "mismatch")
        self.assertEqual(receipt["reason"], "sha_mismatch")
        self.assertNotEqual(receipt.get("verdict"), "passed")

    def test_failed_check_carries_evidence_and_stops(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "failed")],
                          details={1: pipe(1, SHA1, "failed")},
                          jobs={1: [job("unit", "failed", 11)]},
                          traces={11: "FAIL src/a.test.ts\nExpected: 1"})
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "failed")
        self.assertIsNotNone(receipt.get("failed_job"))
        self.assertEqual(receipt["failed_job"]["name"], "unit")
        self.assertEqual(receipt["diagnosis"]["decision"], "stop")

    def test_green_parent_missing_child_is_never_passed(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "success")],
                          details={1: pipe(1, SHA1, "success")},
                          jobs={1: [job("unit", "success", 11)]},
                          bridges={1: []})
        receipt = run_probe(fake, SHA1,
                            prof(["unit"], [{"name": "playwright"}]))
        self.assertEqual(receipt["verdict"], "missing_required_check")
        self.assertNotEqual(receipt.get("verdict"), "passed")

    def test_unreadable_trace_fails_closed(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "failed")],
                          details={1: pipe(1, SHA1, "failed")},
                          jobs={1: [job("unit", "failed", 11)]},
                          traces={})
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "failed")
        self.assertEqual(receipt["diagnosis"]["cause"], "unknown-log")
        self.assertEqual(receipt["diagnosis"]["decision"], "stop")

    def test_unavailable_read_is_not_passed(self) -> None:
        fake = FakeGitlab(heads=[SHA1], pipelines=[],
                          unavailable="pipelines")
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "unavailable")
        self.assertNotEqual(receipt.get("verdict"), "passed")

    def test_changed_head_during_read_invalidates(self) -> None:
        fake = FakeGitlab(heads=[SHA1, SHA2],
                          pipelines=[pipe(1, SHA1, "success")],
                          details={1: pipe(1, SHA1, "success")},
                          jobs={1: [job("unit", "success", 11)]})
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertNotEqual(receipt.get("verdict"), "passed")

    def test_bad_sha_rejected(self) -> None:
        fake = FakeGitlab(heads=[SHA1], pipelines=[])
        with self.assertRaises(gate.GateInputError):
            run_probe(fake, "zzz", prof(["unit"]))

    def test_bad_url_rejected(self) -> None:
        fake = FakeGitlab(heads=[SHA1], pipelines=[])
        with self.assertRaises(gate.GateInputError):
            probe_mod.probe("not-a-url", SHA1, prof(["unit"]),
                            get=fake.get, read_log=fake.read_log)


if __name__ == "__main__":
    unittest.main()
