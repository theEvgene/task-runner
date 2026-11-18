"""Exact same-id recovery after a lost managed-submit response.

The managed job id is a pure function of the verified request digest, so the
producer never scans another job's reservation to guess it and never
re-submits a write to be told it. ``--recover`` reads exactly that id back,
re-authenticates every record it opens, and refuses anything that does not
prove this request's own job.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from managed_claims_fixture import ROOT, ManagedClaimsCase, lan_bridge

sys.path.insert(0, str(ROOT / "scripts" / "dell_lan"))
from dell_managed_job import managed_job_id  # noqa: E402

TOKEN = "0123456789abcdef"
OTHER = "fedcba9876543210"
DIGEST = "a" * 64


class ManagedJobIdTests(unittest.TestCase):
    def test_the_id_is_deterministic_and_domain_separated(self) -> None:
        job_id = managed_job_id(DIGEST)
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self.assertEqual(job_id, managed_job_id(DIGEST))
        self.assertNotEqual(job_id, managed_job_id("b" * 64))

    def test_anything_but_one_lowercase_digest_is_refused(self) -> None:
        for value in ("", "A" * 64, "a" * 63, "a" * 65, None, 64):
            with self.assertRaises(ValueError):
                managed_job_id(value)


class LostResponseTests(ManagedClaimsCase):
    def recover_state(self, writer: dict) -> dict:
        recovered = self.recover(writer)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        return json.loads(recovered.stdout)

    def test_a_lost_submit_response_leaves_the_exact_id_recoverable(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        expected = managed_job_id(writer["requestSha256"])
        # The job publish lands; only the response is lost.
        lost = self.submit(writer, bridge_dir=self.lost_response_bridge())
        self.assertEqual(lost.returncode, 1, lost.stderr)
        self.assertIn("simulated lost submit response", lost.stderr)
        self.assertNotIn("JOBID=", lost.stdout)
        self.assertEqual(self.job_files(), [f"{expected}.json"])
        self.assertEqual(self.reservation_files(), [f"{expected}.json"])
        # One read-only readback names the exact id; nothing is published.
        state = self.recover_state(writer)
        self.assertEqual(state["jobId"], expected)
        self.assertEqual(state["job"], "published")
        self.assertEqual(state["reservation"], "present")
        self.assertTrue(state["footprintHeld"])
        self.assertEqual(state["result"], "absent")
        self.assertEqual(self.job_files(), [f"{expected}.json"])
        self.assertEqual(self.reservation_files(), [f"{expected}.json"])

    def test_recovery_never_names_another_requests_held_job(self) -> None:
        held = self.submit_ok(self.write_request(
            "publish", token=TOKEN, name="publish-request.json")[0])
        second, _ = self.write_request(
            "publish", token=OTHER, name="publish-request-2.json")
        refused = self.submit(second)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.job_files(), [f"{held}.json"])
        # The losing request has no job of its own: the held record is not
        # evidence about it, so recovery reports absence instead of guessing.
        state = self.recover_state(second)
        self.assertEqual(state["jobId"], managed_job_id(second["requestSha256"]))
        self.assertNotEqual(state["jobId"], held)
        self.assertEqual(state["job"], "absent")
        self.assertEqual(state["reservation"], "absent")
        self.assertFalse(state["footprintHeld"])

    def test_a_job_under_the_id_that_is_not_this_request_is_refused(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        expected = managed_job_id(writer["requestSha256"])
        payload = lan_bridge.build_job_payload(
            job_id=expected, command="echo another-command",
            working_directory=str(self.worktree), timeout_seconds=5)
        lan_bridge.publish(str(self.queue / "jobs"), expected,
                           lan_bridge.seal(payload, self.key))
        recovered = self.recover(writer)
        self.assertEqual(recovered.returncode, 1, recovered.stdout)
        self.assertIn("not this request's command", recovered.stderr)
        self.assertEqual(recovered.stdout, "")

    def test_recovery_before_any_submit_creates_nothing(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        state = self.recover_state(writer)
        self.assertEqual(state["job"], "absent")
        self.assertFalse(state["footprintHeld"])
        self.assertEqual(self.job_files(), [])
        self.assertEqual(self.reservation_files(), [])

    def test_the_same_request_twice_keeps_one_job_and_one_claim(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        first = self.submit_ok(writer)
        second = self.submit_ok(writer)
        self.assertEqual(first, managed_job_id(writer["requestSha256"]))
        self.assertEqual(second, first)
        self.assertEqual(self.job_files(), [f"{first}.json"])
        self.assertEqual(self.reservation_files(), [f"{first}.json"])

    def test_recovery_after_the_worker_release_keeps_the_id(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        job_id = self.submit_ok(writer)
        completed = self.run_worker()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = self.recover_state(writer)
        self.assertEqual(state["jobId"], job_id)
        self.assertEqual(state["job"], "published")
        self.assertEqual(state["result"], "present")
        self.assertFalse(state["footprintHeld"])

    def test_a_read_only_probe_has_its_own_recoverable_id(self) -> None:
        writer, _ = self.write_request(
            "publish", token="0011223344556677",
            name="publish-reconcile.json", reconcile=True,
            authorize="import,push,mr,reconcile")
        job_id = self.submit_ok(writer)
        self.assertEqual(job_id, managed_job_id(writer["requestSha256"]))
        state = self.recover_state(writer)
        self.assertEqual(state["job"], "published")
        self.assertEqual(state["reservation"], "absent")
        self.assertFalse(state["footprintHeld"])


if __name__ == "__main__":
    unittest.main()
