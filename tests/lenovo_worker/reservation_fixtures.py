"""Shared fixtures for the reservation referee contract tests.

Real files, real operating-system locks, real subprocesses; no live host and
no live queue. This module holds the temporary queue, the signed job/claim/
result/release publishers and the few wait helpers the referee suites share,
so each test module below states only its own contracts and stays small
enough for the repository file-token guard.
"""

from __future__ import annotations

import hashlib
import json
import os
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

import job_ownership
import job_reservations
import lan_bridge


class ReservationCase(unittest.TestCase):
    """One disposable queue plus signed-envelope publishers for a suite."""

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
