"""Dell transfer/publish/publish-existing on the authoritative claim path.

Each case starts from the real request writer and ends at the real signed
queue: the writer's explicit Lenovo checkout/worktree footprint becomes the
signed reservation, the referee admits or refuses it in the same call that
publishes the job, and only a proof-bound worker release frees it again.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

from managed_claims_fixture import (REQUEST_CLI, ROOT, ManagedClaimsCase,
                                    lan_bridge, run)

sys.path.insert(0, str(ROOT / "scripts" / "lenovo_worker"))
import job_reservations  # noqa: E402  (the queue-side referee read-only)

TOKEN = "0123456789abcdef"
OTHER = "fedcba9876543210"
BASE = "a" * 40


class WriterClaimTests(ManagedClaimsCase):
    def test_a_publish_request_emits_its_own_footprint_claims(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        self.assertEqual(writer["resourceClaims"], [
            f"working-directory:{self.lenovo}",
            f"working-directory:{self.worktree}",
        ])
        self.assertFalse(writer["readOnly"])
        self.assertFalse(self.request_document(writer)["reconcileOnly"])

    def test_a_read_only_reconcile_request_declares_no_claims(self) -> None:
        # The recovery probe writes nothing, so it reserves nothing: it must
        # stay admissible while the unknown job it observes still holds the
        # footprint.
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-reconcile.json",
            reconcile=True, authorize="import,push,mr,reconcile")
        self.assertEqual(writer["resourceClaims"], [])
        self.assertTrue(writer["readOnly"])
        self.assertTrue(self.request_document(writer)["reconcileOnly"])
        job_id = self.submit_ok(writer)
        self.assertEqual(self.job_files(), [f"{job_id}.json"])
        self.assertEqual(self.reservation_files(), [])

    def test_transfer_and_publish_existing_emit_their_footprints(self) -> None:
        for route in ("transfer", "publish-existing"):
            writer, _ = self.write_request(
                route, token=TOKEN, name=f"{route}-request.json")
            self.assertEqual(writer["kind"], f"dell-lan-{route}-request/1")
            self.assertEqual(writer["resourceClaims"], [
                f"working-directory:{self.lenovo}",
                f"working-directory:{self.worktree}",
            ], route)
            self.assertFalse(writer["readOnly"], route)

    def test_a_base_request_is_not_a_managed_write(self) -> None:
        # base/first are Lenovo reads: their unchanged claim-less submit path
        # is not reachable through the managed-write entry point.
        proc = run([
            sys.executable, str(REQUEST_CLI), "base",
            "--shared", str(self.shared), "--token", TOKEN,
            "--lenovo-skill-dir", str(ROOT),
            "--lenovo-driver-dir", str(self.drivers),
            "--python", sys.executable, "--lenovo-checkout", str(self.lenovo),
            "--ref-full", "refs/heads/main",
            "--request-name", "base-request.json",
            "--command-file", str(self.root / "base-command.txt"),
            "--repo", str(self.repo),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        writer = json.loads(proc.stdout)
        self.assertEqual(writer["resourceClaims"], [])
        refused = self.submit(writer)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("managed writes", refused.stderr)
        self.assertEqual(self.job_files(), [])


class SubmitClaimTests(ManagedClaimsCase):
    def test_submit_reserves_and_publishes_in_one_call(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        job_id = self.submit_ok(writer)
        reservation = self.reservation(job_id)
        job_raw = (self.queue / "jobs" / f"{job_id}.json").read_bytes()
        self.assertEqual(reservation["job_envelope_sha256"],
                         hashlib.sha256(job_raw).hexdigest())
        self.assertEqual(reservation["id"], job_id)
        self.assertTrue(set(token.lower() for token in writer["resourceClaims"])
                        <= set(reservation["resources"]))
        self.assertEqual(reservation["resource_digest"],
                         job_reservations.resource_digest(reservation["resources"]))
        self.assertEqual(self.job_payload(job_id)["expected_working_directory"],
                         writer["expectedCwd"])

    def test_a_second_request_for_the_same_footprint_is_refused(self) -> None:
        held = self.submit_ok(self.write_request(
            "publish", token=TOKEN, name="publish-request.json")[0])
        refused = self.submit(self.write_request(
            "publish", token=OTHER, name="publish-request-2.json")[0])
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("reservation refused", refused.stderr)
        self.assertEqual(self.job_files(), [f"{held}.json"])
        self.assertEqual(self.reservation_files(), [f"{held}.json"])

    def test_an_independent_checkout_and_worktree_is_admitted(self) -> None:
        self.submit_ok(self.write_request(
            "publish", token=TOKEN, name="publish-request.json")[0])
        other_checkout = self.root / "lenovo-checkout-2"
        other_checkout.mkdir()
        other_worktree = self.root / "lenovo-task-worktree-2"
        second = self.write_request(
            "publish", token=OTHER, name="publish-request-2.json",
            checkout=other_checkout, worktree=other_worktree)[0]
        job_id = self.submit_ok(second)
        self.assertEqual(len(self.job_files()), 2)
        self.assertEqual(len(self.reservation_files()), 2)
        self.assertIn(f"{job_id}.json", self.reservation_files())

    def test_a_tampered_claim_list_is_refused(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        for field, value in (
                ("resourceClaims", writer["resourceClaims"][:1]),
                ("resourceClaims", writer["resourceClaims"] + [
                    f"working-directory:{self.root / 'elsewhere'}"]),
                ("resourceClaims", []),
                ("readOnly", True)):
            tampered = dict(writer)
            tampered[field] = value
            proc = self.submit(tampered)
            self.assertNotEqual(proc.returncode, 0, (field, value))
            self.assertIn("dell managed submit", proc.stderr)
            self.assertEqual(self.job_files(), [])
            self.assertEqual(self.reservation_files(), [])

    def test_neither_the_request_nor_its_claims_can_be_swapped(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        request_path = Path(writer["requestPath"])
        original = request_path.read_bytes()
        # 1. A rewritten request no longer matches the writer's digest.
        swapped = json.loads(original.decode("utf-8"))
        swapped["lenovoWorktree"] = str(self.root / "other-worktree")
        request_path.write_bytes(json.dumps(
            swapped, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        refused = self.submit(writer)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("digest", refused.stderr)
        # 2. Even with a matching digest, the job cannot keep the old claims.
        raw = request_path.read_bytes()
        rehashed = dict(writer)
        rehashed["requestSha256"] = hashlib.sha256(raw).hexdigest()
        refused = self.submit(rehashed)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("claims do not match", refused.stderr)
        self.assertEqual(self.job_files(), [])
        self.assertEqual(self.reservation_files(), [])


class RecoveryTests(ManagedClaimsCase):
    def test_an_unknown_outcome_holds_the_footprint(self) -> None:
        held = self.submit_ok(self.write_request(
            "publish", token=TOKEN, name="publish-request.json")[0])
        # No result arrives: the outcome is unknown, so the footprint stays
        # held and a second writer for the same target is refused.
        self.assertFalse((self.queue / "results" / f"{held}.json").exists())
        self.assertEqual([record["id"] for record in job_reservations.live_records(
            self.queue, self.key, lan_bridge)], [held])
        refused = self.submit(self.write_request(
            "publish", token=OTHER, name="publish-request-2.json")[0])
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("reservation refused", refused.stderr)
        # The read-only recovery request is still admitted, and it adds no
        # claim of its own.
        recovery = self.write_request(
            "publish", token="0011223344556677", name="publish-reconcile.json",
            reconcile=True, authorize="import,push,mr,reconcile")[0]
        self.submit_ok(recovery)
        self.assertEqual(self.reservation_files(), [f"{held}.json"])
        self.assertEqual(len(self.job_files()), 2)

    def test_a_claim_cannot_be_swapped_onto_another_signed_job(self) -> None:
        # The reservation binds the exact signed job bytes and id; copying it
        # beside another job is refused before that job can claim or run.
        first = self.submit_ok(self.write_request(
            "publish", token=TOKEN, name="publish-request.json")[0])
        other_checkout = self.root / "lenovo-checkout-2"
        other_checkout.mkdir()
        swapped = "5a" * 16
        payload = lan_bridge.build_job_payload(
            job_id=swapped, command="echo swapped",
            working_directory=str(other_checkout), timeout_seconds=5)
        lan_bridge.publish(str(self.queue / "jobs"), swapped,
                           lan_bridge.seal(payload, self.key))
        (self.queue / "reservations" / f"{swapped}.json").write_bytes(
            (self.queue / "reservations" / f"{first}.json").read_bytes())
        completed = self.run_worker()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = lan_bridge.open_envelope(
            lan_bridge.read(str(self.queue / "results"), swapped),
            self.key, "result", swapped)
        self.assertIn("job-unverifiable", result["stderr"])
        self.assertFalse(
            (self.queue / "claims" / f"{swapped}.json").exists())

    def test_a_worker_release_frees_the_footprint_once(self) -> None:
        writer, _ = self.write_request(
            "publish", token=TOKEN, name="publish-request.json")
        job_id = self.submit_ok(writer)
        completed = self.run_worker()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = lan_bridge.open_envelope(
            lan_bridge.read(str(self.queue / "results"), job_id),
            self.key, "result", job_id)
        self.assertEqual(result["status"], "error")
        release = lan_bridge.open_envelope(
            lan_bridge.read(str(self.queue / "releases"), job_id),
            self.key, "release", job_id)
        self.assertEqual(release["id"], job_id)
        self.assertEqual(release["resources"], self.reservation(job_id)["resources"])
        self.assertEqual(job_reservations.live_records(
            self.queue, self.key, lan_bridge), [])
        second = self.write_request(
            "publish", token=OTHER, name="publish-request-2.json")[0]
        self.submit_ok(second)


if __name__ == "__main__":
    unittest.main()
