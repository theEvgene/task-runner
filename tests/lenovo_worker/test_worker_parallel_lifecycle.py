"""Real subprocess fixtures for bounded durable Lenovo job ownership."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import types
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
import job_reservations
import lan_bridge
import proc


def proc_is_alive(pid: int) -> bool:
    return proc.is_alive(int(pid))


def command_for(*parts: object) -> str:
    values = [os.fspath(part) for part in parts]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


class ParallelLifecycleTests(unittest.TestCase):
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
               timeout: int = 5,
               workdir: Path | None = None,
               declared: list[str] | None = None,
               claims: list[str] | None = None) -> tuple[str, Path, Path]:
        workdir = workdir or self.root / name
        workdir.mkdir(parents=True, exist_ok=True)
        active = self.root / f"{name}.active"
        runs = self.root / f"{name}.runs"
        command = command_for(sys.executable, self.fixture, active, runs,
                              str(delay))
        if claims is not None:
            # Supported producer path: only the signed reservation admits
            # parallelism; the payload carries no declaration at all.
            job_id = lan_bridge.submit_job(
                os.fspath(self.queue), self.key, command, os.fspath(workdir),
                timeout, resource_claims=list(claims))
            return job_id, active, runs
        payload = lan_bridge.build_job_payload(
            job_id=job_id, command=command,
            working_directory=os.fspath(workdir),
            timeout_seconds=timeout)
        if declared is not None:
            payload["resource_claims"] = list(declared)
            # Producer attestation only: never independence authority.
            payload["resource_scope"] = {
                "complete": True, "authority": "test-fixture"}
        lan_bridge.publish(str(self.queue / "jobs"), job_id,
                           lan_bridge.seal(payload, self.key))
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

    def assert_process_absent(self, pid: int) -> None:
        if os.name == "nt":
            observed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3, check=False)
            self.assertNotIn(f'"{pid}"', observed.stdout)
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def read_pid(self, path: Path, timeout: float = 5.0) -> int:
        # The fixture writes its pid after the marker appears; wait for the
        # complete record instead of racing the writer.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = path.read_text(encoding="ascii").strip()
            except OSError:
                text = ""
            if text:
                return int(text)
            time.sleep(0.02)
        self.fail("fixture did not publish its pid")

    def result(self, job_id: str) -> dict:
        raw = (self.queue / "results" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "result", job_id)

    def claim(self, job_id: str) -> dict:
        raw = (self.queue / "claims" / f"{job_id}.json").read_bytes()
        return lan_bridge.open_envelope(raw, self.key, "claim", job_id)

    def _started_child(self, job_id: str) -> int:
        raw = (self.queue / "starts" / f"{job_id}.json").read_bytes()
        return int(lan_bridge.open_envelope(
            raw, self.key, "start", job_id)["child_pid"])

    def test_unknown_tree_halts_admission_of_waiting_job(self) -> None:
        jobs = [
            types.SimpleNamespace(job_id=name, resources=(f"r:{name}",))
            for name in ("a", "b", "c")]
        executed: list[str] = []

        def execute(job) -> str:
            executed.append(job.job_id)
            if job.job_id == "a":
                return "unknown"
            time.sleep(0.1)
            return "sealed"

        counts = job_ownership.run_bounded(
            jobs, 2, lambda _job: "claimed", execute)
        self.assertEqual(counts["unknown"], 1)
        self.assertCountEqual(executed, ["a", "b"])

    def test_timeout_isolated_and_reports_terminal_process_state(self) -> None:
        a_id, a_active, _ = self.submit("41" * 16, "a", 0.5, timeout=4)
        b_id, b_active, _ = self.submit("42" * 16, "b", 5.0, timeout=1)
        worker = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.wait_until(b_active.exists)
        b_pid = self.read_pid(b_active)
        b_result_path = self.queue / "results" / f"{b_id}.json"
        self.wait_until(b_result_path.exists, timeout=4)
        # Sequential admission: A already sealed ok before B started, so a
        # timeout tree-kill of B cannot address a live neighbor here. The
        # live-neighbor isolation is covered by the direct OwnedProcess stop
        # test; here we assert B's terminal timeout state and A's success.
        b_result = self.result(b_id)
        self.assertTrue(b_result["timed_out"])
        self.assertEqual(b_result["exit_code"], 124)
        self.assertEqual(b_result["termination"], "timeout")
        self.assert_process_absent(b_pid)
        stdout, stderr = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertEqual(self.result(a_id)["status"], "ok")

    def test_concurrent_timeout_of_b_leaves_a_untouched(self) -> None:
        # Two independently reserved jobs are active together; B's timeout
        # kills only B's tree and reports its true terminal state while A
        # keeps running and later seals ok.
        a_id, a_active, _ = self.submit(
            "43" * 16, "iso-a", 2.5, timeout=10,
            claims=[f"file:{self.root / 'iso-a.out'}"])
        b_id, b_active, _ = self.submit(
            "44" * 16, "iso-b", 5.0, timeout=1,
            claims=[f"file:{self.root / 'iso-b.out'}"])
        worker = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(
                lambda: a_active.exists() and b_active.exists(), timeout=8.0)
            a_pid = self.read_pid(a_active)
            b_result_path = self.queue / "results" / f"{b_id}.json"
            self.wait_until(b_result_path.exists, timeout=6)
            b_result = self.result(b_id)
            self.assertTrue(b_result["timed_out"])
            self.assertEqual(b_result["exit_code"], 124)
            self.assertEqual(b_result["termination"], "timeout")
            # A is untouched by B's tree kill.
            self.assertTrue(proc_is_alive(a_pid))
            self.assertTrue(a_active.exists())
        finally:
            stdout, stderr = worker.communicate(timeout=15)
        self.assertEqual(worker.returncode, 0, stderr)
        a_result = self.result(a_id)
        self.assertEqual(a_result["status"], "ok")
        self.assertFalse(a_result["timed_out"])

    def test_nested_workdirs_never_overlap(self) -> None:
        root = self.root / "shared"
        _, a_active, _ = self.submit(
            "31" * 16, "nested-a", 0.6, workdir=root)
        _, b_active, _ = self.submit(
            "32" * 16, "nested-b", 0.2, workdir=root / "child")
        worker = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        overlapped = False
        while worker.poll() is None:
            overlapped = overlapped or (a_active.exists() and b_active.exists())
            time.sleep(0.02)
        stdout, stderr = worker.communicate(timeout=5)
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertFalse(overlapped)

    def test_reservation_bound_to_other_job_bytes_refuses_execution(self) -> None:
        # A reservation for different signed bytes cannot authorize this
        # job: the binding mismatch refuses without running the command.
        job_id = "16" * 16
        workdir = self.root / "mismatch"
        workdir.mkdir(parents=True, exist_ok=True)
        active = self.root / "mismatch.active"
        runs = self.root / "mismatch.runs"
        payload = lan_bridge.build_job_payload(
            job_id=job_id,
            command=command_for(sys.executable, self.fixture,
                                active, runs, "0.1"),
            working_directory=os.fspath(workdir), timeout_seconds=5)
        other_bytes = lan_bridge.seal(dict(payload, command="echo other"),
                                      self.key)
        record = job_reservations.build(
            job_id, hashlib.sha256(other_bytes).hexdigest(),
            [f"file:{self.root / 'mismatch.out'}"],
            job_lifecycle.resource_identity(os.fspath(workdir)),
            "test-producer", os.getpid(), None,
            lambda: "2026-11-15T00:00:00+00:00")
        lan_bridge.publish(str(self.queue / "reservations"), job_id,
                           lan_bridge.seal(record, self.key))
        lan_bridge.publish(str(self.queue / "jobs"), job_id,
                           lan_bridge.seal(payload, self.key))
        completed = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=10, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(runs.exists())
        result = self.result(job_id)
        self.assertEqual(result["status"], "error")
        self.assertIn("job-unverifiable", result["stderr"])

    def test_jobs_without_authenticated_resources_stay_sequential(self) -> None:
        _, a_active, _ = self.submit(
            "34" * 16, "fallback-a", 0.5)
        _, b_active, _ = self.submit(
            "35" * 16, "fallback-b", 0.2)
        worker = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        overlapped = False
        while worker.poll() is None:
            overlapped = overlapped or (a_active.exists() and b_active.exists())
            time.sleep(0.02)
        _, stderr = worker.communicate(timeout=5)
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertFalse(overlapped)

    def test_shared_authenticated_external_resource_serializes(self) -> None:
        _, a_active, _ = self.submit(
            "36" * 16, "external-a", 0.5,
            declared=["tcp:127.0.0.1:54321"])
        _, b_active, _ = self.submit(
            "37" * 16, "external-b", 0.2,
            declared=["tcp:127.0.0.1:54321"])
        worker = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        overlapped = False
        while worker.poll() is None:
            overlapped = overlapped or (a_active.exists() and b_active.exists())
            time.sleep(0.02)
        _, stderr = worker.communicate(timeout=5)
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertFalse(overlapped)

    def test_explicit_tree_stop_does_not_stop_neighbor(self) -> None:
        a_active = self.root / "direct-a.active"
        b_active = self.root / "direct-b.active"
        a_runs = self.root / "direct-a.runs"
        b_runs = self.root / "direct-b.runs"
        a = job_lifecycle.OwnedProcess(
            command_for(sys.executable, self.fixture, a_active, a_runs, "5"),
            os.fspath(self.root))
        b = job_lifecycle.OwnedProcess(
            command_for(sys.executable, self.fixture, b_active, b_runs, "5"),
            os.fspath(self.root))
        a.start()
        b.start()
        self.addCleanup(lambda: a.stop() if a.process.poll() is None else None)
        self.addCleanup(lambda: b.stop() if b.process.poll() is None else None)
        self.wait_until(lambda: a_active.exists() and b_active.exists())
        _, _, _, signal_name = b.stop()
        self.assertTrue(a_active.exists())
        self.assertIsNone(a.process.poll())
        if os.name != "nt":
            self.assertEqual(signal_name, "SIGKILL")

    @unittest.skipUnless(os.name == "nt", "Windows taskkill contract")
    def test_failed_taskkill_is_bounded_unknown_not_false_exit(self) -> None:
        active = self.root / "failed-kill.active"
        runs = self.root / "failed-kill.runs"
        owned = job_lifecycle.OwnedProcess(
            command_for(sys.executable, self.fixture, active, runs, "5"),
            os.fspath(self.root))
        owned.start()
        self.wait_until(active.exists)
        started = time.monotonic()
        with mock.patch.object(
                job_lifecycle.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(job_lifecycle.TreeTerminationUnknown):
                owned.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        subprocess.run(
            ["taskkill", "/PID", str(owned.process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False)
        owned.process.wait(timeout=3)

    def test_worker_crash_retains_claim_and_restart_does_not_rerun(self) -> None:
        job_id, active, runs = self.submit("51" * 16, "crash", 0.6)
        first = subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.wait_until(active.exists)
        first.kill()
        first.communicate(timeout=5)
        self.wait_until(lambda: not proc_is_alive(first.pid), timeout=10.0)
        # Kill-on-close kills the tree hard; the fixture finally block may
        # not run, so the marker is not tree proof. Wait for the recorded
        # child to die and clear a stale marker for the retry reads.
        self.wait_until(
            lambda: not proc_is_alive(self._started_child(job_id)),
            timeout=10.0)
        try:
            active.unlink(missing_ok=True)
        except OSError:
            pass

        second = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=10, check=False)
        self.assertEqual(second.returncode, 3, second.stderr)
        report = json.loads(second.stdout)
        self.assertEqual(report["owned"], 1)
        self.assertEqual(runs.read_text(encoding="utf-8").splitlines(),
                         ["run"])
        self.assertFalse((self.queue / "results" / f"{job_id}.json").exists())
        claim = self.claim(job_id)
        self.assertEqual(claim["state"],
                         "owned-unknown-until-signed-result")

        finite = subprocess.run(
            [part for part in self.worker_command() if part != "--once"]
            + ["--max-jobs", "1", "--poll", "0.05"],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(finite.returncode, 3, finite.stderr)

        start_raw = (self.queue / "starts" / f"{job_id}.json").read_bytes()
        start = lan_bridge.open_envelope(
            start_raw, self.key, "start", job_id)
        proof_payload = {
            "schema": job_reconciliation.PROOF_SCHEMA,
            "kind": "reconciliation", "id": job_id,
            "claim_generation": claim["generation"],
            "job_envelope_sha256": claim["job_envelope_sha256"],
            "resources": claim["resources"],
            "evidence": {
                "schema": job_reconciliation.EVIDENCE_SCHEMA,
                "owner": claim["owner"],
                "child_pid": start["child_pid"],
                "tree_absent": True,
                "observer": "independent-local-fixture",
                "observed_at": "2026-11-15T00:00:00+00:00",
            },
        }
        proof = self.root / "proof.json"
        proof.write_bytes(lan_bridge.seal(proof_payload, self.key))
        reconciled = subprocess.run(
            self.worker_command()
            + ["--reconcile-id", job_id,
               "--reconcile-proof", os.fspath(proof)],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        result = self.result(job_id)
        # A crash-killed execution was never observed: no false success.
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["termination"], "unknown")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(
            result["reconciliation"]["claim_generation"],
            claim["generation"])
        self.assertEqual(
            result["reconciliation"]["outcome_derivation"],
            "unobserved-crash")
        release_raw = (
            self.queue / "releases" / f"{job_id}.json").read_bytes()
        release = lan_bridge.open_envelope(
            release_raw, self.key, "release", job_id)
        self.assertEqual(release["generation"], claim["generation"])

    def test_malformed_existing_claim_refuses_without_execution(self) -> None:
        job_id, _, runs = self.submit("52" * 16, "bad-claim", 0.1)
        (self.queue / "claims").mkdir()
        (self.queue / "claims" / f"{job_id}.json").write_bytes(
            b"not-json-not-authenticated")
        completed = subprocess.run(
            self.worker_command(), capture_output=True, text=True,
            timeout=5, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(runs.exists())
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())

    def test_competing_workers_execute_one_job_once(self) -> None:
        job_id, _, runs = self.submit("61" * 16, "race", 0.8)
        workers = [subprocess.Popen(
            self.worker_command(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True) for _ in range(2)]
        completed = [worker.communicate(timeout=5) for worker in workers]
        exits = [worker.returncode for worker in workers]
        self.assertIn(0, exits, completed)
        self.assertIn(3, exits, completed)
        self.assertEqual(runs.read_text(encoding="utf-8").splitlines(),
                         ["run"])
        self.assertEqual(self.result(job_id)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
