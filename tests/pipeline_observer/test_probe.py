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


    def test_empty_profile_refused_strict(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "success")],
                          details={1: pipe(1, SHA1, "success")},
                          jobs={1: [job("unit", "success", 11)]})
        with self.assertRaises(gate.GateInputError):
            run_probe(fake, SHA1, prof([]))
        with self.assertRaises(gate.GateInputError):
            gate.normalize_profile(None)

    def test_malformed_pipeline_id_is_unavailable_not_crash(self) -> None:
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[{"id": None, "sha": SHA1,
                                      "status": "success",
                                      "web_url": URL}],
                          details={}, jobs={})
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "unavailable")
        self.assertNotEqual(receipt.get("verdict"), "passed")

    def test_live_client_missing_dir_refused(self) -> None:
        with self.assertRaises(gate.GateInputError):
            probe_mod.load_live_client(Path("C:/no/such/dir/definitely"))

    def test_trace_read_uses_job_project(self) -> None:
        from testkit import PROJECT, SINGLE_LOG, run_probe
        calls: list[tuple] = []
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "failed")],
                          details={1: pipe(1, SHA1, "failed")},
                          jobs={1: [job("unit", "failed", 11)]},
                          traces={11: SINGLE_LOG})
        receipt = run_probe(
            fake, SHA1, prof(["unit"]),
            read_log=lambda h, p, j: (calls.append((h, p, j)),
                                      SINGLE_LOG)[1])
        self.assertEqual(receipt["verdict"], "failed")
        self.assertEqual(calls, [("gitlab.example", PROJECT, 11)])
        self.assertEqual(receipt["failed_job"]["project"], PROJECT)
        self.assertTrue(receipt["diagnosis"]["log_available"])

    def test_trace_read_uses_child_project(self) -> None:
        from testkit import SINGLE_LOG, bridge_entry, run_probe
        calls: list[tuple] = []
        fake = FakeGitlab(
            heads=[SHA1],
            pipelines=[pipe(1, SHA1, "failed")],
            details={1: pipe(1, SHA1, "failed")},
            jobs={1: [job("unit", "success", 11)]},
            bridges={1: [bridge_entry("playwright", "failed", 9)]},
            child_details={9: pipe(9, SHA1, "failed")},
            child_jobs={9: [job("e2e", "failed", 71)]},
            traces={71: SINGLE_LOG})
        receipt = run_probe(
            fake, SHA1, prof(["unit"], [{"name": "playwright",
                                         "required_jobs": ["e2e"]}]),
            read_log=lambda h, p, j: (calls.append((h, p, j)),
                                      SINGLE_LOG)[1])
        self.assertEqual(receipt["verdict"], "failed")
        self.assertEqual(receipt["failed_job"]["name"],
                         "child:playwright/e2e")
        self.assertEqual(calls, [("gitlab.example", 42, 71)])

    def test_pending_and_missing_carry_no_diagnosis(self) -> None:
        from testkit import run_probe
        running = FakeGitlab(heads=[SHA1],
                             pipelines=[pipe(1, SHA1, "running")],
                             details={1: pipe(1, SHA1, "running")},
                             jobs={1: [job("unit", "running", 11)]})
        pending = run_probe(running, SHA1, prof(["unit"]))
        self.assertEqual(pending["verdict"], "pending")
        self.assertNotIn("diagnosis", pending)
        green = FakeGitlab(heads=[SHA1],
                           pipelines=[pipe(1, SHA1, "success")],
                           details={1: pipe(1, SHA1, "success")},
                           jobs={1: [job("unit", "success", 11)]},
                           bridges={1: []})
        missing = run_probe(green, SHA1,
                            prof(["unit"], [{"name": "playwright"}]))
        self.assertEqual(missing["verdict"], "missing_required_check")
        self.assertNotIn("diagnosis", missing)

    def test_failed_carries_stop_diagnosis(self) -> None:
        from testkit import SINGLE_LOG, run_probe
        fake = FakeGitlab(heads=[SHA1],
                          pipelines=[pipe(1, SHA1, "failed")],
                          details={1: pipe(1, SHA1, "failed")},
                          jobs={1: [job("unit", "failed", 11)]},
                          traces={11: SINGLE_LOG})
        receipt = run_probe(fake, SHA1, prof(["unit"]))
        self.assertEqual(receipt["verdict"], "failed")
        self.assertEqual(receipt["diagnosis"]["decision"], "stop")
        self.assertTrue(receipt["diagnosis"]["log_available"])

    def test_bare_child_failure_pins_no_job_id(self) -> None:
        from testkit import SINGLE_LOG, run_probe
        calls: list[tuple] = []
        fake = FakeGitlab(
            heads=[SHA1],
            pipelines=[pipe(1, SHA1, "failed")],
            details={1: pipe(1, SHA1, "failed")},
            jobs={1: [job("unit", "success", 11)]},
            bridges={1: [bridge_entry("playwright", "failed", 9)]},
            child_details={9: pipe(9, SHA1, "failed")},
            child_jobs={9: [job("e2e", "failed", 71)]},
            traces={71: SINGLE_LOG})
        receipt = run_probe(
            fake, SHA1, prof(["unit"], [{"name": "playwright"}]),
            read_log=lambda h, p, j: (calls.append((h, p, j)),
                                      SINGLE_LOG)[1])
        self.assertEqual(receipt["verdict"], "failed")
        self.assertEqual(receipt["failed_job"]["name"],
                         "child:playwright")
        self.assertIsNone(receipt["failed_job"]["id"])
        self.assertEqual(calls, [])
        self.assertNotIn("diagnosis", receipt)

    def test_probe_main_runs_against_fixture_client(self) -> None:
        import json
        import os
        import sys
        import tempfile
        from testkit import (passed_client_fixture, profile_file,
                             write_fixture_client)
        import probe as probe_main
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        client = write_fixture_client(root / "client",
                                      passed_client_fixture())
        env_key = "PIPELINE_OBSERVER_FIXTURE_JSON"
        old = os.environ.get(env_key)
        os.environ[env_key] = str(client / "fixture.json")
        self.addCleanup(lambda: (
            os.environ.pop(env_key, None),
            old is not None and os.environ.__setitem__(env_key, old)))
        profile = profile_file(root)
        out = root / "receipt.json"
        code = probe_main.main([
            "--url", URL, "--profile", str(profile),
            "--expected-sha", SHA1, "--client-dir", str(client),
            "--receipt-out", str(out)])
        self.assertEqual(code, 0)
        receipt = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(receipt["verdict"], "passed")
        self.assertEqual(receipt["expected_sha"], SHA1)


if __name__ == "__main__":
    unittest.main()
