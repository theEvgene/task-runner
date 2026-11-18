"""Exact same-id recovery after a lost managed-submit response.

The managed job id is a pure function of the verified request digest, so the
producer never scans another job's reservation to guess it and never
re-submits a write to be told it. ``--recover`` reads exactly that id back,
re-authenticates every record it opens, and refuses anything that does not
prove this request's own job. An unavailable queue is a refusal, not an
absence; a reservation whose job never landed is unresolved, not free; and a
job under the id must match every request-derived execution field.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from managed_claims_fixture import (
    MANAGED_SUBMIT, ROOT, ManagedClaimsCase, lan_bridge, run,
)

sys.path.insert(0, str(ROOT / "scripts" / "dell_lan"))
sys.path.insert(0, str(ROOT / "scripts" / "lenovo_worker"))
from dell_managed_claims import verify_writer_output  # noqa: E402
from dell_managed_command import expected_command  # noqa: E402
from dell_managed_job import managed_job_id  # noqa: E402
import job_reservation_claims  # noqa: E402

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


class RecoveryCase(ManagedClaimsCase):
    """The shared harness plus the one asserted read-only readback."""

    def recover_state(self, writer: dict) -> dict:
        recovered = self.recover(writer)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        return json.loads(recovered.stdout)


class LostResponseTests(RecoveryCase):
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
        self.assertIsNone(state["jobEnvelopeSha256"])
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


class QueueAvailabilityTests(ManagedClaimsCase):
    def test_an_unavailable_queue_is_refused_not_reported_absent(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        cases = (("absent", self.root / "queue-not-mounted"), ("jobs", self.queue))
        for label, queue in cases:
            with self.subTest(label):
                if label == "jobs":
                    shutil.rmtree(self.queue / "jobs", ignore_errors=True)
                output = self.root / f"writer-{label}.json"
                output.write_text(json.dumps(writer), encoding="utf-8")
                refused = run([
                    sys.executable, str(MANAGED_SUBMIT),
                    "--writer-output", str(output), "--queue", str(queue),
                    "--key-file", str(self.key_file), "--repo", str(self.repo),
                    "--recover"])
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("queue-missing", refused.stderr)
                self.assertEqual(refused.stdout, "")


class ReservationCrashWindowTests(RecoveryCase):
    def test_reservation_only_is_unresolved_and_the_same_resubmit_resolves_it(
            self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        job_id = self.submit_ok(writer)
        # The crash window: the reservation landed, the job never did.
        (self.queue / "jobs" / f"{job_id}.json").unlink()
        state = self.recover_state(writer)
        self.assertEqual(state["job"], "absent")
        self.assertEqual(state["reservation"], "reservation-only")
        self.assertIsNone(state["footprintHeld"])
        self.assertEqual(self.job_files(), [])
        # The documented retry resolves the orphan under the referee lock.
        self.assertEqual(self.submit_ok(writer), job_id)
        self.assertEqual(self.job_files(), [f"{job_id}.json"])
        self.assertEqual(self.reservation_files(), [f"{job_id}.json"])
        state = self.recover_state(writer)
        self.assertEqual(state["job"], "published")
        self.assertEqual(state["reservation"], "present")
        self.assertTrue(state["footprintHeld"])

    def test_a_foreign_reservation_is_not_released_or_replaced(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        request = self.request_document(writer)
        claims = verify_writer_output(writer, request)
        job_id = managed_job_id(writer["requestSha256"])
        record = job_reservation_claims.build(
            job_id, "0" * 64, claims,
            "working-directory:" + request["execution"]["expectedCwd"],
            "foreign-producer", 4242, None,
            lambda: "2026-11-18T00:00:00+00:00")
        lan_bridge.publish(str(self.queue / "reservations"), job_id,
                           lan_bridge.seal(record, self.key))
        refused = self.submit(writer)
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("inert reservation", refused.stderr)
        self.assertEqual(self.job_files(), [])
        self.assertEqual(self.reservation_files(), [f"{job_id}.json"])
        self.assertEqual(self.reservation(job_id)["owner"]["instance"],
                         "foreign-producer")

    def test_an_inert_reservation_with_a_different_footprint_is_refused(
            self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        job_id = self.submit_ok(writer)
        digest = hashlib.sha256(lan_bridge.read(
            str(self.queue / "jobs"), job_id)).hexdigest()
        (self.queue / "jobs" / f"{job_id}.json").unlink()
        (self.queue / "reservations" / f"{job_id}.json").unlink()
        record = job_reservation_claims.build(
            job_id, digest, [f"file:{self.root / 'foreign.out'}"],
            "working-directory:" + str(self.worktree),
            "foreign-producer", 4242, None,
            lambda: "2026-11-18T00:00:00+00:00")
        lan_bridge.publish(str(self.queue / "reservations"), job_id,
                           lan_bridge.seal(record, self.key))
        refused = self.submit(writer)
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("different footprint", refused.stderr)
        self.assertEqual(self.job_files(), [])
        self.assertEqual(self.reservation_files(), [f"{job_id}.json"])

    def test_two_simultaneous_resubmits_resolve_one_inert_reservation(
            self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        job_id = self.submit_ok(writer)
        (self.queue / "jobs" / f"{job_id}.json").unlink()
        commands = [self.submit_command(writer), self.submit_command(writer)]
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
            for command in commands]
        results = [process.communicate(timeout=60) for process in processes]
        self.assertEqual([process.returncode for process in processes],
                         [0, 0], results)
        ids = {stdout.strip().split("JOBID=", 1)[1].strip()
               for stdout, _ in results}
        self.assertEqual(ids, {job_id}, results)
        self.assertEqual(self.job_files(), [f"{job_id}.json"])
        self.assertEqual(self.reservation_files(), [f"{job_id}.json"])


class ExecutionBindingTests(RecoveryCase):
    def test_recovery_refuses_a_job_with_a_different_working_directory(
            self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        request = self.request_document(writer)
        command, parameters = expected_command(request, writer["requestSha256"])
        other = str(self.root / "elsewhere")
        Path(other).mkdir()
        claims = verify_writer_output(writer, request)
        job_id = lan_bridge.submit_job(
            str(self.queue), self.key, command, other, 300, 65536, None,
            parameters["dialect"], parameters["driverPath"],
            resource_claims=claims, expected_working_directory=other,
            job_id=managed_job_id(writer["requestSha256"]))
        self.assertEqual(self.job_payload(job_id)["working_directory"], other)
        recovered = self.recover(writer)
        self.assertEqual(recovered.returncode, 1, recovered.stdout)
        self.assertIn("working directory", recovered.stderr)
        self.assertEqual(recovered.stdout, "")

    def test_recovery_binds_every_request_derived_execution_field(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        request = self.request_document(writer)
        command, parameters = expected_command(request, writer["requestSha256"])
        job_id = managed_job_id(writer["requestSha256"])
        elsewhere = str(self.root / "elsewhere")
        return_file = parameters["returnFile"]
        base = {
            "job_id": job_id, "command": command,
            "working_directory": parameters["expectedCwd"],
            "timeout_seconds": 300, "max_output_bytes": 65536,
            "return_files": [return_file] if return_file else [],
            "dialect": parameters["dialect"],
            "receipt_script_path": parameters["driverPath"],
            "expected_working_directory": parameters["expectedCwd"],
        }
        variants = {
            "working directory": {"working_directory": elsewhere},
            "expected working directory": {
                "expected_working_directory": elsewhere},
            "dialect": {"dialect": "powershell"},
            "receipt script path": {
                "receipt_script_path": str(self.root / "other" / "driver.py")},
            "return files": {"return_files": [str(self.root / "other.out")]},
        }
        for label, override in variants.items():
            with self.subTest(label):
                job = dict(base)
                job.update(override)
                path = self.queue / "jobs" / f"{job_id}.json"
                path.unlink(missing_ok=True)
                lan_bridge.publish(
                    str(self.queue / "jobs"), job_id,
                    lan_bridge.seal(lan_bridge.build_job_payload(**job),
                                    self.key))
                recovered = self.recover(writer)
                self.assertEqual(recovered.returncode, 1, recovered.stdout)
                self.assertIn(label, recovered.stderr)
                self.assertEqual(recovered.stdout, "")

    def test_a_result_without_its_job_is_a_refused_contradiction(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        job_id = managed_job_id(writer["requestSha256"])
        raw = lan_bridge.seal(
            lan_bridge.build_result_payload(job_id, "ok", 0), self.key)
        lan_bridge.publish(str(self.queue / "results"), job_id, raw)
        recovered = self.recover(writer)
        self.assertEqual(recovered.returncode, 1, recovered.stdout)
        self.assertIn("contradictory", recovered.stderr)
        self.assertEqual(recovered.stdout, "")


if __name__ == "__main__":
    unittest.main()
