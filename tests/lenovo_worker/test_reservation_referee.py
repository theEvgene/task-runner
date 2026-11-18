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
6. A configured capacity quota is counted and a denial names the limiting
   resource; an unknown quota or unprovable path classification fails
   closed before publication.
7. The quota is one queue-local authority: a producer cannot raise it, two
   concurrent producers cannot exceed it, an owned retry survives a
   configuration change, and the supported submit route can use it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
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
        self.key_file = self.root / "key.bin"
        self.key_file.write_bytes(self.key)

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
                capacities: dict[str, int] | None = None,
                ) -> dict:
        return job_reservations.reserve_and_submit(
            self.queue, self.key, lan_bridge, self.payload(job_id, name),
            claims, "test-producer", os.getpid(), self.now,
            wait_seconds=wait_seconds, capacities=capacities)

    def live(self) -> set[str]:
        return {record["id"] for record in job_reservations.live_records(
            self.queue, self.key, lan_bridge)}

    def configure(self, capacities: object) -> None:
        """Write the one queue-local quota authority an operator controls."""

        path = self.queue / job_reservations.CAPACITY_CONFIG_NAME
        path.write_text(
            capacities if isinstance(capacities, str)
            else json.dumps(capacities), encoding="utf-8")

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
