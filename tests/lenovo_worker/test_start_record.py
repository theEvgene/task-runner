import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from launch_fixtures import SCRIPTS, TempRoot
sys.path.insert(0, str(SCRIPTS))
import launch as launch_mod
import proc as launch_proc


class StartRecordFailureTests(unittest.TestCase):
    def test_failed_pid_write_blocks_a_second_spawn(self):
        root = TempRoot()
        self.addCleanup(root.cleanup)
        state = root.root
        with mock.patch.object(launch_proc, "spawn_detached", return_value=1234) as spawn, \
             mock.patch.object(launch_proc, "write_pid_file", side_effect=OSError("disk full")):
            with self.assertRaises(launch_mod.reasons.Refusal):
                launch_mod._ensure_role(state, "worker", ["worker"], "marker", {})
            with self.assertRaises(launch_mod.reasons.Refusal):
                launch_mod._ensure_role(state, "worker", ["worker"], "marker", {})
            self.assertEqual(spawn.call_count, 1)


    def test_spawn_failure_reports_retained_marker(self):
        root = TempRoot()
        self.addCleanup(root.cleanup)
        with mock.patch.object(launch_proc, "spawn_detached", side_effect=OSError("failed")):
            with self.assertRaises(launch_mod.reasons.Refusal) as caught:
                launch_mod._ensure_role(root.root, "worker", ["worker"], "marker", {})
        self.assertIn("worker.starting", str(caught.exception))
        self.assertTrue((root.root / "worker.starting").exists())
