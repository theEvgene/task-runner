"""Capacity quota contracts: one queue-local authority, counted live holders.

Real files, real operating-system locks, real subprocesses; no live host and
no live queue. Each test is one independent-review reproduction:

6. A configured capacity quota is counted and a denial names the limiting
   resource; an unknown quota or unprovable path classification fails
   closed before publication.
7. The quota is one queue-local authority: a producer cannot raise it, two
   concurrent producers cannot exceed it, an owned retry survives a
   configuration change, and the supported submit route can use it.

The shared queue and signed-envelope publishers live in
``reservation_fixtures``; the exclusive footprint and release contracts are
in ``test_reservation_referee.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import types
import unittest

# The fixtures module puts the worker and bridge packages on sys.path, so it
# is imported before the modules under test.
from reservation_fixtures import BRIDGE_DIR, WORKER_DIR, ReservationCase

import job_lifecycle
import job_ownership
import job_reservations
import lan_bridge


class ReservationCapacityTests(ReservationCase):
    def test_configured_capacity_quota_names_the_limiting_resource(self) -> None:
        limits = {"heavy-build": 2, "batch": 1}
        self.configure(limits)
        heavy = "capacity:heavy-build"
        first = self.reserve("71" * 16, [heavy], "cap-a", capacities=limits)
        second = self.reserve("72" * 16, [heavy], "cap-b", capacities=limits)
        self.assertIn(heavy, first["resources"])
        self.assertIn(second["id"], self.live())
        # A configured quota of two admits two owners, but capacity is
        # shared under the referee, not exclusive in worker admission.
        self.assertFalse(job_lifecycle.resources_overlap((heavy,), (heavy,)))
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                r"capacity quota exhausted: capacity:heavy-build "
                r"\(limit 2, held 2\)"):
            self.reserve("73" * 16, [heavy], "cap-c", capacities=limits)
        self.assertFalse((self.queue / "jobs" / f"{'73' * 16}.json").exists())
        # A stale generation frees nothing: the third owner stays refused.
        record = job_reservations.read(
            self.queue, self.key, lan_bridge, "71" * 16,
            self.job_digest("71" * 16))
        claim = self.publish_claim("71" * 16, list(record["resources"]),
                                   "ab" * 16)
        digest = self.publish_result("71" * 16)
        self.publish_release("71" * 16, list(record["resources"]),
                             "cd" * 16, claim["owner"], digest)
        self.assertIn("71" * 16, self.live())
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve("74" * 16, [heavy], "cap-d", capacities=limits)
        # A separate quota is independent, and the exact proof-bound
        # release frees its slot for the next owner.
        batch = self.reserve("75" * 16, ["capacity:batch"], "batch-a",
                             capacities=limits)
        batch_claim = self.publish_claim("75" * 16, list(batch["resources"]),
                                         "ef" * 16)
        batch_digest = self.publish_result("75" * 16)
        self.publish_release("75" * 16, list(batch["resources"]),
                             batch_claim["generation"], batch_claim["owner"],
                             batch_digest)
        self.assertNotIn("75" * 16, self.live())
        self.reserve("76" * 16, ["capacity:batch"], "batch-b",
                     capacities=limits)

    def test_unknown_quota_and_unprovable_classification_fail_closed(self) -> None:
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                r"unknown capacity quota: capacity:heavy-build"):
            self.reserve("81" * 16, ["capacity:heavy-build"], "quota-unknown")
        # The supported managed-write submit path reads the same queue
        # authority, so with no quota file it refuses this claim too.
        with self.assertRaises(lan_bridge.BridgeError) as bridge_refused:
            lan_bridge.submit_job(
                os.fspath(self.queue), self.key, "echo hi",
                os.fspath(self.root / "bridge-capacity"), 5,
                resource_claims=["capacity:heavy-build"])
        self.assertIn("unknown capacity quota",
                      str(bridge_refused.exception))
        for bad in ({"heavy-build": 0}, {"heavy-build": True},
                    {"bad name!": 1},
                    {"CPU": 1, "cpu": 2},
                    {f"k{index}": 1 for index in range(9)}):
            with self.assertRaises(job_reservations.ReservationRefused):
                self.reserve("82" * 16, ["capacity:heavy-build"], "bad-quota",
                             capacities=bad)
        for claim in ("file:relative/path", "file:", "tcp:host:notaport",
                      "fixture:declared", "gpu:0"):
            with self.assertRaises(job_reservations.ReservationRefused):
                self.reserve("83" * 16, [claim], "bad-class")
        for job_id in ("81" * 16, "82" * 16, "83" * 16):
            self.assertFalse((self.queue / "jobs" / f"{job_id}.json").exists())
            self.assertFalse(
                (self.queue / "reservations" / f"{job_id}.json").exists())
        # An unreadable quota authority is never treated as "no quota".
        for broken in ("{", "[]", '{"heavy-build": "2"}'):
            self.configure(broken)
            with self.assertRaises(job_reservations.ReservationRefused):
                self.reserve("84" * 16, ["capacity:heavy-build"], "broken")
        self.assertFalse((self.queue / "jobs" / f"{'84' * 16}.json").exists())

    def test_capacity_quota_authority_cannot_be_raised_per_producer(self) -> None:
        # One queue-local quota file decides the ceiling for every producer.
        self.configure({"cpu": 1})
        heavy = "capacity:cpu"
        admitted = self.reserve("91" * 16, [heavy], "authority-a",
                                capacities={"cpu": 1})
        self.assertIn(heavy, admitted["resources"])
        # A second producer asserting a larger ceiling is refused by name
        # instead of being admitted beside the first holder.
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                r"capacity quota assertion differs from the queue "
                r"authority: capacity:cpu"):
            self.reserve("92" * 16, [heavy], "authority-b",
                         capacities={"cpu": 8})
        # The same producer with no assertion still cannot exceed the
        # authoritative limit of one.
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                r"capacity quota exhausted: capacity:cpu "
                r"\(limit 1, held 1\)"):
            self.reserve("93" * 16, [heavy], "authority-c")
        self.assertEqual(self.live(), {"91" * 16})
        for job_id in ("92" * 16, "93" * 16):
            self.assertFalse((self.queue / "jobs" / f"{job_id}.json").exists())
            self.assertFalse(
                (self.queue / "reservations" / f"{job_id}.json").exists())
        # An assertion that agrees with the authority for the name it
        # claims is still admitted, so the queue file stays the ceiling
        # without forcing a producer to restate every configured name.
        self.configure({"cpu": 1, "batch": 1})
        self.reserve("95" * 16, ["capacity:batch"], "authority-e",
                     capacities={"batch": 1})
        self.assertIn("95" * 16, self.live())
        # Removing the authority admits no capacity claim at all.
        (self.queue / job_reservations.CAPACITY_CONFIG_NAME).unlink()
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                r"unknown capacity quota: capacity:cpu"):
            self.reserve("94" * 16, [heavy], "authority-d", capacities={})

    def test_owned_capacity_retry_survives_configuration_change(self) -> None:
        self.configure({"mem": 2})
        job_id = "95" * 16
        claim = ["capacity:mem"]
        first = self.reserve(job_id, claim, "owned-cap",
                             capacities={"mem": 2})
        # An already-published job is settled: a retry is idempotent even
        # when the caller asserts a different quota, and even after the
        # queue configuration changed under it.
        for capacities in ({"mem": 1}, None):
            retried = self.reserve(job_id, claim, "owned-cap",
                                   capacities=capacities)
            self.assertEqual(retried, first)
            self.assertEqual(retried["generation"], first["generation"])
        self.configure({"mem": 1})
        self.assertEqual(
            self.reserve(job_id, claim, "owned-cap"), first)
        self.assertEqual(
            [path.name for path in
             (self.queue / "reservations").glob("*.json")],
            [f"{job_id}.json"])
        # The settled owner still holds its slot, so a new claimant waits
        # for the exact proof-bound release rather than double-admitting.
        with self.assertRaisesRegex(
                job_reservations.ReservationRefused,
                r"capacity quota exhausted: capacity:mem \(limit 1, held 1\)"):
            self.reserve("96" * 16, claim, "owned-cap-next")
        record = job_reservations.read(
            self.queue, self.key, lan_bridge, job_id, self.job_digest(job_id))
        worker_claim = self.publish_claim(
            job_id, list(record["resources"]), "aa" * 16)
        digest = self.publish_result(job_id)
        self.publish_release(job_id, list(record["resources"]),
                             worker_claim["generation"],
                             worker_claim["owner"], digest)
        self.reserve("96" * 16, claim, "owned-cap-next")

    def test_concurrent_capacity_producers_obey_one_quota(self) -> None:
        # Two real producers race for one configured quota of one; the
        # referee counts holders under the queue lock, so exactly one is
        # admitted and the other is refused by naming the resource.
        self.configure({"cpu": 1})
        producer = self.root / "capacity_race.py"
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
            "    job_id=sys.argv[6], command='echo hi',\n"
            "    working_directory=os.fspath(workdir), timeout_seconds=5)\n"
            "try:\n"
            "    job_reservations.reserve_and_submit(\n"
            "        queue, key, lan_bridge, payload, ['capacity:cpu'],\n"
            "        'capacity-race', os.getpid(),\n"
            "        lambda: '2026-11-15T00:00:00+00:00',\n"
            "        wait_seconds=30.0)\n"
            "except job_reservations.ReservationRefused as error:\n"
            "    print('REFUSED=' + str(error))\n"
            "else:\n"
            "    print('ADMITTED')\n",
            encoding="utf-8")
        commands = []
        for index, job_id in enumerate(("a1" * 16, "a2" * 16)):
            workdir = self.root / f"capacity-race-{index}"
            workdir.mkdir(parents=True, exist_ok=True)
            commands.append([
                sys.executable, os.fspath(producer), os.fspath(WORKER_DIR),
                os.fspath(BRIDGE_DIR), os.fspath(self.queue),
                os.fspath(self.key_file), os.fspath(workdir), job_id])
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
        self.assertIn("capacity quota exhausted: capacity:cpu", refused[0])
        self.assertEqual(len(self.live()), 1, lines)
        self.assertEqual(
            len(list((self.queue / "jobs").glob("*.json"))), 1, lines)

    def test_capacity_holders_run_concurrently_beside_independent_job(
            self) -> None:
        # Worker admission shares the referee's semantics: capacity is a
        # quota, so two holders run at once beside an unrelated footprint.
        # The barrier only clears when all three are active together.
        rendezvous = threading.Barrier(3, timeout=10)
        executed: list[str] = []

        def execute(job) -> str:
            rendezvous.wait()
            executed.append(job.job_id)
            return "sealed"

        jobs = [
            types.SimpleNamespace(job_id="cap-first",
                                  resources=("capacity:cpu",)),
            types.SimpleNamespace(job_id="cap-second",
                                  resources=("capacity:cpu",)),
            types.SimpleNamespace(
                job_id="independent",
                resources=(f"file:{self.root / 'admission' / 'out.txt'}",)),
        ]
        counts = job_ownership.run_bounded(
            jobs, 3, lambda _job: "claimed", execute)
        self.assertEqual(counts.get("sealed"), 3)
        self.assertCountEqual(executed,
                              ["cap-first", "cap-second", "independent"])

    def test_supported_submit_route_uses_the_configured_quota(self) -> None:
        # The documented submit route configures no per-call mapping, so it
        # must be able to use the queue-local authority rather than being
        # permanently unable to submit a capacity claim.
        self.configure({"heavy-build": 1})
        claim = ["capacity:heavy-build"]
        first = lan_bridge.submit_job(
            os.fspath(self.queue), self.key, "echo hi",
            os.fspath(self.root / "submit-a"), 5, resource_claims=claim)
        self.assertTrue((self.queue / "jobs" / f"{first}.json").is_file())
        self.assertIn(first, self.live())
        with self.assertRaises(lan_bridge.BridgeError) as refused:
            lan_bridge.submit_job(
                os.fspath(self.queue), self.key, "echo hi",
                os.fspath(self.root / "submit-b"), 5, resource_claims=claim)
        self.assertIn("capacity quota exhausted: capacity:heavy-build",
                      str(refused.exception))
        self.assertEqual(len(list((self.queue / "jobs").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
