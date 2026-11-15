"""Crash reconciliation: ownership is released only by typed proof.

Real subprocesses only; no live hosts. A crash-killed execution was never
observed, so settlement is always a truthful non-success, and reconciliation
refuses while the owner, the claimed tree, or the evidence binding is
unproven.
"""

from __future__ import annotations

import hashlib
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

import job_lifecycle
import job_ownership
import job_reconciliation
import lan_bridge
import proc


def command_for(*parts: object) -> str:
    values = [os.fspath(part) for part in parts]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


class ReconcileProofTests(unittest.TestCase):
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

    def submit(self, job_id: str, name: str, delay: float,
               timeout: int = 5) -> tuple[Path, Path]:
        workdir = self.root / name
        workdir.mkdir(parents=True, exist_ok=True)
        active = self.root / f"{name}.active"
        runs = self.root / f"{name}.runs"
        payload = lan_bridge.build_job_payload(
            job_id=job_id,
            command=command_for(sys.executable, self.fixture,
                                active, runs, str(delay)),
            working_directory=os.fspath(workdir),
            timeout_seconds=timeout)
        lan_bridge.publish(str(self.queue / "jobs"), job_id,
                           lan_bridge.seal(payload, self.key))
        return active, runs

    def worker_command(self) -> list[str]:
        return [sys.executable, os.fspath(WORKER),
                "--queue", os.fspath(self.queue),
                "--key-file", os.fspath(self.key_file),
                "--max-active", "2", "--once"]

    def test_once_help_describes_all_batch_exit_codes(self) -> None:
        shown = subprocess.run(
            [sys.executable, os.fspath(WORKER), "--help"],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        help_text = " ".join(shown.stdout.split())
        self.assertIn("0=settled; 2=failed; 3=retained", help_text)

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

    def claim(self, job_id: str) -> dict:
        raw = (self.queue / "claims" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "claim", job_id)

    def start(self, job_id: str) -> dict:
        raw = (self.queue / "starts" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "start", job_id)

    def reconcile_proof(self, job_id: str, claim: dict,
                        child_pid: int | None = None) -> Path:
        if child_pid is None:
            child_pid = int(self.start(job_id)["child_pid"])
        payload = {
            "schema": job_reconciliation.PROOF_SCHEMA,
            "kind": "reconciliation", "id": job_id,
            "claim_generation": claim["generation"],
            "job_envelope_sha256": claim["job_envelope_sha256"],
            "resources": claim["resources"],
            "evidence": {
                "schema": job_reconciliation.EVIDENCE_SCHEMA,
                "owner": claim["owner"],
                "child_pid": int(child_pid),
                "tree_absent": True,
                "observer": "independent-local-fixture",
                "observed_at": "2026-11-15T00:00:00+00:00",
            },
        }
        proof = self.root / f"{job_id}.proof.json"
        proof.write_bytes(lan_bridge.seal(payload, self.key))
        return proof

    def reconcile(self, job_id: str, proof: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.worker_command()
            + ["--reconcile-id", job_id,
               "--reconcile-proof", os.fspath(proof)],
            capture_output=True, text=True, timeout=10, check=False)

    def dead_owned_job(self, job_id: str, name: str) -> tuple[dict, Path]:
        self.submit(job_id, name, 0.1)
        raw_job = (self.queue / "jobs" / f"{job_id}.json").read_bytes()
        workdir = self.root / name
        resources = [
            "exclusive:global",
            job_lifecycle.resource_identity(os.fspath(workdir)),
        ]
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        self.assertFalse(proc.is_alive(dead.pid))
        generation = hashlib.sha256(job_id.encode("ascii")).hexdigest()[:32]
        owner = {"instance": "dead-test-owner", "pid": dead.pid}
        claim = {
            "schema": job_ownership.CLAIM_SCHEMA, "kind": "claim",
            "id": job_id, "owner": owner, "generation": generation,
            "claimed_at": "2026-01-01T00:00:00+00:00",
            "resources": resources,
            "job_envelope_sha256": hashlib.sha256(raw_job).hexdigest(),
            "state": "owned-unknown-until-signed-result",
        }
        lan_bridge.publish(str(self.queue / "claims"), job_id,
                           lan_bridge.seal(claim, self.key))
        start = {
            "schema": job_ownership.START_SCHEMA, "kind": "start",
            "id": job_id, "owner": owner, "generation": generation,
            "child_pid": dead.pid,
            "started_at": "2026-01-01T00:00:01+00:00",
            "resources": resources,
            "job_envelope_sha256": claim["job_envelope_sha256"],
        }
        lan_bridge.publish(str(self.queue / "starts"), job_id,
                           lan_bridge.seal(start, self.key))
        return claim, self.reconcile_proof(job_id, claim, dead.pid)

    def publish_existing_result(self, job_id: str, status: str,
                                exit_code: int) -> bytes:
        payload = lan_bridge.build_result_payload(job_id, status, exit_code)
        raw = lan_bridge.seal(payload, self.key)
        lan_bridge.publish(str(self.queue / "results"), job_id, raw)
        return raw

    def test_reconcile_existing_result_must_be_exact_non_success(self) -> None:
        for suffix, status, exit_code in (
                ("success", "ok", 0),
                ("other-error", "error", 2)):
            with self.subTest(suffix=suffix):
                job_id = hashlib.sha256(
                    suffix.encode("ascii")).hexdigest()[:32]
                claim, proof = self.dead_owned_job(job_id, suffix)
                original = self.publish_existing_result(
                    job_id, status, exit_code)
                refused = self.reconcile(job_id, proof)
                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertIn("existing result conflicts", refused.stderr)
                self.assertEqual(
                    (self.queue / "results" / f"{job_id}.json").read_bytes(),
                    original)
                self.assertFalse(
                    (self.queue / "releases" / f"{job_id}.json").exists())
                self.assertEqual(self.claim(job_id), claim)

    def test_reconcile_exact_existing_non_success_is_idempotent(self) -> None:
        job_id = "e7" * 16
        _, proof = self.dead_owned_job(job_id, "exact-reconcile")
        first = self.reconcile(job_id, proof)
        self.assertEqual(first.returncode, 0, first.stderr)
        result_raw = (
            self.queue / "results" / f"{job_id}.json").read_bytes()
        release_raw = (
            self.queue / "releases" / f"{job_id}.json").read_bytes()
        second = self.reconcile(job_id, proof)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            (self.queue / "results" / f"{job_id}.json").read_bytes(),
            result_raw)
        self.assertEqual(
            (self.queue / "releases" / f"{job_id}.json").read_bytes(),
            release_raw)

    def test_reconcile_refuses_while_claimed_tree_alive(self) -> None:
        # Kill-on-close means a crash kills the tree; the tree gate is
        # proven with a manually retained claim whose recorded child is a
        # live sleeper. Reconcile must refuse while it lives and seal
        # after it dies.
        job_id = "d4" * 16
        workdir = self.root / "live-tree"
        workdir.mkdir(parents=True, exist_ok=True)
        payload = lan_bridge.build_job_payload(
            job_id=job_id, command="echo hi",
            working_directory=os.fspath(workdir), timeout_seconds=5)
        sealed = lan_bridge.seal(payload, self.key)
        lan_bridge.publish(str(self.queue / "jobs"), job_id, sealed)
        # A claim-less job takes the sequential fallback resource set.
        resources = ["exclusive:global",
                     job_lifecycle.resource_identity(os.fspath(workdir))]
        envelope_sha256 = hashlib.sha256(sealed).hexdigest()
        # Dead owner: a short process that has already exited.
        probe = subprocess.Popen([sys.executable, "-c", "pass"])
        probe.wait(timeout=10)
        owner_pid = int(probe.pid)
        self.assertFalse(proc.is_alive(owner_pid))
        generation = "ab" * 16
        claim = {
            "schema": job_ownership.CLAIM_SCHEMA, "kind": "claim",
            "id": job_id, "owner": {"instance": "test-owner",
                                    "pid": owner_pid},
            "generation": generation,
            "claimed_at": "2026-01-01T00:00:00+00:00",
            "resources": resources,
            "job_envelope_sha256": envelope_sha256,
            "state": "owned-unknown-until-signed-result",
        }
        lan_bridge.publish(str(self.queue / "claims"), job_id,
                           lan_bridge.seal(claim, self.key))
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (sleeper.kill()
                                 if sleeper.poll() is None else None))
        self.assertTrue(proc.is_alive(int(sleeper.pid)))
        start = {
            "schema": job_ownership.START_SCHEMA, "kind": "start",
            "id": job_id,
            "owner": {"instance": "test-owner", "pid": owner_pid},
            "generation": generation, "child_pid": int(sleeper.pid),
            "started_at": "2026-01-01T00:00:00+00:00",
            "resources": resources,
            "job_envelope_sha256": envelope_sha256,
        }
        lan_bridge.publish(str(self.queue / "starts"), job_id,
                           lan_bridge.seal(start, self.key))
        proof = self.reconcile_proof(job_id, claim)
        refused = self.reconcile(job_id, proof)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())
        sleeper.kill()
        sleeper.wait(timeout=10)
        self.wait_until(
            lambda: not proc.is_alive(int(sleeper.pid)), timeout=10.0)
        settled = self.reconcile(job_id, proof)
        self.assertEqual(settled.returncode, 0, settled.stderr)
        result = self.result(job_id)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["termination"], "unknown")

    def test_crash_kills_tree_and_reconciles_after_death(self) -> None:
        # With kill-on-close a worker crash cannot leave the claimed child
        # active; retry must not rerun and reconcile seals once dead.
        job_id = "d5" * 16
        active, runs = self.submit(job_id, "crash-kill", 3.0, timeout=10)
        runner = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(active.exists, timeout=5.0)
            self.wait_until(
                (self.queue / "claims" / f"{job_id}.json").exists,
                timeout=5.0)
            runner.kill()
            runner.communicate(timeout=10)
        finally:
            try:
                if runner.poll() is None:
                    runner.kill()
                    runner.communicate(timeout=10)
            except Exception:
                pass
        self.wait_until(
            lambda: not proc.is_alive(runner.pid), timeout=10.0)
        retry = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=10, check=False)
        self.assertEqual(retry.returncode, 3, retry.stderr)
        self.assertEqual(runs.read_text(encoding="utf-8").splitlines(),
                         ["run"])
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())
        claim = self.claim(job_id)
        start = self.start(job_id)
        self.assertEqual(start["generation"], claim["generation"])
        # Kill-on-close: the crash killed the tree promptly.
        self.wait_until(
            lambda: not proc.is_alive(int(start["child_pid"])),
            timeout=10.0)
        settled = self.reconcile(
            job_id, self.reconcile_proof(job_id, claim))
        self.assertEqual(settled.returncode, 0, settled.stderr)
        result = self.result(job_id)
        # A crash-killed run was never observed; never a false success.
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["termination"], "unknown")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(
            result["reconciliation"]["outcome_derivation"],
            "unobserved-crash")

    def test_reconcile_refuses_evidence_for_another_tree(self) -> None:
        # Typed evidence that names a different child pid than the durable
        # start record is refused; the caller cannot redirect ownership.
        job_id = "f6" * 16
        active, _ = self.submit(job_id, "wrong-tree", 0.2, timeout=5)
        first = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(active.exists, timeout=5.0)
            self.wait_until(
                (self.queue / "claims" / f"{job_id}.json").exists,
                timeout=5.0)
        finally:
            first.kill()
            first.communicate(timeout=10)
        self.wait_until(
            lambda: not proc.is_alive(first.pid), timeout=10.0)
        claim = self.claim(job_id)
        start = self.start(job_id)
        self.wait_until(
            lambda: not proc.is_alive(int(start["child_pid"])),
            timeout=10.0)
        # The hard-killed fixture may leave its marker; it is not proof.
        try:
            active.unlink(missing_ok=True)
        except OSError:
            pass
        bad = self.reconcile_proof(
            job_id, claim, child_pid=int(start["child_pid"]) + 1)
        refused = self.reconcile(job_id, bad)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
