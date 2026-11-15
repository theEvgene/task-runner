"""Referee contracts: canonical footprints, live-lock authority, release proof.

Real files, real operating-system locks, real subprocesses; no live host and
no live queue. Each test is one independent-review reproduction:

1. TCP wildcard and alias endpoints name one effective port, so they must
   collide instead of being compared as text.
2. A live referee lock is never reclaimed because wall time passed.
3. A signed result alone never frees a footprint.
4. Only a release bound to the generation, resources, job, and exact result
   bytes frees a footprint.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "scripts" / "lenovo_worker"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(WORKER_DIR))

import job_lifecycle
import job_ownership
import job_reservations
import lan_bridge


class ReservationRefereeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue"
        (self.queue / "jobs").mkdir(parents=True)
        (self.queue / "results").mkdir()
        self.key = bytes(range(32))

    def now(self) -> str:
        return "2026-11-15T00:00:00+00:00"

    def payload(self, job_id: str, name: str) -> dict:
        workdir = self.root / name
        workdir.mkdir(parents=True, exist_ok=True)
        return lan_bridge.build_job_payload(
            job_id=job_id, command="echo hi",
            working_directory=os.fspath(workdir), timeout_seconds=5)

    def reserve(self, job_id: str, claims: list[str], name: str,
                wait_seconds: float = job_reservations.LOCK_WAIT_SECONDS,
                ) -> dict:
        return job_reservations.reserve_and_submit(
            self.queue, self.key, lan_bridge, self.payload(job_id, name),
            claims, "test-producer", os.getpid(), self.now,
            wait_seconds=wait_seconds)

    def live(self) -> set[str]:
        return {record["id"] for record in job_reservations.live_records(
            self.queue, self.key, lan_bridge)}

    def job_digest(self, job_id: str) -> str:
        return hashlib.sha256(
            (self.queue / "jobs" / f"{job_id}.json").read_bytes()).hexdigest()

    def publish_claim(self, job_id: str, resources: list[str],
                      generation: str) -> dict:
        claim = {
            "schema": job_ownership.CLAIM_SCHEMA, "kind": "claim",
            "id": job_id,
            "owner": {"instance": "test-worker", "pid": os.getpid()},
            "generation": generation, "claimed_at": self.now(),
            "resources": list(resources),
            "job_envelope_sha256": self.job_digest(job_id),
            "state": "owned-unknown-until-signed-result",
        }
        lan_bridge.publish(str(self.queue / "claims"), job_id,
                           lan_bridge.seal(claim, self.key))
        return claim

    def publish_result(self, job_id: str) -> str:
        raw = lan_bridge.seal(
            lan_bridge.build_result_payload(job_id, "ok", 0), self.key)
        lan_bridge.publish(str(self.queue / "results"), job_id, raw)
        return hashlib.sha256(raw).hexdigest()

    def publish_release(self, job_id: str, resources: list[str],
                        generation: str, owner: dict,
                        result_digest: str) -> None:
        release = {
            "schema": job_reservations.RELEASE_SCHEMA, "kind": "release",
            "id": job_id, "generation": generation,
            "owner": owner,
            "job_envelope_sha256": self.job_digest(job_id),
            "resources": list(resources),
            "result_envelope_sha256": result_digest,
            "released_at": self.now(),
        }
        lan_bridge.publish(str(self.queue / "releases"), job_id,
                           lan_bridge.seal(release, self.key))

    def wait_until(self, predicate, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("fixture condition did not become true")

    def test_tcp_wildcard_and_alias_endpoints_collide(self) -> None:
        normalize = job_reservations.normalize_claim
        for left, right in (
                ("tcp:0.0.0.0:54321", "tcp:127.0.0.1:54321"),
                ("tcp:localhost:54321", "tcp:127.0.0.1:54321"),
                ("tcp:localhost:54321", "tcp:0.0.0.0:54321"),
                ("tcp:::1:54321", "tcp:127.0.0.1:54321")):
            first, second = normalize(left), normalize(right)
            self.assertEqual(first, second, (left, right))
            self.assertTrue(
                job_lifecycle.resources_overlap((first,), (second,)))
        self.assertFalse(job_lifecycle.resources_overlap(
            (normalize("tcp:127.0.0.1:54321"),),
            (normalize("tcp:127.0.0.1:54322"),)))
        # The referee boundary refuses, not just the normalizer.
        self.reserve("71" * 16, ["tcp:0.0.0.0:54321"], "tcp-a")
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve("72" * 16, ["tcp:localhost:54321"], "tcp-b")
        self.assertFalse((self.queue / "jobs" / f"{'72' * 16}.json").exists())
        self.reserve("73" * 16, ["tcp:127.0.0.1:54322"], "tcp-c")

    def test_live_referee_lock_is_never_reclaimed_by_age(self) -> None:
        holder = self.root / "hold_referee.py"
        holder.write_text(
            "import sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import job_reservations\n"
            "with job_reservations._referee_lock(Path(sys.argv[2]), 5.0):\n"
            "    Path(sys.argv[3]).write_text('held', encoding='ascii')\n"
            "    time.sleep(1.5)\n",
            encoding="utf-8")
        marker = self.root / "held.marker"
        process = subprocess.Popen(
            [sys.executable, os.fspath(holder), os.fspath(WORKER_DIR),
             os.fspath(self.queue), os.fspath(marker)])
        self.addCleanup(lambda: (process.kill()
                                 if process.poll() is None else None))
        self.wait_until(marker.exists)
        lock = self.queue / "reservations" / job_reservations.LOCK_NAME
        self.assertTrue(lock.is_file())
        aged = time.time() - 10000
        os.utime(lock, (aged, aged))
        started = time.monotonic()
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve("81" * 16, [f"file:{self.root / 'aged.out'}"],
                         "aged", wait_seconds=0.6)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertFalse((self.queue / "jobs" / f"{'81' * 16}.json").exists())
        self.assertFalse(
            (self.queue / "reservations" / f"{'81' * 16}.json").exists())
        self.assertEqual(process.wait(timeout=20), 0)
        # The operating system dropped the lock with its holder; only now
        # is the same footprint admitted.
        self.reserve("82" * 16, [f"file:{self.root / 'aged.out'}"], "aged")

    def test_signed_result_alone_never_frees_a_footprint(self) -> None:
        shared = f"file:{self.root / 'shared' / 'out.txt'}"
        first = "91" * 16
        self.reserve(first, [shared], "result-only-a")
        self.publish_result(first)
        self.assertFalse(
            (self.queue / "releases" / f"{first}.json").exists())
        self.assertIn(first, self.live())
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve("92" * 16, [shared], "result-only-b")
        self.assertFalse((self.queue / "jobs" / f"{'92' * 16}.json").exists())

    def test_release_proof_must_match_every_binding(self) -> None:
        generation = "cd" * 16
        for index, mutation in enumerate(
                ("generation", "owner", "resources", "result", "claim")):
            job_id = f"{index + 11:02d}" * 16
            record = self.reserve(
                job_id, [f"file:{self.root / f'proof{index}' / 'out.txt'}"],
                f"proof-{index}")
            resources = list(record["resources"])
            claim = None
            if mutation != "claim":
                claim = self.publish_claim(job_id, resources, generation)
            result_digest = self.publish_result(job_id)
            release_generation = generation
            release_owner = (claim["owner"] if claim is not None
                             else {"instance": "missing", "pid": 1})
            release_resources = resources
            release_digest = result_digest
            if mutation == "generation":
                release_generation = "ef" * 16
            elif mutation == "owner":
                release_owner = {"instance": "foreign-worker", "pid": 1}
            elif mutation == "resources":
                release_resources = resources + [
                    f"file:{self.root / 'other.out'}"]
            elif mutation == "result":
                release_digest = "0" * 64
            self.publish_release(job_id, release_resources,
                                 release_generation, release_owner,
                                 release_digest)
            self.assertIn(job_id, self.live(), mutation)

    def test_exact_release_frees_the_footprint(self) -> None:
        shared = f"file:{self.root / 'exact' / 'out.txt'}"
        first = "a1" * 16
        record = self.reserve(first, [shared], "exact-a")
        claim = self.publish_claim(first, list(record["resources"]),
                                   "cd" * 16)
        digest = self.publish_result(first)
        self.assertIn(first, self.live())
        self.publish_release(first, list(record["resources"]),
                             claim["generation"], claim["owner"], digest)
        self.assertNotIn(first, self.live())
        second = "a2" * 16
        self.reserve(second, [shared], "exact-b")
        self.assertTrue((self.queue / "jobs" / f"{second}.json").is_file())

    def test_resubmission_is_idempotent(self) -> None:
        job_id = "17" * 16
        claims = [f"file:{self.root / 'idempotent.out'}"]
        first = self.reserve(job_id, claims, "idempotent")
        second = self.reserve(job_id, claims, "idempotent")
        self.assertEqual(first["resources"], second["resources"])
        self.assertEqual(
            [path.name for path in
             (self.queue / "reservations").glob("*.json")],
            [f"{job_id}.json"])
        # A different footprint for an already published job is refused.
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve(job_id, [f"file:{self.root / 'other.out'}"],
                         "idempotent")


if __name__ == "__main__":
    unittest.main()
