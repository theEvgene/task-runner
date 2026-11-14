"""Signed worker fallback tests for indirect cmd execution boundaries."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "lenovo_worker"
BRIDGE = Path(__file__).resolve().parents[2] / "scripts" / "lan_bridge"
sys.path[:0] = [str(SCRIPTS), str(BRIDGE)]

import worker


class FakeProc:
    returncode = 0
    stdout = "safe\n"
    stderr = ""


class BrokenClassifier:
    """Present helper whose classifier cannot produce a verdict."""


def never_run(*args, **kwargs):
    raise AssertionError("unsafe command reached subprocess.run")


class SignedFallbackBoundaryTests(unittest.TestCase):
    unsafe = [
        'start "" powershell -NoProfile -Command "this is ((("',
        'if 1==1 powershell -NoProfile -Command "this is ((("',
        'cmd /d /c start "" powershell -Command "this is ((("',
        'if /i x==X call powershell -Command "this is ((("',
        'if not exist missing start "" powershell -Command "this is ((("',
        'call start "" powershell -Command "this is ((("',
    ]
    safe = [
        'start "" python --version',
        'if 1==1 python --version',
        'cmd /d /c start "" python --version',
        'cmd /d /c if 1==1 python --version',
    ]

    def run_signed(self, command, helper, runner, dialect="cmd"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            (queue / "jobs").mkdir(parents=True)
            (queue / "results").mkdir()
            key = bytes(range(32))
            job_id = "46" * 16
            payload = worker.bridge.build_job_payload(
                job_id=job_id, command=command,
                working_directory=str(root), dialect=dialect)
            worker.bridge.publish(
                str(queue / "jobs"), job_id,
                worker.bridge.seal(payload, key))
            with mock.patch.object(worker, "truth", helper), \
                    mock.patch.object(worker, "truth_problem", "helper missing"), \
                    mock.patch("worker.subprocess.run", side_effect=runner) as run:
                self.assertEqual(worker._run_one(queue, key, job_id), "sealed")
            result = worker.bridge.open_envelope(
                (queue / "results" / f"{job_id}.json").read_bytes(),
                key, "result", job_id)
        return result, run

    def assert_unsafe_refused(self, helper):
        for command in self.unsafe:
            with self.subTest(command=command):
                result, run = self.run_signed(command, helper, never_run)
                self.assertFalse(run.called)
                self.assertEqual(result["status"], "error")
                self.assertNotEqual(result["exit_code"], 0)

    def assert_safe_runs(self, helper):
        for command in self.safe:
            with self.subTest(command=command):
                result, run = self.run_signed(
                    command, helper, lambda *args, **kwargs: FakeProc())
                self.assertTrue(run.called)
                self.assertEqual(
                    (result["status"], result["exit_code"]), ("ok", 0))

    def test_missing_helper_refuses_start_and_if(self):
        self.assert_unsafe_refused(None)

    def test_broken_classifier_refuses_start_and_if(self):
        self.assert_unsafe_refused(BrokenClassifier)

    def test_missing_helper_preserves_safe_analogues(self):
        self.assert_safe_runs(None)

    def test_broken_classifier_preserves_safe_analogues(self):
        self.assert_safe_runs(BrokenClassifier)

    def test_powershell_dialect_refuses_when_classifier_is_unavailable(self):
        for helper in (None, BrokenClassifier):
            with self.subTest(helper=helper):
                result, run = self.run_signed(
                    "Write-Host hi", helper, never_run, dialect="powershell")
                self.assertFalse(run.called)
                self.assertEqual(result["status"], "error")
                self.assertNotEqual(result["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
