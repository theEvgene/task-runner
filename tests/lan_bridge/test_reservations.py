"""Issue #90 contracts: one atomic referee for footprints, ports, quotas.

Real files, real operating-system locks, real producer subprocesses; no
live host, no live queue, no Docker, no port bind. Each test names one
acceptance criterion:

1. Two concurrent producers naming one or nested worktree footprint: only
   one publishes, and independent footprints still both publish.
2. A denied port or configured heavy quota reports the limiting resource;
   an unknown quota, malformed quota configuration, or unclassifiable
   path fails closed before anything is published.
3. A quota is returned only by the same proof the referee already
   requires for a footprint: a signed result alone, a stale generation, or
   a foreign owner never frees it.
"""

from __future__ import annotations

import hashlib
import json
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

PRODUCER = """import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import job_reservations
import lan_bridge

queue, job_id, workdir, claims = (
    Path(sys.argv[3]), sys.argv[4], sys.argv[5], json.loads(sys.argv[6]))
start = Path(sys.argv[7])
payload = lan_bridge.build_job_payload(
    job_id=job_id, command="echo hi", working_directory=workdir,
    timeout_seconds=5)
while not start.exists():
    time.sleep(0.01)
try:
    job_reservations.reserve_and_submit(
        queue, bytes(range(32)), lan_bridge, payload, claims,
        "race-" + job_id, os.getpid(),
        lambda: "2026-11-18T00:00:00+00:00", wait_seconds=5.0)
except job_reservations.ReservationRefused as error:
    print("refused: " + str(error))
    raise SystemExit(3)
print("reserved")
"""


class LanReservationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue"
        (self.queue / "jobs").mkdir(parents=True)
        (self.queue / "results").mkdir()
        self.key = bytes(range(32))
        self.producer = self.root / "producer.py"
        self.producer.write_text(PRODUCER, encoding="utf-8")

    def now(self) -> str:
        return "2026-11-18T00:00:00+00:00"

    def quotas(self, document: object) -> None:
        (self.queue / job_reservations.CAPACITY_CONFIG_NAME).write_text(
            json.dumps(document), encoding="utf-8")

    def payload(self, job_id: str, name: str) -> dict:
        workdir = self.root / name
        workdir.mkdir(parents=True, exist_ok=True)
        return lan_bridge.build_job_payload(
            job_id=job_id, command="echo hi",
            working_directory=os.fspath(workdir), timeout_seconds=5)

    def reserve(self, job_id: str, claims: list[str], name: str,
                **kwargs) -> dict:
        return job_reservations.reserve_and_submit(
            self.queue, self.key, lan_bridge, self.payload(job_id, name),
            claims, "test-producer", os.getpid(), self.now, **kwargs)

    def live(self) -> set[str]:
        return {record["id"] for record in job_reservations.live_records(
            self.queue, self.key, lan_bridge)}

    def job_digest(self, job_id: str) -> str:
        return hashlib.sha256(
            (self.queue / "jobs" / f"{job_id}.json").read_bytes()).hexdigest()

    def file_resources(self, job_id: str) -> list[str]:
        return job_reservations.read(
            self.queue, self.key, lan_bridge, job_id,
            self.job_digest(job_id))["resources"]

    def publish_claim(self, job_id: str, generation: str) -> dict:
        claim = {
            "schema": job_ownership.CLAIM_SCHEMA, "kind": "claim",
            "id": job_id,
            "owner": {"instance": "test-worker", "pid": os.getpid()},
            "generation": generation, "claimed_at": self.now(),
            "resources": self.file_resources(job_id),
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

    def publish_release(self, job_id: str, generation: str, owner: dict,
                        result_digest: str) -> None:
        release = {
            "schema": job_reservations.RELEASE_SCHEMA, "kind": "release",
            "id": job_id, "generation": generation, "owner": owner,
            "job_envelope_sha256": self.job_digest(job_id),
            "resources": self.file_resources(job_id),
            "result_envelope_sha256": result_digest,
            "released_at": self.now(),
        }
        lan_bridge.publish(str(self.queue / "releases"), job_id,
                           lan_bridge.seal(release, self.key))

    def wait_until(self, predicate, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("fixture condition did not become true")

    def run_producers(self, jobs: list[tuple[str, str, list[str]]]) -> list:
        """Start the producers together and return their completed results."""

        start = self.root / "race.start"
        processes = []
        for job_id, workdir, claims in jobs:
            workdir.mkdir(parents=True, exist_ok=True)
            processes.append(subprocess.Popen(
                [sys.executable, os.fspath(self.producer),
                 os.fspath(WORKER_DIR), os.fspath(BRIDGE_DIR),
                 os.fspath(self.queue), job_id, os.fspath(workdir),
                 json.dumps(claims), os.fspath(start)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        self.wait_until(lambda: all(
            process.poll() is None for process in processes))
        time.sleep(0.05)
        start.write_text("go", encoding="ascii")
        completed = []
        for process in processes:
            out, err = process.communicate(timeout=60)
            completed.append((process.returncode, out or "", err or ""))
        return completed

    def published(self) -> list[str]:
        return sorted(path.name for path in
                      (self.queue / "jobs").glob("*.json"))

    def reservations(self) -> list[str]:
        return sorted(path.name for path in
                      (self.queue / "reservations").glob("*.json"))

    def test_nested_worktree_admits_one_concurrent_owner(self) -> None:
        shared = self.root / "nested-shared"
        nested = shared / "nested"
        nested.mkdir(parents=True)
        # Producer A runs in the shared worktree, producer B writes one
        # nested path: different text, the same effective nested worktree.
        completed = self.run_producers([
            ("31" * 16, shared, [f"file:{shared / 'own.out'}"]),
            ("32" * 16, self.root / "nested-other",
             [f"file:{nested / 'out.txt'}"]),
        ])
        self.assertEqual(sorted(code for code, _, _ in completed), [0, 3],
                         completed)
        refused = [out for code, out, _ in completed if code == 3]
        # Atomicity is the assertion here: one admission, one refusal. A
        # contender that cannot enter the bounded lock in time refuses as
        # contention, and resource naming is proven deterministically by
        # the port and quota tests below.
        self.assertTrue(refused[0].startswith("refused:"), refused[0])
        self.assertEqual(len(self.published()), 1)
        self.assertEqual(len(self.reservations()), 1)

    def test_independent_worktrees_publish_together(self) -> None:
        completed = self.run_producers([
            ("41" * 16, self.root / "independent-a",
             [f"file:{self.root / 'independent-a' / 'own.out'}"]),
            ("42" * 16, self.root / "independent-b",
             [f"file:{self.root / 'independent-b' / 'own.out'}"]),
        ])
        self.assertEqual([code for code, _, _ in completed], [0, 0], completed)
        self.assertEqual(len(self.published()), 2)
        self.assertEqual(len(self.reservations()), 2)

    def test_denied_port_names_the_limiting_port(self) -> None:
        held = "51" * 16
        self.reserve(held, ["tcp:0.0.0.0:54321"], "port-a")
        with self.assertRaises(job_reservations.ReservationRefused) as refused:
            self.reserve("52" * 16, ["tcp:localhost:54321"], "port-b")
        message = str(refused.exception)
        self.assertIn(f"resource conflict with job {held}", message)
        self.assertIn("tcp:*:54321", message)
        self.assertEqual(self.published(), [f"{held}.json"])

    def test_denied_quota_names_the_limiting_quota(self) -> None:
        self.quotas({"heavy": 1, "cpu": 4})
        first = "61" * 16
        self.reserve(first, [f"file:{self.root / 'heavy-a.out'}",
                             "capacity:heavy"], "heavy-a")
        with self.assertRaises(job_reservations.ReservationRefused) as refused:
            self.reserve("62" * 16, [f"file:{self.root / 'heavy-b.out'}",
                                     "capacity:heavy"], "heavy-b")
        message = str(refused.exception)
        self.assertIn("capacity quota exhausted: capacity:heavy", message)
        self.assertIn("limit 1", message)
        self.assertIn("held 1", message)
        self.assertEqual(self.published(), [f"{first}.json"])

        for index in range(4):
            self.reserve(f"{70 + index:02d}" * 16,
                         [f"file:{self.root / f'cpu-{index}.out'}",
                          "capacity:cpu"], f"cpu-{index}")
        with self.assertRaisesRegex(job_reservations.ReservationRefused,
                                    "capacity quota exhausted: capacity:cpu"):
            self.reserve("79" * 16, [f"file:{self.root / 'cpu-over.out'}",
                                     "capacity:cpu"], "cpu-over")
        # Saturated quota never blocks a footprint that claims none.
        self.reserve("80" * 16, [f"file:{self.root / 'cpu-free.out'}"],
                     "cpu-free")

    def test_unknown_or_ambiguous_quota_fails_closed(self) -> None:
        no_file = "81" * 16
        with self.assertRaisesRegex(job_reservations.ReservationRefused,
                                    "unknown capacity quota: capacity:gpu"):
            self.reserve(no_file, ["capacity:gpu"], "closed")
        self.assertEqual(self.published(), [])
        # A malformed, over-bounded, or non-integer configuration refuses
        # instead of being narrowed or guessed.
        for document in ('{"cpu": 0}', '{"cpu": true}',
                         '{"cpu": 2, "CPU": 2}', 'not json',
                         json.dumps({f"c{index}": 1 for index in range(9)})):
            (self.queue / job_reservations.CAPACITY_CONFIG_NAME).write_text(
                document, encoding="utf-8")
            with self.assertRaises(job_reservations.ReservationRefused):
                self.reserve(no_file, ["capacity:cpu"], "closed")
        # A caller mapping is an assertion only: it cannot raise the
        # queue-local ceiling or invent a quota.
        self.quotas({"cpu": 2})
        with self.assertRaisesRegex(job_reservations.ReservationRefused,
                                    "differs from the queue authority"):
            self.reserve(no_file, ["capacity:cpu"], "closed",
                         capacities={"cpu": 4})
        # Capacity claims are shared quota tokens, never exclusive owners.
        self.assertEqual(
            job_lifecycle.overlapping_resources(
                ("capacity:cpu",), ("capacity:cpu",)), [])
        self.assertEqual(self.published(), [])

    def test_unknown_path_classification_fails_closed(self) -> None:
        for claims, expected in (
                (["file:relative/out.txt"], "relative resource path"),
                (["capacity:-bad"], "bad capacity claim"),
                (["capacity:"], "bad reservation resource claim"),
                (["capacity:Bad Name"], "bad reservation resource claim"),
                (["vm:whatever"], "unprovable resource type")):
            with self.assertRaisesRegex(
                    job_reservations.ReservationRefused, expected):
                self.reserve("82" * 16, claims, "closed-path")
        self.assertEqual(self.published(), [])
        self.assertFalse((self.queue / "reservations").exists())

    def test_quota_is_returned_only_by_the_full_proof(self) -> None:
        self.quotas({"heavy": 1})
        held = "91" * 16
        claims = [f"file:{self.root / 'release-cap.out'}", "capacity:heavy"]
        self.reserve(held, claims, "release-cap")
        blocked = "92" * 16
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve(blocked, claims, "release-blocked")
        # A signed result alone is not termination proof (#91).
        digest = self.publish_result(held)
        with self.assertRaises(job_reservations.ReservationRefused):
            self.reserve(blocked, claims, "release-blocked")
        claim = self.publish_claim(held, "ab" * 16)
        self.publish_release(held, claim["generation"], claim["owner"], digest)
        self.assertNotIn(held, self.live())
        self.reserve(blocked, claims, "release-blocked")
        self.assertIn(blocked, self.live())

    def test_stale_or_foreign_release_never_returns_quota(self) -> None:
        self.quotas({"alpha": 1, "beta": 1})
        first, second = "a5" * 16, "a6" * 16
        self.reserve(first, [f"file:{self.root / 'alpha.out'}",
                             "capacity:alpha"], "stale")
        self.reserve(second, [f"file:{self.root / 'beta.out'}",
                              "capacity:beta"], "foreign")
        first_claim = self.publish_claim(first, "b1" * 16)
        second_claim = self.publish_claim(second, "b2" * 16)
        first_digest = self.publish_result(first)
        second_digest = self.publish_result(second)
        # A stale generation is not this attempt's proof.
        self.publish_release(first, "ff" * 16, first_claim["owner"],
                             first_digest)
        # A foreign owner is never the claim owner.
        self.publish_release(second, second_claim["generation"],
                             {"instance": "foreign-owner", "pid": 1},
                             second_digest)
        self.assertEqual(self.live(), {first, second})
        for blocked, name, token in (("ba" * 16, "gone-alpha", "alpha"),
                                     ("bb" * 16, "gone-beta", "beta")):
            with self.assertRaisesRegex(job_reservations.ReservationRefused,
                                        f"capacity:{token}"):
                self.reserve(blocked, [f"file:{self.root / f'{name}.out'}",
                                       f"capacity:{token}"], name)

    def test_owned_resubmit_survives_a_changed_quota_file(self) -> None:
        self.quotas({"heavy": 1})
        job_id = "b7" * 16
        claims = [f"file:{self.root / 'owned.out'}", "capacity:heavy"]
        first = self.reserve(job_id, claims, "owned")
        self.quotas({"heavy": 2})
        second = self.reserve(job_id, claims, "owned")
        self.assertEqual(first, second)
        self.assertEqual(len(self.reservations()), 1)
        self.assertEqual(len(self.published()), 1)


if __name__ == "__main__":
    unittest.main()
