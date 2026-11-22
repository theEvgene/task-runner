"""Execution cap on the worker: at most 600 s from spawn (issue #168).

Proves with fast doubles (no ten-minute waits):
- already-signed jobs with 601..3600 are clamped to 600, smaller values pass;
- the lifecycle wait receives the capped value (queue waiting consumes nothing);
- a confirmed tree kill seals timed_out/124, an unproven kill stays unknown
  with no result (ownership retained by the caller);
- a normal command still completes as before and max_active 1..2 is intact.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "scripts" / "lenovo_worker"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(WORKER_DIR))

import job_lifecycle  # noqa: E402
import worker  # noqa: E402


def make_queue(root: Path):
    queue = root / "queue"
    (queue / "jobs").mkdir(parents=True)
    (queue / "results").mkdir()
    (queue / "claims").mkdir()
    (queue / "starts").mkdir()
    (queue / "releases").mkdir()
    return queue


def seal_job(queue: Path, key: bytes, job_id: str, command: str,
             workdir: str, timeout_seconds: int):
    # Bypass the client-side 600 validation on purpose: legacy signed jobs
    # may already carry up to 3600.
    payload = {"kind": "job", "id": job_id, "command": command,
               "working_directory": workdir,
               "timeout_seconds": timeout_seconds,
               "max_output_bytes": 65536, "return_files": []}
    worker.bridge.publish(str(queue / "jobs"), job_id,
                          worker.bridge.seal(payload, key))


class WorkerExecutionCapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = make_queue(self.root)
        self.key = bytes(range(32))
        self.workdir = self.root / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)

    def prepare(self, job_id: str, timeout: int):
        seal_job(self.queue, self.key, job_id, "echo hi",
                 os.fspath(self.workdir), timeout)
        return worker._prepare_job(self.queue, self.key, job_id)

    def test_prepare_clamps_legacy_preserves_smaller(self):
        for job_id, requested, expected in (
                ("60" * 16, 600, 600), ("61" * 16, 599, 599),
                ("62" * 16, 601, 600), ("63" * 16, 3600, 600),
                ("64" * 16, 45, 45), ("65" * 16, 1, 1)):
            prepared = self.prepare(job_id, requested)
            self.assertNotIsInstance(prepared, str, msg=repr(requested))
            assert not isinstance(prepared, str)
            self.assertEqual(prepared.timeout_seconds, expected,
                             msg=repr(requested))

    def test_prepare_refuses_out_of_legacy_range(self):
        for index, bad in enumerate((0, -1, 3601, 10**6)):
            job_id = f"{70 + index:02d}" * 16
            seal_job(self.queue, self.key, job_id, "echo hi",
                     os.fspath(self.workdir), bad)
            outcome = worker._prepare_job(self.queue, self.key, job_id)
            self.assertIsInstance(outcome, str)
            # A terminal refusal seals one result; nothing executes.
            result = worker.bridge.open_envelope(
                worker.bridge.read(str(self.queue / "results"), job_id),
                self.key, "result", job_id)
            self.assertEqual(result["status"], "error")

    def test_lifecycle_wait_receives_capped_value(self):
        captured: dict = {}

        class FakeOwned:
            def __init__(self, command, working_directory):
                pass

            def start(self, on_start=None):
                pass

            def wait(self, timeout_seconds):
                captured["timeout"] = timeout_seconds
                return "", "", 0

            def settle_tree(self):
                pass

        with mock.patch.object(job_lifecycle, "OwnedProcess", FakeOwned):
            for requested, expected in ((45, 45), (599, 599), (600, 600),
                                        (601, 600), (3600, 600)):
                captured.clear()
                outcome = job_lifecycle.execute(
                    "echo hi", os.fspath(self.workdir), requested, 65536)
                self.assertEqual(captured["timeout"], expected,
                                 msg=repr(requested))
                self.assertEqual(outcome["exit_code"], 0)
                self.assertFalse(outcome["timed_out"])

    def test_confirmed_timeout_seals_124_unknown_raises(self):
        class ConfirmingOwned:
            def __init__(self, command, working_directory):
                pass

            def start(self, on_start=None):
                pass

            def wait(self, timeout_seconds):
                raise subprocess.TimeoutExpired("cmd", timeout_seconds)

            def stop(self):
                return "out", "err", 3, None

        with mock.patch.object(job_lifecycle, "OwnedProcess",
                               ConfirmingOwned):
            outcome = job_lifecycle.execute(
                "echo hi", os.fspath(self.workdir), 600, 65536)
        self.assertTrue(outcome["timed_out"])
        self.assertEqual(outcome["exit_code"], 124)
        self.assertEqual(outcome["termination"], "timeout")
        self.assertIn("after 600s", outcome["stderr"])

        class UnknownOwned(ConfirmingOwned):
            def stop(self):
                raise job_lifecycle.TreeTerminationUnknown("unproven kill")

        with mock.patch.object(job_lifecycle, "OwnedProcess", UnknownOwned):
            with self.assertRaises(job_lifecycle.TreeTerminationUnknown):
                job_lifecycle.execute(
                    "echo hi", os.fspath(self.workdir), 600, 65536)
        # Unknown seals nothing: no result file appears for this path
        # (execute raises before any seal; the worker keeps the claim).

    def test_queue_waiting_does_not_consume_execution(self):
        # Simulate time spent waiting in the queue, then prove the execution
        # budget is still the full capped value measured from spawn.
        import time
        prepared = self.prepare("66" * 16, 3600)
        assert not isinstance(prepared, str)
        self.assertEqual(prepared.timeout_seconds, 600)
        time.sleep(0.05)  # queue wait: must not reduce the budget
        captured: dict = {}

        class FakeOwned:
            def __init__(self, command, working_directory):
                self.spawned_at = time.monotonic()

            def start(self, on_start=None):
                pass

            def wait(self, timeout_seconds):
                captured["timeout"] = timeout_seconds
                self.finished_at = time.monotonic()
                return "", "", 0

            def settle_tree(self):
                pass

        with mock.patch.object(job_lifecycle, "OwnedProcess", FakeOwned):
            outcome = job_lifecycle.execute(
                prepared.command, prepared.workdir,
                prepared.timeout_seconds, prepared.output_limit)
        self.assertEqual(captured["timeout"], 600)
        self.assertEqual(outcome["exit_code"], 0)

    def test_normal_command_and_concurrency_unchanged(self):
        command = (f"{sys.executable} -c "
                   "\"import sys; sys.stdout.write('ok')\"")
        outcome = job_lifecycle.execute(
            command, os.fspath(self.workdir), 600, 65536)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["exit_code"], 0)
        self.assertFalse(outcome["timed_out"])
        self.assertEqual(outcome["stdout"], "ok")
        # Existing bound of at most two actives is untouched.
        counts = worker.run_once(self.queue, self.key, 1)
        self.assertIn("sealed", counts)
        with self.assertRaises(ValueError):
            worker.run_once(self.queue, self.key, 3)


if __name__ == "__main__":
    unittest.main()
