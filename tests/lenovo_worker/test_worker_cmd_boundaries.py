"""Worker executor boundaries for authenticated cmd jobs and helper failures."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "lenovo_worker"
BRIDGE = Path(__file__).resolve().parents[2] / "scripts" / "lan_bridge"
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(SCRIPTS))

import worker


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def never_run(*args, **kwargs):
    raise AssertionError("the command must not be executed")


class BrokenWrap:
    """Helper that classifies a launcher but fails while wrapping."""

    UnsupportedLauncher = worker.truth.UnsupportedLauncher

    @staticmethod
    def launcher_kind(command):
        return "wrappable"

    @staticmethod
    def wrap_powershell(command):
        raise RuntimeError("SECRET command text")


class BrokenClassify:
    """Helper whose classification fails at call time."""


class MissedClassify:
    """Present but incompatible helper that misses an explicit launcher."""

    @staticmethod
    def launcher_kind(command):
        return None


class BrokenWrapWithoutExceptionType:
    @staticmethod
    def launcher_kind(command):
        return "wrappable"

    @staticmethod
    def wrap_powershell(command):
        raise RuntimeError("SECRET command text")


class WorkerCmdBoundaryTests(unittest.TestCase):
    def run_execute(self, command, runner):
        with mock.patch("worker.subprocess.run", side_effect=runner) as patched:
            outcome = worker._execute(command, ".", 30, 65536)
        return outcome, patched

    def test_authenticated_cmd_dialect_refuses_powershell_executable(self):
        with mock.patch("worker.subprocess.run", side_effect=never_run) as patched:
            outcome = worker._execute(
                'po"wer"shell.exe -Command "this is ((("', ".", 30, 65536,
                dialect="cmd")
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")

    def test_authenticated_cmd_documentation_mention_stays_legitimate(self):
        command = "cmd /d /c echo powershell documentation"
        with mock.patch(
                "worker.subprocess.run",
                return_value=FakeProc(0, "powershell documentation\n")) as patched:
            outcome = worker._execute(
                command, ".", 30, 65536, dialect="cmd")
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("ok", 0))
        self.assertEqual(patched.call_args[0][0], command)

    def test_authenticated_cmd_caret_launcher_still_refuses(self):
        with mock.patch("worker.subprocess.run", side_effect=never_run) as patched:
            outcome = worker._execute(
                'po^wer^shell.exe -Command "Write-Host hi"',
                ".", 30, 65536, dialect="cmd")
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")

    def test_cmd_indirections_cannot_hide_powershell(self):
        commands = [
            'call powershell -NoProfile -Command "this is ((("',
            '@powershell -NoProfile -Command "this is ((("',
            ('cmd /d /s /c ""C:\\Windows\\System32\\WindowsPowerShell'
             '\\v1.0\\powershell.exe" -NoProfile -Command "this is (((""'),
            ('cmd /d /s /c "call @powershell -NoProfile -Command '
             '"this is (((""'),
            ('%ComSpec% /d /c powershell -NoProfile -Command '
             '"this is ((("'),
            ('cmd /d /c start /wait powershell -NoProfile -Command '
             '"this is ((("'),
            ('cmd /d /c if 1==1 powershell -NoProfile -Command '
             '"this is ((("'),
        ]
        for command in commands:
            with self.subTest(command=command):
                outcome, patched = self.run_execute(command, never_run)
                self.assertFalse(patched.called)
                self.assertEqual(outcome["status"], "error")
                self.assertNotEqual(outcome["exit_code"], 0)

    def test_cmd_indirection_clean_analogues_still_run(self):
        commands = [
            'call python -c "print(1)"',
            '@python --version',
            ('cmd /d /s /c ""C:\\Program Files\\Python\\python.exe" '
             '--version"'),
            'cmd /d /s /c "call @python --version"',
        ]
        for command in commands:
            with self.subTest(command=command):
                outcome, patched = self.run_execute(
                    command, lambda *a, **k: FakeProc(0, "ok\n"))
                self.assertEqual(
                    (outcome["status"], outcome["exit_code"]), ("ok", 0))
                self.assertEqual(patched.call_args[0][0], command)

    def test_wrap_failure_refuses_without_leaking_detail(self):
        with mock.patch.object(worker, "truth", BrokenWrap):
            outcome, patched = self.run_execute(
                'powershell -NoProfile -Command "Write-Host hi"', never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)
        self.assertNotIn("SECRET", outcome["stderr"])

    def test_malformed_helper_wrap_failure_also_refuses(self):
        with mock.patch.object(worker, "truth", BrokenWrapWithoutExceptionType):
            outcome, patched = self.run_execute(
                'powershell -NoProfile -Command "Write-Host hi"', never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotIn("SECRET", outcome["stderr"])

    def test_classification_failure_keeps_cmd_working(self):
        with mock.patch.object(worker, "truth", BrokenClassify):
            outcome, patched = self.run_execute(
                "echo hi", lambda *a, **k: FakeProc(0, "hi\n"))
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("ok", 0))
        self.assertTrue(patched.called)

    def test_classification_failure_refuses_powershell(self):
        with mock.patch.object(worker, "truth", BrokenClassify):
            outcome, patched = self.run_execute(
                'powershell -NoProfile -Command "Write-Host hi"', never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")

    def test_misclassification_refuses_powershell(self):
        with mock.patch.object(worker, "truth", MissedClassify):
            outcome, patched = self.run_execute(
                'powershell -NoProfile -Command "Write-Host hi"', never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)

    def test_unsupported_launcher_shapes_refuse(self):
        shapes = [
            "powershell -NoProfile -File script.ps1",
            "powershell -EncodedCommand AAAA",
            "powershell -NoProfile -Command 'Write-Host hi'",
            'powershell -NoProfile -Command "Write-Host hi" -NoProfile',
            'powershell -NoProfile "Write-Host hi"',
            'powershell -NoProfile -Command Write-Host "hi"',
            'cmd /c powershell -NoProfile -Command "Write-Host hi"',
            'cmd /d /c powershell -NoProfile -Command "Write-Host hi"',
            'cmd /d /s /c "powershell -NoProfile -Command Write-Host"',
            'cmd /future /c powershell -Command "Write-Host hi"',
            '& powershell -NoProfile -Command "Write-Host hi"',
        ]
        for command in shapes:
            outcome, patched = self.run_execute(command, never_run)
            self.assertFalse(patched.called, command)
            self.assertEqual(outcome["status"], "error", command)
            self.assertNotEqual(outcome["exit_code"], 0, command)
            self.assertNotIn("Write-Host", outcome["stderr"], command)
            self.assertNotIn("script.ps1", outcome["stderr"], command)


if __name__ == "__main__":
    unittest.main()
