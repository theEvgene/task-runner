"""Referee contracts: canonical footprints, live-lock authority, release proof.

Real files, real operating-system locks, real subprocesses; no live host and
no live queue. Each test is one independent-review reproduction:

1. TCP wildcard and alias endpoints name one effective port, so they must
   collide instead of being compared as text.
2. A live referee lock is never reclaimed because wall time passed.
3. A signed result alone never frees a footprint.
4. Only a release bound to the generation, resources, job, and exact result
   bytes frees a footprint.
5. Two concurrent producers cannot both hold one worktree or a nested
   worktree, while independent footprints are admitted.

The shared queue and signed-envelope publishers live in
``reservation_fixtures``; the ``capacity:`` quota contracts are in
``test_reservation_referee_capacity.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

# The fixtures module puts the worker and bridge packages on sys.path, so it
# is imported before the modules under test.
from reservation_fixtures import BRIDGE_DIR, WORKER_DIR, ReservationCase

import job_lifecycle
import job_reservations
import lan_bridge


class ReservationRefereeTests(ReservationCase):
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
        # The referee boundary refuses, not just the normalizer, and the
        # refusal names the limiting port.
        self.reserve("71" * 16, ["tcp:0.0.0.0:54321"], "tcp-a")
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                r"resource conflict with job [0-9a-f]{32}: tcp:\*:54321"):
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
        stored = job_reservations.read(
            self.queue, self.key, lan_bridge, job_id,
            self.job_digest(job_id))
        self.assertEqual(first, second)
        self.assertEqual(second, stored)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(
            [path.name for path in
             (self.queue / "reservations").glob("*.json")],
            [f"{job_id}.json"])
        # A different footprint for an already published job is refused.
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve(job_id, [f"file:{self.root / 'other.out'}"],
                         "idempotent")

    def test_final_canonical_resource_limit_refuses_before_publish(self) -> None:
        accepted = "18" * 16
        accepted_claims = [
            f"file:{self.root / 'accepted' / f'{index}.out'}"
            for index in range(job_reservations.MAX_RESOURCES - 1)
        ]
        record = self.reserve(accepted, accepted_claims, "accepted-workdir")
        self.assertEqual(len(record["resources"]),
                         job_reservations.MAX_RESOURCES)
        self.assertIn(accepted, self.live())

        refused = "19" * 16
        refused_claims = [
            f"file:{self.root / 'refused' / f'{index}.out'}"
            for index in range(job_reservations.MAX_RESOURCES)
        ]
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                "too many reservation resources"):
            self.reserve(refused, refused_claims, "refused-workdir")
        self.assertFalse(
            (self.queue / "reservations" / f"{refused}.json").exists())
        self.assertFalse((self.queue / "jobs" / f"{refused}.json").exists())

        independent = "20" * 16
        self.reserve(
            independent, [f"file:{self.root / 'independent.out'}"],
            "independent-workdir")
        self.assertIn(independent, self.live())

    def test_concurrent_owners_cannot_share_one_worktree(self) -> None:
        # Two real producer processes race for one directory and its nested
        # path. The queue-local referee serializes the check-and-publish, so
        # exactly one active claim exists; an independent footprint is still
        # admitted beside it.
        shared = self.root / "race-shared"
        shared.mkdir()
        claims = (f"file:{shared}", f"file:{shared / 'nested' / 'out.txt'}")
        ids = ("61" * 16, "62" * 16)
        producer = self.root / "race_producer.py"
        producer.write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import job_reservations, lan_bridge\n"
            "queue = Path(sys.argv[3])\n"
            "key = Path(sys.argv[4]).read_bytes()\n"
            "workdir = Path(sys.argv[5])\n"
            "payload = lan_bridge.build_job_payload(\n"
            "    job_id=sys.argv[7], command='echo hi',\n"
            "    working_directory=os.fspath(workdir), timeout_seconds=5)\n"
            "try:\n"
            "    job_reservations.reserve_and_submit(\n"
            "        queue, key, lan_bridge, payload, [sys.argv[6]],\n"
            "        'race-producer', os.getpid(),\n"
            "        lambda: '2026-11-15T00:00:00+00:00')\n"
            "except job_reservations.ReservationRefused as error:\n"
            "    print('REFUSED=' + str(error))\n"
            "else:\n"
            "    print('ADMITTED')\n",
            encoding="utf-8")
        commands = []
        for index, (job_id, claim) in enumerate(zip(ids, claims)):
            workdir = self.root / f"race-{index}"
            workdir.mkdir(parents=True, exist_ok=True)
            commands.append([
                sys.executable, os.fspath(producer), os.fspath(WORKER_DIR),
                os.fspath(BRIDGE_DIR), os.fspath(self.queue),
                os.fspath(self.key_file), os.fspath(workdir), claim, job_id])
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
            for command in commands]
        results = [process.communicate(timeout=30) for process in processes]
        self.assertEqual([process.returncode for process in processes],
                         [0, 0], results)
        lines = [stdout.strip() for stdout, _ in results]
        self.assertEqual(sum(line == "ADMITTED" for line in lines), 1, lines)
        refused = [line for line in lines if line.startswith("REFUSED=")]
        self.assertEqual(len(refused), 1, lines)
        self.assertIn("resource conflict with job", refused[0])
        self.assertIn("file:", refused[0])
        job_files = sorted(path.name for path in
                           (self.queue / "jobs").glob("*.json"))
        reservation_files = sorted(path.name for path in
                                   (self.queue / "reservations").glob("*.json"))
        self.assertEqual(len(job_files), 1, (job_files, lines))
        self.assertEqual(reservation_files, job_files, lines)
        independent = self.reserve(
            "63" * 16,
            [f"file:{self.root / 'independent-race' / 'out.txt'}"],
            "race-independent")
        self.assertIn(independent["id"], self.live())


if __name__ == "__main__":
    unittest.main()
