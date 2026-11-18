"""The executed command is bound to the verified request, not to the record.

Each case starts at the real request writer and ends at the real signed queue.
The reviewed defect was that a verified request could be published with a
different command file, so a write claim could be re-pointed and a read-only
request could run a real write. Both reproductions are repeated here, plus the
sealed-command identity, the parameter binding, a two-process producer
conflict, and the same-id recovery handle.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from managed_claims_fixture import ROOT, SCRIPTS, ManagedClaimsCase, lan_bridge

sys.path.insert(0, str(ROOT / "scripts" / "lenovo_worker"))
sys.path.insert(0, str(SCRIPTS))
import dell_managed_command  # noqa: E402
import job_reservations  # noqa: E402

TOKEN = "0123456789abcdef"
OTHER = "fedcba9876543210"
PROBE = "0011223344556677"


class CommandBindingCase(ManagedClaimsCase):
    """One writer target plus the shared refusal assertions."""

    def derived(self, writer: dict) -> str:
        command, _ = dell_managed_command.expected_command(
            self.request_document(writer), writer["requestSha256"])
        return command

    def assert_refused(self, proc: subprocess.CompletedProcess,
                       needle: str) -> None:
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("dell managed submit", proc.stderr)
        self.assertIn(needle, proc.stderr)
        self.assertEqual(self.job_files(), [])
        self.assertEqual(self.reservation_files(), [])

    def write(self, route: str = "publish", command: str = "command.txt",
              **kwargs) -> dict:
        writer, _ = self.write_request(
            route, command_file=self.root / command, **kwargs)
        return writer


class CommandSwapTests(CommandBindingCase):
    def test_a_command_for_another_request_is_refused(self) -> None:
        # Reviewed reproduction 1: request A stays verified while command B,
        # which names request B's path and digest, is executed.
        first = self.write(token=TOKEN, name="request-a.json",
                           command="command-a.txt")
        other_checkout = self.root / "lenovo-checkout-b"
        other_checkout.mkdir()
        second = self.write(token=OTHER, name="request-b.json",
                            command="command-b.txt",
                            checkout=other_checkout,
                            worktree=self.root / "lenovo-worktree-b")
        swapped = dict(first)
        swapped["commandFile"] = second["commandFile"]
        self.assert_refused(self.submit(swapped), "command file")

    def test_a_read_only_request_cannot_execute_a_write_command(self) -> None:
        # Reviewed reproduction 2: a verified read-only request publishes no
        # claim, so pairing it with a write command must refuse before
        # publication rather than publish an unclaimed write.
        probe = self.write(token=TOKEN, name="request-reconcile.json",
                           command="command-reconcile.txt", reconcile=True,
                           authorize="import,push,mr,reconcile")
        self.assertTrue(probe["readOnly"])
        write = self.write(token=OTHER, name="request-write.json",
                           command="command-write.txt")
        swapped = dict(probe)
        swapped["commandFile"] = write["commandFile"]
        self.assert_refused(self.submit(swapped), "command file")

    def test_a_write_request_cannot_execute_a_reconcile_command(self) -> None:
        write = self.write(token=TOKEN, name="request-write.json",
                           command="command-write.txt")
        probe = self.write(token=OTHER, name="request-reconcile.json",
                           command="command-reconcile.txt", reconcile=True,
                           authorize="import,push,mr,reconcile")
        swapped = dict(write)
        swapped["commandFile"] = probe["commandFile"]
        self.assert_refused(self.submit(swapped), "command file")
        # The write record's own pre-rendered probe is not submittable either:
        # its request is a write request, so it must not execute unclaimed.
        probe_file = self.root / "command-from-record.txt"
        probe_file.write_text(write["reconcileCommand"], encoding="utf-8")
        swapped = dict(write)
        swapped["commandFile"] = str(probe_file)
        self.assert_refused(self.submit(swapped), "command file")

    def test_a_command_file_cannot_add_a_parameter(self) -> None:
        writer = self.write(token=TOKEN, name="request.json")
        command_file = Path(writer["commandFile"])
        command_file.write_text(
            command_file.read_text(encoding="utf-8") + " --reconcile",
            encoding="utf-8")
        self.assert_refused(self.submit(writer), "command file")


class ParameterBindingTests(CommandBindingCase):
    def test_the_sealed_command_is_the_request_derived_one(self) -> None:
        writer = self.write(token=TOKEN, name="request.json")
        job_id = self.submit_ok(writer)
        payload = self.job_payload(job_id)
        expected = self.derived(writer)
        self.assertEqual(payload["command"], expected)
        self.assertEqual(payload["working_directory"], writer["expectedCwd"])
        self.assertIn(writer["requestPath"], expected)
        self.assertIn(writer["requestSha256"], expected)
        self.assertNotIn("--reconcile", expected)
        self.assertEqual(len(self.reservation(job_id)["resources"]), 2)

    def test_a_parameter_that_disagrees_with_the_request_is_refused(self) -> None:
        writer = self.write(token=TOKEN, name="request.json")
        for field, value in (
                ("kind", "dell-lan-transfer-request/1"),
                ("dialect", "powershell"),
                ("receiptScriptPath", str(self.drivers / "other_driver.py")),
                ("expectedCwd", str(self.root / "elsewhere"))):
            tampered = dict(writer)
            tampered[field] = value
            self.assert_refused(
                self.submit(tampered), "disagrees with the verified request")

    def test_a_request_without_the_execution_block_is_refused(self) -> None:
        # Without the authenticated block the submit cannot derive a command,
        # so it refuses instead of falling back to the caller's record.
        writer = self.write(token=TOKEN, name="request.json")
        path = Path(writer["requestPath"])
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("execution")
        raw = json.dumps(document, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8") + b"\n"
        path.write_bytes(raw)
        rehashed = dict(writer)
        rehashed["requestSha256"] = hashlib.sha256(raw).hexdigest()
        self.assert_refused(self.submit(rehashed), "execution block")


class ProducerRecoveryTests(CommandBindingCase):
    def test_two_real_producers_for_one_footprint_publish_exactly_one(self) -> None:
        first = self.write(token=TOKEN, name="request-a.json",
                           command="command-a.txt")
        second = self.write(token=OTHER, name="request-b.json",
                            command="command-b.txt")
        processes = [
            subprocess.Popen(self.submit_command(writer),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
            for writer in (first, second)]
        results = [process.communicate(timeout=120) for process in processes]
        codes = sorted(process.returncode for process in processes)
        self.assertEqual(codes, [0, 1], (results, codes))
        self.assertEqual(len(self.job_files()), 1, results)
        self.assertEqual(len(self.reservation_files()), 1, results)
        loser = [stderr for process, (_, stderr)
                 in zip(processes, results) if process.returncode != 0]
        self.assertIn("reservation refused", loser[0])

    def test_the_unknown_job_id_stays_the_recovery_handle(self) -> None:
        writer = self.write(token=TOKEN, name="request.json")
        job_id = self.submit_ok(writer)
        # No result arrived: the outcome is unknown, so the same id stays
        # readable and keeps owning the footprint.
        self.assertEqual(self.job_payload(job_id)["command"],
                         self.derived(writer))
        self.assertEqual([record["id"] for record in job_reservations.live_records(
            self.queue, self.key, lan_bridge)], [job_id])
        refused = self.submit(self.write(
            token=OTHER, name="request-2.json", command="command-2.txt"))
        self.assertIn("reservation refused", refused.stderr)
        self.assertEqual(self.job_files(), [f"{job_id}.json"])
        # The read-only probe is admitted beside the held footprint under its
        # own id, and it never takes over the original reservation.
        probe_id = self.submit_ok(self.write(
            token=PROBE, name="request-reconcile.json",
            command="command-reconcile.txt", reconcile=True,
            authorize="import,push,mr,reconcile"))
        self.assertNotEqual(probe_id, job_id)
        self.assertEqual(self.reservation_files(), [f"{job_id}.json"])
        self.assertIn("--reconcile", self.job_payload(probe_id)["command"])


if __name__ == "__main__":
    unittest.main()
