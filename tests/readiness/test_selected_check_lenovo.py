"""Fast fake-worker fixtures for the Lenovo selected check and its receipts."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from selected_lenovo_fixture import (  # noqa: E402
    LenovoSelectedFixture, bridge, readiness, selected, selected_hosts)


class SelectedCheckLenovoTests(LenovoSelectedFixture):
    def test_lenovo_positive_is_one_submit_bound_to_the_evidence(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head})
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertEqual(receipt["requestedHost"], "lenovo")
        self.assertEqual(receipt["observedHost"], "lenovo")
        self.assertTrue(receipt["hostBound"])
        self.assertEqual(receipt["hostEvidence"], "signed-queue-result")
        self.assertEqual(receipt["revision"]["state"], "confirmed")
        self.assertEqual(receipt["revision"]["source"],
                         "signed-target-preamble")
        self.assertEqual(receipt["preflight"],
                         {"state": "confirmed", "reason": "confirmed"})
        self.assertEqual(receipt["execution"]["kind"], "signed-queue")
        self.assertRegex(self._check_job_id(receipt), r"^[0-9a-f]{32}$")
        self.assertEqual(len(self._job_ids()), 2, "exactly one check submit")
        self.assertNotIn(str(self.temp), proc.stdout + proc.stderr)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)
        job = bridge.open_envelope(
            bridge.read(self.queue / "jobs", self._check_job_id(receipt)),
            self.key, "job", self._check_job_id(receipt))
        self.assertEqual(job["working_directory"], str(self.target))
        self.assertEqual(job["dialect"], "cmd")
        self.assertNotIn("SystemExit(0)", job["command"])

    def test_lenovo_readiness_evidence_revision_is_verified(self) -> None:
        payload = bridge.fetch_result(
            str(self.queue), self.key, self.evidence_job,
            timeout_seconds=1, poll_seconds=0.01)
        components = readiness.worker_components(payload, self.config)
        checkout = next(item for item in components
                        if item["name"] == "checkout")
        self.assertEqual(checkout["state"], "confirmed")
        self.assertEqual(checkout["revisionEvidence"], "verified")
        self.assertEqual(checkout["observedRevision"], self.head)

    def test_lenovo_target_preamble_not_run_is_not_pass(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "NOT_RUN",
            "reason": "revision-stale", "exitCode": None,
            "observedRevision": "0" * 40}, status="error", exit_code=78)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["observedHost"], "lenovo")
        self.assertTrue(receipt["hostBound"])
        self.assertEqual(receipt["revision"]["state"], "stale")
        self.assertEqual(receipt["preflight"]["state"], "confirmed")
        self.assertIsNone(receipt["execution"]["exitCode"])
        self.assertEqual(len(self._job_ids()), 2)

    def test_lenovo_nonzero_exit_is_fail(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 9,
            "observedRevision": self.head}, status="error", exit_code=9)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "FAIL")
        self.assertEqual(receipt["execution"]["exitCode"], 9)

    def test_lenovo_worker_timeout_is_unknown_and_not_pass(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head}, status="error", exit_code=None,
            timed_out=True)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertFalse(receipt["execution"]["verified"])
        self.assertTrue(receipt["execution"]["timedOut"])

    def test_lenovo_missing_result_is_unknown_and_never_resubmits(self) -> None:
        self.descriptor["lenovo"]["waitSeconds"] = 0.5
        proc = self._run()
        self.assertEqual(proc.returncode, 3, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertIsNone(receipt["observedHost"])
        self.assertFalse(receipt["hostBound"])
        job_id = self._check_job_id(receipt)
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self.assertIn(job_id, receipt["nextAction"])
        self.assertEqual(len(self._job_ids()), 2, "unknown must not resubmit")

    def test_invalid_result_preserves_job_id_and_requests_owner_inspection(self) -> None:
        job_id = "c" * 32
        execution = selected_hosts.lenovo_result(job_id, {}, self.head)
        self.assertEqual(execution["jobId"], job_id)
        self.assertEqual(execution["state"], "unknown")
        action = selected._next_action("UNKNOWN", {}, execution)
        self.assertIn(job_id, action)
        self.assertIn("owner", action)
        self.assertIn("never resubmit", action)
        execution["reason"] = "worker-refused"
        self.assertIn("owner", selected._next_action("UNKNOWN", {}, execution))

    def test_lenovo_run_receipt_with_a_foreign_revision_is_not_pass(self) -> None:
        payload = bridge.build_result_payload(
            "c" * 32, "completed", exit_code=0,
            stdout=json.dumps({"schema": "lenovo-selected-check/1",
                               "state": "RUN", "reason": "completed",
                               "exitCode": 0,
                               "observedRevision": "0" * 40},
                              separators=(",", ":")))
        execution = selected_hosts.lenovo_result("c" * 32, payload, self.head)
        self.assertEqual(execution["revisionState"], "stale")
        self.assertEqual(execution["state"], "not_run")
        self.assertIsNone(execution["exitCode"])
        self.assertEqual(
            selected.decide("lenovo", selected_hosts._confirmed(self.head),
                            execution),
            "NOT_RUN")

    def test_unknown_result_recovers_by_same_job_id_without_resubmitting(
            self) -> None:
        self.descriptor["lenovo"]["waitSeconds"] = 0.5
        first = self._run()
        self.assertEqual(first.returncode, 3, first.stderr)
        receipt = self._receipt(first)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        job_id = self._check_job_id(receipt)
        self.assertEqual(len(self._job_ids()), 2)
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head}, job_id=job_id)
        second = self._run(extra=("--job-id", job_id, "--wait", "15"))
        worker.join(20)
        self.assertEqual(second.returncode, 0, second.stderr)
        receipt = self._receipt(second)
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertEqual(receipt["execution"]["jobId"], job_id)
        self.assertEqual(len(self._job_ids()), 2,
                         "same-id recovery submitted another job")

    def test_unverifiable_result_keeps_the_job_id_in_the_receipt(self) -> None:
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head}, tamper=True)
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertFalse(receipt["execution"]["verified"])
        self.assertFalse(receipt["hostBound"])
        job_id = self._check_job_id(receipt)
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self.assertIn(job_id, receipt["nextAction"])
        self.assertEqual(len(self._job_ids()), 2)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    def test_lenovo_evidence_refusals_publish_nothing(self) -> None:
        mismatch = json.loads(json.dumps(self.descriptor))
        mismatch["check"]["cwd"] = str(self.temp / "other-target")
        worker_missing = json.loads(json.dumps(self.descriptor))
        worker_missing["lenovo"]["readinessJobId"] = "f" * 32
        tool_mismatch = json.loads(json.dumps(self.descriptor))
        tool_mismatch["check"]["argv"][0] = "other-python"
        for data, reason in ((mismatch, "cwd-unproven"),
                             (worker_missing, "evidence-unbound"),
                             (tool_mismatch, "tool-unproven")):
            with self.subTest(reason=reason):
                proc = self._run(data)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                receipt = self._receipt(proc)
                self.assertEqual(receipt["outcome"], "NOT_RUN")
                self.assertEqual(receipt["preflight"]["reason"], reason)
                self.assertEqual(len(self._job_ids()), 1,
                                 "a refused preflight submitted a check job")

    def test_lenovo_evidence_revision_mismatch_refuses_before_submit(self) -> None:
        self._republish_evidence(revision="0" * 40)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["preflight"]["reason"], "revision-stale")
        self.assertEqual(receipt["revision"]["state"], "stale")
        self.assertEqual(receipt["revision"]["observed"], "0" * 40)
        self.assertEqual(receipt["revision"]["source"], "readiness-evidence")
        self.assertIsNone(receipt["execution"]["exitCode"])
        self.assertEqual(len(self._job_ids()), 1,
                         "a stale target revision submitted a check job")

    def test_lenovo_evidence_missing_revision_refuses_before_submit(self) -> None:
        self._republish_evidence(revision=None)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["preflight"]["reason"], "revision-unconfirmed")
        self.assertEqual(receipt["revision"]["state"], "unconfirmed")
        self.assertEqual(receipt["revision"]["source"], "readiness-evidence")
        self.assertIsNone(receipt["execution"]["exitCode"])
        self.assertEqual(len(self._job_ids()), 1,
                         "missing target revision submitted a check job")

    def test_lenovo_legacy_evidence_is_not_verified(self) -> None:
        self._republish_evidence(schema="lenovo-readiness-probe/1")
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["preflight"]["reason"], "evidence-untrusted")
        self.assertEqual(receipt["revision"]["state"], "unconfirmed")
        self.assertEqual(receipt["revision"]["source"], "readiness-evidence")
        self.assertEqual(len(self._job_ids()), 1,
                         "legacy evidence submitted a check job")

    def test_lenovo_untrusted_cwd_binding_is_not_verified(self) -> None:
        for kwargs in ({"cwd_digest": "f" * 64},
                       {"revision": "not-a-sha"}):
            with self.subTest(kwargs=kwargs):
                self._republish_evidence(**kwargs)
                proc = self._run()
                self.assertEqual(proc.returncode, 1, proc.stderr)
                receipt = self._receipt(proc)
                self.assertEqual(receipt["outcome"], "NOT_RUN")
                self.assertEqual(receipt["preflight"]["reason"],
                                 "evidence-untrusted")
                self.assertEqual(len(self._job_ids()), 1,
                                 "untrusted evidence submitted a check job")

    def test_lenovo_missing_or_tampered_evidence_never_passes(self) -> None:
        (self.queue / "results" / f"{self.evidence_job}.json").unlink()
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self._receipt(proc)["preflight"]["reason"],
                         "worker-unproven")
        self.assertEqual(len(self._job_ids()), 1)
        self._publish_evidence(tamper=True)
        proc = self._run()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("bad mac", proc.stderr)
        self.assertEqual(len(self._job_ids()), 1)


if __name__ == "__main__":
    unittest.main()
