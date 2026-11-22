"""Derived signed-job timeout cap and legacy same-id readback (#168).

New submissions derive one signed job budget from the check budget: the check
value plus a 60 s stop window, capped at the 600 s execution contract. A
genuinely old signed job carries the pre-cap value (601..660 for a 541..600 s
check) and must still be readable by exact id, while any other timeout or any
differently shaped job stays `job-unbound`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from selected_lenovo_fixture import (  # noqa: E402
    LenovoSelectedFixture, bridge, selected_hosts)


class SelectedJobTimeoutCapTests(LenovoSelectedFixture):
    def test_job_timeout_caps_at_600_and_keeps_smaller_values(self) -> None:
        """541..600 must not derive an unsubmittable 601..660 (#168)."""
        self.assertEqual(
            selected_hosts._job_timeouts({"timeoutSeconds": 540}), (600,))
        self.assertEqual(
            selected_hosts._job_timeouts({"timeoutSeconds": 541}), (600, 601))
        self.assertEqual(
            selected_hosts._job_timeouts({"timeoutSeconds": 599}), (600, 659))
        self.assertEqual(
            selected_hosts._job_timeouts({"timeoutSeconds": 600}), (600, 660))
        for seconds in range(1, 601):
            with self.subTest(timeoutSeconds=seconds):
                derived = selected_hosts._job_timeout(
                    {"timeoutSeconds": seconds})
                self.assertEqual(derived, min(600, seconds + 60))
                self.assertLessEqual(derived, 600)
                built = bridge.build_job_payload(
                    job_id="a" * 32, command="echo hi",
                    working_directory="C:\\work",
                    timeout_seconds=derived)
                self.assertEqual(built["timeout_seconds"], derived)

    def test_lenovo_check_timeout_541_submits_exactly_one_600_job(self) -> None:
        self.descriptor["check"]["timeoutSeconds"] = 541
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head})
        proc = self._run()
        worker.join(20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "PASS")
        job_id = self._check_job_id(receipt)
        self.assertEqual(len(self._job_ids()), 2, "exactly one check submit")
        job = bridge.open_envelope(
            bridge.read(self.queue / "jobs", job_id), self.key, "job", job_id)
        self.assertEqual(job["timeout_seconds"], 600)
        self.assertEqual(job["command"],
                         selected_hosts.remote_command(self.descriptor))

    def test_lenovo_legacy_signed_job_is_bound_by_exact_id(self) -> None:
        """Old producers sealed timeout+60 uncapped: 601..660 stay readable."""
        for seconds in (541, 560, 599, 600):
            with self.subTest(timeoutSeconds=seconds):
                descriptor = self._descriptor_copy(seconds)
                job_id = format(0x6000 + seconds, "032x")
                self._seal_signed_job(job_id, seconds + 60,
                                      descriptor=descriptor)
                preflight, key = self._resume(descriptor, job_id)
                self.assertEqual(preflight["state"], "confirmed", preflight)
                self.assertIsNotNone(key)

    def test_lenovo_mismatched_signed_jobs_are_refused(self) -> None:
        descriptor = self._descriptor_copy(560)  # current 600, legacy 620
        command = selected_hosts.remote_command(descriptor)
        cases = [
            ("timeout-599", {"timeout_seconds": 599}),
            ("timeout-601", {"timeout_seconds": 601}),
            ("timeout-619", {"timeout_seconds": 619}),
            ("timeout-621", {"timeout_seconds": 621}),
            ("current-600-other-command", {"timeout_seconds": 600,
                                            "command": command + " "}),
            ("legacy-other-command", {"timeout_seconds": 620,
                                      "command": command + " "}),
            ("legacy-other-cwd", {"timeout_seconds": 620,
                                  "cwd": str(self.temp)}),
            ("legacy-other-output", {"timeout_seconds": 620,
                                     "max_output_bytes": 1024}),
        ]
        for index, (label, overrides) in enumerate(cases):
            with self.subTest(case=label):
                fields = dict(overrides)
                timeout_seconds = fields.pop("timeout_seconds")
                job_id = format(0x6100 + index, "032x")
                self._seal_signed_job(job_id, timeout_seconds,
                                      descriptor=descriptor, **fields)
                preflight, key = self._resume(descriptor, job_id)
                self.assertEqual(preflight["state"], "refused", preflight)
                self.assertEqual(preflight["reason"], "job-unbound")
                self.assertIsNotNone(key)

    def test_lenovo_smaller_check_timeout_binds_only_its_own_job(self) -> None:
        descriptor = self._descriptor_copy(30)
        accepted = format(0x6200, "032x")
        self._seal_signed_job(accepted, 90, descriptor=descriptor)
        self.assertEqual(self._resume(descriptor, accepted)[0]["state"],
                         "confirmed")
        for index, signed in enumerate((89, 91, 150, 600)):
            with self.subTest(signed=signed):
                job_id = format(0x6201 + index, "032x")
                self._seal_signed_job(job_id, signed, descriptor=descriptor)
                preflight, _ = self._resume(descriptor, job_id)
                self.assertEqual(preflight["state"], "refused", preflight)
                self.assertEqual(preflight["reason"], "job-unbound")

    def test_lenovo_legacy_job_is_resumed_without_a_second_submit(self) -> None:
        self.descriptor["check"]["timeoutSeconds"] = 560
        legacy_id = format(0x6300, "032x")
        self._seal_signed_job(legacy_id, 620)
        worker = self._fake_worker({
            "schema": "lenovo-selected-check/1", "state": "RUN",
            "reason": "completed", "exitCode": 0,
            "observedRevision": self.head}, job_id=legacy_id)
        proc = self._run(extra=("--job-id", legacy_id, "--wait", "15"))
        worker.join(20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = self._receipt(proc)
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertEqual(receipt["execution"]["jobId"], legacy_id)
        self.assertEqual(len(self._job_ids()), 2,
                         "legacy same-id recovery submitted another job")


if __name__ == "__main__":
    unittest.main()
