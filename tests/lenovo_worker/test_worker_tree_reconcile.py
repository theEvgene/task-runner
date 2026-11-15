"""Correction-2 fixtures: authoritative admission, tree proof, honest reconcile.

Real subprocesses only for process-tree and timing semantics; no mocks
for lifecycle outcomes, no live hosts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
import worker


def command_for(*parts: object) -> str:
    values = [os.fspath(part) for part in parts]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


class Correction2Tests(unittest.TestCase):
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
        self.sleeper = self.root / "sleeper.py"
        self.sleeper.write_text(
            "import time\ntime.sleep(30)\n", encoding="utf-8")
        self.spawn_parent = self.root / "spawn_parent.py"
        self.spawn_parent.write_text(
            "import os, subprocess, sys\n"
            "child, pidfile = sys.argv[1], sys.argv[2]\n"
            "if os.name == 'nt':\n"
            "    flags = 0x8 | 0x200\n"
            "    child_proc = subprocess.Popen(\n"
            "        [sys.executable, child], stdout=subprocess.DEVNULL,\n"
            "        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,\n"
            "        creationflags=flags, close_fds=True)\n"
            "else:\n"
            "    child_proc = subprocess.Popen(\n"
            "        [sys.executable, child], stdout=subprocess.DEVNULL,\n"
            "        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,\n"
            "        close_fds=True)\n"
            "open(pidfile, 'w', encoding='ascii').write(str(child_proc.pid))\n",
            encoding="utf-8")

    def submit(self, job_id: str, name: str, delay: float,
               timeout: int = 5,
               workdir: Path | None = None,
               resource_claims: list[str] | None = None,
               declare_resources: bool = True,
               attest: bool = True) -> tuple[Path, Path]:
        workdir = workdir or self.root / name
        workdir.mkdir(parents=True, exist_ok=True)
        active = self.root / f"{name}.active"
        runs = self.root / f"{name}.runs"
        payload = lan_bridge.build_job_payload(
            job_id=job_id,
            command=command_for(sys.executable, self.fixture,
                                active, runs, str(delay)),
            working_directory=os.fspath(workdir),
            timeout_seconds=timeout)
        if declare_resources:
            payload["resource_claims"] = (
                list(resource_claims) if resource_claims is not None
                else [f"fixture:{name}"])
            if attest:
                payload["resource_scope"] = {
                    "complete": True, "authority": "test-fixture"}
        lan_bridge.publish(str(self.queue / "jobs"), job_id,
                           lan_bridge.seal(payload, self.key))
        return active, runs

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

    def assert_process_absent(self, pid: int) -> None:
        if os.name == "nt":
            observed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10, check=False)
            self.assertNotIn(f'"{pid}"', observed.stdout)
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def result(self, job_id: str) -> dict:
        raw = (self.queue / "results" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "result", job_id)

    def claim(self, job_id: str) -> dict:
        raw = (self.queue / "claims" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "claim", job_id)

    def start_record(self, job_id: str) -> dict:
        raw = (self.queue / "starts" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "start", job_id)

    def reconcile_proof(self, job_id: str, claim: dict,
                        tree_absent: bool = True) -> Path:
        start = self.start_record(job_id)
        payload = {
            "schema": job_reconciliation.PROOF_SCHEMA,
            "kind": "reconciliation", "id": job_id,
            "claim_generation": claim["generation"],
            "job_envelope_sha256": claim["job_envelope_sha256"],
            "resources": claim["resources"],
            "evidence": {
                "schema": job_reconciliation.EVIDENCE_SCHEMA,
                "owner": claim["owner"],
                "child_pid": start["child_pid"],
                "tree_absent": tree_absent,
                "observer": "independent-local-fixture",
                "observed_at": "2026-11-15T00:00:00+00:00",
            },
        }
        proof = self.root / f"{job_id}.proof.json"
        proof.write_bytes(lan_bridge.seal(payload, self.key))
        return proof

    # -- Finding 1: unattested claims must not admit parallelism ---------

    def test_unattested_disjoint_claims_stay_sequential(self) -> None:
        a_active, _ = self.submit(
            "71" * 16, "unattested-a", 0.5, attest=False)
        b_active, _ = self.submit(
            "72" * 16, "unattested-b", 0.5,
            resource_claims=["fixture:other"], attest=False)
        worker_proc = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        overlapped = False
        while worker_proc.poll() is None:
            overlapped = overlapped or (
                a_active.exists() and b_active.exists())
            time.sleep(0.02)
        _, stderr = worker_proc.communicate(timeout=10)
        self.assertEqual(worker_proc.returncode, 0, stderr)
        self.assertFalse(overlapped)
        self.assertEqual(self.result("71" * 16)["status"], "ok")
        self.assertEqual(self.result("72" * 16)["status"], "ok")

    def test_malformed_claim_refuses_without_execution(self) -> None:
        _, runs = self.submit(
            "73" * 16, "bad-shape", 0.1,
            resource_claims=["not canonical!!"])
        completed = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=10, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(runs.exists())
        result = self.result("73" * 16)
        self.assertEqual(result["status"], "error")
        self.assertIn("job-unverifiable", result["stderr"])

    # -- Finding 2: no success may outlive its tree ---------------------

    def test_detached_descendant_cannot_outlive_success(self) -> None:
        job_id = "74" * 16
        workdir = self.root / "detached-work"
        workdir.mkdir(parents=True, exist_ok=True)
        pidfile = self.root / "detached.pid"
        payload = lan_bridge.build_job_payload(
            job_id=job_id,
            command=command_for(sys.executable, self.spawn_parent,
                                self.sleeper, pidfile),
            working_directory=os.fspath(workdir),
            timeout_seconds=10)
        payload["resource_claims"] = ["fixture:detached"]
        payload["resource_scope"] = {
            "complete": True, "authority": "test-fixture"}
        lan_bridge.publish(str(self.queue / "jobs"), job_id,
                           lan_bridge.seal(payload, self.key))
        completed = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=20, check=False)
        try:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(self.result(job_id)["status"], "ok")
            self.wait_until(pidfile.exists, timeout=5.0)
            detached = int(pidfile.read_text(encoding="ascii"))
            self.assert_process_absent(detached)
        finally:
            if pidfile.exists():
                try:
                    stray = int(pidfile.read_text(encoding="ascii"))
                except ValueError:
                    stray = -1
                if stray > 0 and proc.is_alive(stray):
                    job_lifecycle.job_tree._kill_member(stray)

    def test_lifecycle_io_failure_after_spawn_retains_unknown(self) -> None:
        job_id = "75" * 16
        active, _ = self.submit(job_id, "io-fault", 5.0, timeout=30)
        real_popen = job_lifecycle.subprocess.Popen
        spawned: list[int] = []

        def boom_popen(*args, **kwargs):
            # Fault only the job spawn (shell=True); taskkill and other
            # helper spawns (shell=False) must keep working so stop() can
            # still reap the orphaned tree.
            child_proc = real_popen(*args, **kwargs)
            spawned.append(int(child_proc.pid))
            if not kwargs.get("shell"):
                return child_proc
            original = child_proc.communicate
            state = {"failed": False}

            def communicate(*call_args, **call_kwargs):
                if not state["failed"]:
                    state["failed"] = True
                    raise OSError("injected read failure")
                return original(*call_args, **call_kwargs)

            child_proc.communicate = communicate  # type: ignore[method-assign]
            return child_proc

        with mock.patch.object(job_lifecycle.subprocess, "Popen",
                               boom_popen):
            counts = worker.run_once(self.queue, self.key, 1)
        self.assertEqual(counts.get("unknown"), 1)
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())
        # The claim is retained for explicit recovery; the killed child
        # cannot remove its marker, so only the live tree is asserted dead.
        self.assertTrue(
            (self.queue / "claims" / f"{job_id}.json").exists())
        for pid in spawned:
            self.assertFalse(proc.is_alive(pid))

    # -- Finding 3: reconciliation must not settle an active job ---------

    def test_reconcile_refuses_while_owner_active(self) -> None:
        job_id = "76" * 16
        active, _ = self.submit(job_id, "active-job", 4.0, timeout=10)
        runner = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(active.exists, timeout=5.0)
            self.wait_until(
                (self.queue / "claims" / f"{job_id}.json").exists,
                timeout=5.0)
            claim = self.claim(job_id)
            proof = self.reconcile_proof(job_id, claim)
            refused = subprocess.run(
                self.worker_command()
                + ["--reconcile-id", job_id,
                   "--reconcile-proof", os.fspath(proof)],
                capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertFalse(
                (self.queue / "results" / f"{job_id}.json").exists())
        finally:
            runner.kill()
            runner.communicate(timeout=10)
        self.wait_until(
            lambda: not proc.is_alive(runner.pid), timeout=10.0)
        claim = self.claim(job_id)
        start_raw = (self.queue / "starts" / f"{job_id}.json").read_bytes()
        child_pid = int(lan_bridge.open_envelope(
            start_raw, self.key, "start", job_id)["child_pid"])
        self.wait_until(
            lambda: not proc.is_alive(child_pid), timeout=10.0)
        try:
            active.unlink(missing_ok=True)
        except OSError:
            pass
        proof = self.reconcile_proof(job_id, claim)
        settled = subprocess.run(
            self.worker_command()
            + ["--reconcile-id", job_id,
               "--reconcile-proof", os.fspath(proof)],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(settled.returncode, 0, settled.stderr)
        result = self.result(job_id)
        # The crash-killed execution was never observed: non-success only.
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["termination"], "unknown")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(
            result["reconciliation"]["claim_generation"],
            claim["generation"])
        self.assertEqual(
            result["reconciliation"]["outcome_derivation"],
            "unobserved-crash")
        proof_payload = lan_bridge.open_envelope(
            proof.read_bytes(), self.key, "reconciliation", job_id)
        self.assertEqual(
            result["reconciliation"]["evidence_sha256"],
            job_reconciliation.canonical_digest(proof_payload["evidence"]))

    def test_reconcile_refuses_opaque_evidence_bytes(self) -> None:
        job_id = "77" * 16
        active, _ = self.submit(job_id, "bad-evidence", 0.6, timeout=5)
        runner = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(active.exists, timeout=5.0)
            self.wait_until(
                (self.queue / "claims" / f"{job_id}.json").exists,
                timeout=5.0)
        finally:
            runner.kill()
            runner.communicate(timeout=10)
        self.wait_until(
            lambda: not proc.is_alive(runner.pid), timeout=10.0)
        start_raw = (self.queue / "starts" / f"{job_id}.json").read_bytes()
        child_pid = int(lan_bridge.open_envelope(
            start_raw, self.key, "start", job_id)["child_pid"])
        self.wait_until(
            lambda: not proc.is_alive(child_pid), timeout=10.0)
        try:
            active.unlink(missing_ok=True)
        except OSError:
            pass
        claim = self.claim(job_id)
        proof_payload = {
            "schema": "lenovo-job-reconciliation/1",
            "kind": "reconciliation", "id": job_id,
            "claim_generation": claim["generation"],
            "job_envelope_sha256": claim["job_envelope_sha256"],
            "resources": claim["resources"],
            "evidence_sha256": hashlib.sha256(b"actual").hexdigest(),
            "evidence_b64": base64.b64encode(b"tampered").decode("ascii"),
            "outcome": {
                "status": "ok", "exit_code": 0,
                "process_exit_code": 0, "termination": "exit",
            },
        }
        proof = self.root / f"{job_id}.proof.json"
        proof.write_bytes(lan_bridge.seal(proof_payload, self.key))
        refused = subprocess.run(
            self.worker_command()
            + ["--reconcile-id", job_id,
               "--reconcile-proof", os.fspath(proof)],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())

    def test_reconcile_refuses_unproven_tree_absence(self) -> None:
        job_id = "7a" * 16
        active, _ = self.submit(job_id, "unproven", 0.4, timeout=5)
        runner = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(active.exists, timeout=5.0)
            self.wait_until(
                (self.queue / "claims" / f"{job_id}.json").exists,
                timeout=5.0)
        finally:
            runner.kill()
            runner.communicate(timeout=10)
        self.wait_until(
            lambda: not proc.is_alive(runner.pid), timeout=10.0)
        child_pid = int(self.start_record(job_id)["child_pid"])
        self.wait_until(
            lambda: not proc.is_alive(child_pid), timeout=10.0)
        try:
            active.unlink(missing_ok=True)
        except OSError:
            pass
        claim = self.claim(job_id)
        proof = self.reconcile_proof(job_id, claim, tree_absent=False)
        refused = subprocess.run(
            self.worker_command()
            + ["--reconcile-id", job_id,
               "--reconcile-proof", os.fspath(proof)],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())

    def test_release_idempotence_validates_all_bindings(self) -> None:
        now = lambda: "2026-11-15T00:00:00+00:00"  # noqa: E731
        generations = {"a": "aa" * 16, "b": "bb" * 16}
        prepared: dict[str, job_ownership.PreparedJob] = {}
        for tag, job_id in (("a", "78" * 16), ("b", "79" * 16)):
            payload = lan_bridge.build_job_payload(
                job_id=job_id, command="echo hi",
                working_directory=os.fspath(self.root),
                timeout_seconds=5)
            raw = lan_bridge.seal(payload, self.key)
            lan_bridge.publish(str(self.queue / "jobs"), job_id, raw)
            prepared[tag] = job_ownership.PreparedJob(
                job_id=job_id, command="echo hi",
                workdir=os.fspath(self.root), timeout_seconds=5,
                output_limit=1024, dialect=None, return_files=[],
                return_files_invalid=False,
                resources=("exclusive:global",),
                envelope_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(job_ownership.claim_job(
                self.queue, prepared[tag], f"test-owner-{tag}",
                lan_bridge, self.key, now, generations[tag]), "claimed")
            lan_bridge.publish(
                str(self.queue / "results"), job_id,
                lan_bridge.seal({"kind": "result", "id": job_id,
                                 "status": "ok"},
                                self.key))
        self.assertEqual(
            job_reconciliation.publish_release(
                self.queue, self.key, prepared["a"], generations["a"],
                lan_bridge, now),
            "sealed")
        self.assertEqual(
            job_reconciliation.publish_release(
                self.queue, self.key, prepared["a"], generations["a"],
                lan_bridge, now),
            "sealed")
        self.assertEqual(
            job_reconciliation.publish_release(
                self.queue, self.key, prepared["a"], "cc" * 16,
                lan_bridge, now),
            "failed")
        forged = {
            "schema": "lenovo-job-release/1", "kind": "release",
            "id": prepared["b"].job_id, "generation": generations["b"],
            "owner": {"instance": "test-owner-b", "pid": os.getpid()},
            "job_envelope_sha256": prepared["b"].envelope_sha256,
            "resources": list(prepared["b"].resources),
            "result_envelope_sha256": "00" * 32,
            "released_at": now(),
        }
        lan_bridge.publish(
            str(self.queue / "releases"), prepared["b"].job_id,
            lan_bridge.seal(forged, self.key))
        self.assertEqual(
            job_reconciliation.publish_release(
                self.queue, self.key, prepared["b"], generations["b"],
                lan_bridge, now),
            "failed")


if __name__ == "__main__":
    unittest.main()
