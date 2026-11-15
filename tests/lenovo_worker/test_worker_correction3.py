"""Correction-3 Windows containment regressions.

Real subprocesses only for process-tree semantics; no live hosts. Every
test proves that command code cannot run before kill-on-close ownership
exists: containment unavailable, a delayed containment failure, a failed
durable start record, a failed resume, a worker crash inside that window,
and an unproven termination.
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "scripts" / "lenovo_worker"
BRIDGE_DIR = ROOT / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(WORKER_DIR))

import job_lifecycle
import job_ownership
import lan_bridge
import worker


def command_for(*parts: object) -> str:
    values = [os.fspath(part) for part in parts]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


class ContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue"
        (self.queue / "jobs").mkdir(parents=True)
        (self.queue / "results").mkdir()
        self.key = bytes(range(32))
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

    @unittest.skipUnless(os.name == "nt", "Windows containment contract")
    def test_containment_unavailable_stays_unknown(self) -> None:
        job_id = "c3" * 16
        active, _ = self.submit(job_id, "contain-fail", 0.3, timeout=5)
        with mock.patch.object(
                job_lifecycle.job_tree, "contain", return_value=None):
            with self.assertRaises(job_lifecycle.TreeTerminationUnknown):
                job_lifecycle.execute(
                    command_for(sys.executable, self.fixture, active,
                                self.root / "contain-fail.runs", "0.3"),
                    os.fspath(self.root), 5, 65536)
        # The child was created suspended: no command code ran.
        time.sleep(0.2)
        self.assertFalse(active.exists())
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows containment contract")
    def test_delayed_containment_failure_runs_no_command_code(self) -> None:
        # Exact review reproduction: containment is attempted slowly and
        # then fails. The command must not have begun at all.
        active = self.root / "slow-contain.active"
        runs = self.root / "slow-contain.runs"

        def delayed_contain(_pid: int) -> None:
            time.sleep(0.4)
            return None

        with mock.patch.object(
                job_lifecycle.job_tree, "contain", delayed_contain):
            with self.assertRaises(job_lifecycle.TreeTerminationUnknown):
                job_lifecycle.execute(
                    command_for(sys.executable, self.fixture, active,
                                runs, "0.3"),
                    os.fspath(self.root), 5, 65536)
        self.assertFalse(active.exists())
        self.assertFalse(runs.exists())

    @unittest.skipUnless(os.name == "nt", "Windows containment contract")
    def test_start_record_failure_runs_no_command_code(self) -> None:
        # Containment succeeded but the durable start record failed: the
        # still-suspended child is terminated, nothing runs, nothing seals.
        job_id = "c7" * 16
        active, runs = self.submit(job_id, "start-fail", 0.3, timeout=5)
        with mock.patch.object(
                job_ownership, "publish_start",
                side_effect=ValueError("injected start failure")):
            counts = worker.run_once(self.queue, self.key, 1)
        self.assertEqual(counts.get("unknown"), 1)
        time.sleep(0.2)
        self.assertFalse(active.exists())
        self.assertFalse(runs.exists())
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())
        self.assertTrue(
            (self.queue / "claims" / f"{job_id}.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows containment contract")
    def test_resume_failure_runs_no_command_code(self) -> None:
        job_id = "c8" * 16
        active, runs = self.submit(job_id, "resume-fail", 0.3, timeout=5)
        with mock.patch.object(
                job_lifecycle.job_tree, "resume_process",
                return_value=False):
            counts = worker.run_once(self.queue, self.key, 1)
        self.assertEqual(counts.get("unknown"), 1)
        time.sleep(0.2)
        self.assertFalse(active.exists())
        self.assertFalse(runs.exists())
        self.assertFalse(
            (self.queue / "results" / f"{job_id}.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows containment contract")
    def test_crash_between_spawn_and_containment_has_no_side_effect(self) -> None:
        # The worker dies in the spawn/containment window. The suspended
        # child must never reach command code, so no external effect and
        # no start record exist; only a suspended orphan to reap.
        marker = self.root / "crash-window.active"
        runs = self.root / "crash-window.runs"
        pidfile = self.root / "crash-window.pid"
        script = self.root / "crashing_worker.py"
        script.write_text(
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import job_lifecycle\n"
            "real_popen = job_lifecycle.subprocess.Popen\n"
            "def dying_popen(*args, **kwargs):\n"
            "    proc = real_popen(*args, **kwargs)\n"
            "    with open(sys.argv[2], 'w', encoding='ascii') as handle:\n"
            "        handle.write(str(proc.pid))\n"
            "    os._exit(9)\n"
            "job_lifecycle.subprocess.Popen = dying_popen\n"
            "job_lifecycle.execute(sys.argv[3], sys.argv[4], 5, 65536)\n",
            encoding="utf-8")
        command = command_for(sys.executable, self.fixture, marker, runs, "3")
        crashed = subprocess.run(
            [sys.executable, os.fspath(script), os.fspath(WORKER_DIR),
             os.fspath(pidfile), command, os.fspath(self.root)],
            capture_output=True, text=True, timeout=20, check=False)
        try:
            self.assertEqual(crashed.returncode, 9, crashed.stderr)
            time.sleep(0.6)
            self.assertTrue(pidfile.is_file())
            self.assertFalse(marker.exists())
            self.assertFalse(runs.exists())
        finally:
            if pidfile.is_file():
                pid = int(pidfile.read_text(encoding="ascii"))
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False)

    @unittest.skipUnless(os.name == "nt", "Windows containment contract")
    def test_failed_termination_stays_unknown(self) -> None:
        owned = job_lifecycle.OwnedProcess(
            command_for(sys.executable, self.fixture,
                        self.root / "term-fail.active",
                        self.root / "term-fail.runs", "5"),
            os.fspath(self.root))
        owned.start()
        self.addCleanup(
            lambda: subprocess.run(
                ["taskkill", "/PID", str(owned.process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False))
        try:
            with mock.patch.object(
                    job_lifecycle.job_tree, "terminate_job",
                    return_value=False):
                with self.assertRaises(
                        job_lifecycle.TreeTerminationUnknown):
                    owned.settle_tree()
        finally:
            try:
                owned.process.communicate(timeout=5)
            except Exception:
                owned._abandon_pipes()
                try:
                    owned.process.wait(timeout=5)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
