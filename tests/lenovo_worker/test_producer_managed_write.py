"""The supported producer path: signed submit -> reservation -> worker.

Real subprocesses only; no live queue, no live host. Proves that the
managed-write producer API (`lan_bridge.submit_job` with explicit claims)
is what creates the authoritative reservation, that the legacy claim-less
submit path is unchanged, and that only a proof-bound release frees a
footprint for the next job.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "scripts" / "lenovo_worker"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
WORKER = WORKER_DIR / "worker.py"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(WORKER_DIR))

import lan_bridge


def command_for(*parts: object) -> str:
    values = [os.fspath(part) for part in parts]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


class ManagedWriteProducerTests(unittest.TestCase):
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
        self.fixture = self.root / "owned_child.py"
        self.fixture.write_text(
            "import pathlib, sys, time\n"
            "active = pathlib.Path(sys.argv[1])\n"
            "runs = pathlib.Path(sys.argv[2])\n"
            "runs.parent.mkdir(parents=True, exist_ok=True)\n"
            "with runs.open('a', encoding='utf-8') as handle:\n"
            "    handle.write('run\\n')\n"
            "active.write_text(str(__import__('os').getpid()), encoding='ascii')\n"
            "try:\n"
            "    time.sleep(float(sys.argv[3]))\n"
            "finally:\n"
            "    active.unlink(missing_ok=True)\n",
            encoding="utf-8")

    def submit(self, name: str, claims: list[str] | None = None,
               delay: float = 0.1,
               timeout: int = 5) -> tuple[str, Path, Path]:
        workdir = self.root / name
        workdir.mkdir(parents=True, exist_ok=True)
        active = self.root / f"{name}.active"
        runs = self.root / f"{name}.runs"
        job_id = lan_bridge.submit_job(
            os.fspath(self.queue), self.key,
            command_for(sys.executable, self.fixture, active, runs,
                        str(delay)),
            os.fspath(workdir), timeout, resource_claims=claims)
        return job_id, active, runs

    def worker_command(self) -> list[str]:
        return [sys.executable, os.fspath(WORKER),
                "--queue", os.fspath(self.queue),
                "--key-file", os.fspath(self.key_file),
                "--max-active", "2", "--once"]

    def wait_until(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("fixture condition did not become true")

    def result(self, job_id: str) -> dict:
        raw = (self.queue / "results" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "result", job_id)

    def test_self_attested_claims_never_authorize_parallel_writes(self) -> None:
        # Exact finding shape: distinct declared claims plus one omitted
        # shared directory both commands touch. A payload declaration is
        # not authority, so both stay sequential and never interleave.
        shared = self.root / "omitted-shared"
        shared.mkdir(parents=True, exist_ok=True)
        sentinel = shared / "write.log"
        appender = self.root / "append_shared.py"
        appender.write_text(
            "import pathlib, sys, time\n"
            "active = pathlib.Path(sys.argv[1])\n"
            "log = pathlib.Path(sys.argv[2])\n"
            "active.write_text('1', encoding='ascii')\n"
            "try:\n"
            "    with log.open('a', encoding='utf-8') as h:\n"
            "        h.write('begin\\n'); h.flush()\n"
            "        time.sleep(0.6)\n"
            "        h.write('end\\n')\n"
            "finally:\n"
            "    active.unlink(missing_ok=True)\n",
            encoding="utf-8")
        ids = []
        for job_id, name in (("a1" * 16, "omit-a"), ("b2" * 16, "omit-b")):
            workdir = self.root / name
            workdir.mkdir(parents=True, exist_ok=True)
            payload = lan_bridge.build_job_payload(
                job_id=job_id,
                command=command_for(sys.executable, appender,
                                    self.root / f"{name}.active", sentinel),
                working_directory=os.fspath(workdir), timeout_seconds=5)
            payload["resource_claims"] = [f"fixture:declared-{name}"]
            payload["resource_scope"] = {
                "complete": True, "authority": "self-attested-key-holder"}
            lan_bridge.publish(str(self.queue / "jobs"), job_id,
                               lan_bridge.seal(payload, self.key))
            ids.append(job_id)
        completed = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=15, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8").splitlines(),
                         ["begin", "end", "begin", "end"])
        for job_id in ids:
            self.assertEqual(self.result(job_id)["status"], "ok")

    def test_two_active_and_third_waits_through_the_supported_submit(self) -> None:
        # A, B, and C declare independent footprints through the real
        # producer API. Two run together and the third waits for a slot,
        # so the work takes two waves instead of one. Nothing calls the
        # reservation helper directly.
        markers = []
        ids = []
        for name in ("a", "b", "c"):
            job_id, active, _ = self.submit(
                name, claims=[f"file:{self.root / (name + '.out')}"],
                delay=1.2)
            ids.append(job_id)
            markers.append(active)
        worker = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        seen: list[set[int]] = []
        work_started = None
        while worker.poll() is None:
            snapshot = {index for index, path in enumerate(markers)
                        if path.exists()}
            if snapshot and work_started is None:
                work_started = time.monotonic()
            seen.append(snapshot)
            time.sleep(0.02)
        _, stderr = worker.communicate(timeout=20)
        elapsed = time.monotonic() - (work_started or time.monotonic())
        self.assertEqual(worker.returncode, 0, stderr)
        # The active set reaches two, never three, and all three ran.
        self.assertEqual(max(map(len, seen), default=0), 2, seen)
        self.assertEqual({index for snapshot in seen for index in snapshot},
                         {0, 1, 2})
        # Two waves of 1.2 s work behind two slots; three concurrent jobs
        # would finish in one wave.
        self.assertGreater(elapsed, 1.9, seen)
        for job_id in ids:
            self.assertEqual(self.result(job_id)["status"], "ok")
            self.assertTrue(
                (self.queue / "releases" / f"{job_id}.json").is_file())

    def test_legacy_submit_without_claims_is_unchanged(self) -> None:
        # No claim means no reservation: the documented sequential
        # fallback, and the queue gains no reservation infrastructure.
        first, a_active, _ = self.submit("legacy-a", delay=0.6)
        second, b_active, _ = self.submit("legacy-b", delay=0.2)
        worker = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        overlapped = False
        while worker.poll() is None:
            overlapped = overlapped or (a_active.exists()
                                        and b_active.exists())
            time.sleep(0.02)
        _, stderr = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertFalse(overlapped)
        self.assertFalse((self.queue / "reservations").exists())
        for job_id in (first, second):
            self.assertEqual(self.result(job_id)["status"], "ok")

    def test_conflicting_footprint_refused_before_publish(self) -> None:
        shared = self.root / "shared"
        held, _, _ = self.submit(
            "held", claims=[f"file:{shared / 'out.txt'}"])
        jobs_before = sorted(
            path.name for path in (self.queue / "jobs").glob("*.json"))
        for name, claim in (("same", f"file:{shared / 'out.txt'}"),
                            ("nested", f"file:{shared}")):
            with self.assertRaises(lan_bridge.BridgeError) as refused:
                self.submit(name, claims=[claim])
            self.assertIn("reservation refused",
                          str(refused.exception))
        self.assertEqual(
            sorted(path.name for path in (self.queue / "jobs").glob("*.json")),
            jobs_before)
        self.assertEqual(
            [path.name for path in
             (self.queue / "reservations").glob("*.json")],
            [f"{held}.json"])
        # An independent footprint is still admitted.
        self.submit("free", claims=[f"file:{self.root / 'free.out'}"])

    def test_worker_release_frees_the_footprint_for_the_next_job(self) -> None:
        out = f"file:{self.root / 'reuse' / 'out.txt'}"
        first, _, _ = self.submit("reuse-a", claims=[out])
        with self.assertRaises(lan_bridge.BridgeError):
            self.submit("reuse-b", claims=[out])
        completed = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=15, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        release_raw = (self.queue / "releases" / f"{first}.json").read_bytes()
        release = lan_bridge.open_envelope(
            release_raw, self.key, "release", first)
        claim_raw = (self.queue / "claims" / f"{first}.json").read_bytes()
        claim = lan_bridge.open_envelope(claim_raw, self.key, "claim", first)
        self.assertEqual(release["generation"], claim["generation"])
        second, _, _ = self.submit("reuse-c", claims=[out])
        self.assertTrue(
            (self.queue / "jobs" / f"{second}.json").is_file())
        self.assertTrue(
            (self.queue / "reservations" / f"{second}.json").is_file())

    def test_signed_result_without_release_keeps_the_footprint_held(self) -> None:
        out = f"file:{self.root / 'unreleased' / 'out.txt'}"
        first, _, _ = self.submit("unreleased-a", claims=[out])
        # A key holder seals a perfectly valid result, but no claim, no
        # start, and no release exist: that is not termination proof.
        lan_bridge.publish(
            str(self.queue / "results"), first,
            lan_bridge.seal(
                lan_bridge.build_result_payload(first, "ok", 0), self.key))
        self.assertFalse(
            (self.queue / "releases" / f"{first}.json").exists())
        with self.assertRaises(lan_bridge.BridgeError) as refused:
            self.submit("unreleased-b", claims=[out])
        self.assertIn("reservation refused", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
