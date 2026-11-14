"""Worker executor truth: fail closed through the real ``worker._execute``.

These tests go through the worker path that builds the envelope fields,
not only ``powershell_truth.execute``. They prove the #36 contract:

- a PowerShell parse error is ``error``/non-zero, never ``ok``;
- a correct command stays ``ok``/0;
- ``stderr`` text never flips the verdict in either direction;
- a missing/broken helper or an unsupported launcher shape refuses
  without executing the launcher unwrapped and without leaking command
  text, while ordinary ``cmd`` commands keep executing.

Deterministic and fast except for one local PowerShell parser regression,
which skips when no runtime is available. No live Lenovo/GitLab or network.
"""

from __future__ import annotations

import base64
import shutil
import sys
import tempfile
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


def decoded_outer(command):
    marker = "-EncodedCommand "
    if marker not in command:
        return ""
    return base64.b64decode(command.split(marker, 1)[1]).decode("utf-16le")


def decoded_inner(command):
    outer = decoded_outer(command)
    marker = "FromBase64String('"
    if marker not in outer:
        return ""
    payload = outer.split(marker, 1)[1].split("')", 1)[0]
    return base64.b64decode(payload).decode("utf-16le")


def legacy_parse_backend(command, **kwargs):
    """Exit 0 for malformed same-unit text, but honor the outer boundary."""

    inner = decoded_inner(command)
    if inner and "this is (((" in inner:
        return FakeProc(1, "", "ParserError")
    if "this is (((" in command:
        return FakeProc(0, "", "")
    if inner and "Write-Host hi" in inner:
        return FakeProc(0, "hi\n", "")
    return FakeProc(0, "ok\n", "")


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


class WorkerExecutorTruthTests(unittest.TestCase):
    def run_execute(self, command, runner):
        with mock.patch("worker.subprocess.run", side_effect=runner) as patched:
            outcome = worker._execute(command, ".", 30, 65536)
        return outcome, patched

    def run_signed_job(self, command, helper, runner):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            (queue / "jobs").mkdir(parents=True)
            (queue / "results").mkdir()
            key = bytes(range(32))
            job_id = "36" * 16
            payload = worker.bridge.build_job_payload(
                job_id=job_id, command=command,
                working_directory=str(root), dialect="cmd")
            worker.bridge.publish(
                str(queue / "jobs"), job_id,
                worker.bridge.seal(payload, key))
            with mock.patch.object(worker, "truth", helper), \
                    mock.patch("worker.subprocess.run", side_effect=runner) as patched:
                self.assertEqual(worker._run_one(queue, key, job_id), "sealed")
            result = worker.bridge.open_envelope(
                (queue / "results" / f"{job_id}.json").read_bytes(),
                key, "result", job_id)
        return result, patched

    def test_live_signed_parse_error_does_not_echo_command_or_secret(self):
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if exe is None:
            self.skipTest("no PowerShell runtime on this host")
        marker = "FAKE_SECRET_7f41c9"
        submitted = f'Write-Output {marker}; this is ((('
        command = (
            f'"{exe}" -NoProfile -NonInteractive -Command "{submitted}"')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            (queue / "jobs").mkdir(parents=True)
            (queue / "results").mkdir()
            key = bytes(range(32))
            job_id = "37" * 16
            payload = worker.bridge.build_job_payload(
                job_id=job_id, command=command,
                working_directory=str(root), dialect="powershell")
            worker.bridge.publish(
                str(queue / "jobs"), job_id,
                worker.bridge.seal(payload, key))

            self.assertEqual(worker._run_one(queue, key, job_id), "sealed")
            result = worker.bridge.open_envelope(
                (queue / "results" / f"{job_id}.json").read_bytes(),
                key, "result", job_id)

        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["exit_code"], 0)
        diagnostics = result["stderr"]
        self.assertEqual(diagnostics.strip(), "PowerShell command failed")
        self.assertNotIn(marker, diagnostics)
        self.assertNotIn(submitted, diagnostics)

    # --- verdict is the exit code, through the real worker path ---------

    def test_parse_error_is_error_not_ok(self):
        legacy_same_unit = (
            "powershell -Command \"$ErrorActionPreference='Stop'; "
            "this is (((; exit 1\"")
        self.assertEqual(
            legacy_parse_backend(legacy_same_unit).returncode, 0)
        outcome, patched = self.run_execute(
            'powershell -NoProfile -Command "this is ((("',
            legacy_parse_backend)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)
        effective = patched.call_args[0][0]
        outer = decoded_outer(effective)
        self.assertIn("[ScriptBlock]::Create($innerText)", outer)
        self.assertIn("exit $LASTEXITCODE", outer)
        self.assertEqual(decoded_inner(effective), "this is (((")

    def test_correct_command_stays_ok(self):
        outcome, patched = self.run_execute(
            'powershell -NoProfile -Command "Write-Host hi"',
            legacy_parse_backend)
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("ok", 0))
        self.assertEqual(decoded_inner(patched.call_args[0][0]),
                         "Write-Host hi")

    def test_scary_stderr_with_exit_zero_stays_ok(self):
        outcome, _ = self.run_execute(
            'powershell -NoProfile -Command "Write-Host hi"',
            lambda *a, **k: FakeProc(0, "", "ParseException: scary noise\n"))
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("ok", 0))

    def test_clean_stderr_with_nonzero_stays_error(self):
        outcome, _ = self.run_execute(
            'powershell -NoProfile -Command "this is ((("',
            lambda *a, **k: FakeProc(2, "", ""))
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)

    def test_stdout_stderr_and_native_exit_are_preserved(self):
        outcome, _ = self.run_execute(
            'powershell -NoProfile -Command "native-tool"',
            lambda *a, **k: FakeProc(7, "legitimate output\n",
                                      "legitimate error\n"))
        self.assertEqual(outcome["exit_code"], 7)
        self.assertEqual(outcome["stdout"], "legitimate output\n")
        self.assertEqual(outcome["stderr"], "legitimate error\n")

    # --- helper missing / broken: refuse PowerShell, keep cmd -----------

    def test_missing_helper_refuses_powershell_without_execution(self):
        with mock.patch.object(worker, "truth", None), \
                mock.patch.object(worker, "truth_problem", "helper missing"):
            outcome, patched = self.run_execute(
                'powershell -NoProfile -Command "Write-Host hi"', never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)
        self.assertIn("helper missing", outcome["stderr"])
        self.assertNotIn("Write-Host", outcome["stderr"])

    def test_missing_helper_keeps_ordinary_cmd_working(self):
        with mock.patch.object(worker, "truth", None), \
                mock.patch.object(worker, "truth_problem", "helper missing"):
            outcome, patched = self.run_execute(
                "echo hi", lambda *a, **k: FakeProc(0, "hi\n"))
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("ok", 0))
        self.assertEqual(patched.call_args[0][0], "echo hi")

    def test_signed_chain_refuses_when_helper_is_missing(self):
        with mock.patch.object(worker, "truth_problem", "helper missing"):
            result, patched = self.run_signed_job(
                ('echo ready & powershell -NoProfile -Command '
                 '"this is ((("'), None, never_run)
        self.assertFalse(patched.called)
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["exit_code"], 0)

    def test_signed_chain_refuses_when_classifier_fails(self):
        result, patched = self.run_signed_job(
            ('echo ready & powershell -NoProfile -Command '
             '"this is ((("'), BrokenClassify, never_run)
        self.assertFalse(patched.called)
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["exit_code"], 0)

    def test_signed_execution_exception_does_not_leak_secret(self):
        secret = "SECRET_IN_EXCEPTION_4b9c"
        result, patched = self.run_signed_job(
            "echo ready", worker.truth, RuntimeError(secret))
        self.assertTrue(patched.called)
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["stderr"], "execution failed on the worker")
        self.assertNotIn(secret, result["stderr"])

        with mock.patch.object(worker, "_execute",
                               side_effect=RuntimeError(secret)):
            result, _ = self.run_signed_job(
                "echo ready", worker.truth,
                AssertionError("subprocess.run must not be reached"))
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["stderr"], "execution failed on the worker")
        self.assertNotIn(secret, result["stderr"])

    def test_signed_safe_chain_stays_ok_without_helper(self):
        with mock.patch.object(worker, "truth_problem", "helper missing"):
            result, patched = self.run_signed_job(
                "echo ready & python --version", None,
                lambda *a, **k: FakeProc(0, "ready\nPython 3\n"))
        self.assertTrue(patched.called)
        self.assertEqual((result["status"], result["exit_code"]), ("ok", 0))

    def test_missing_helper_refuses_nested_launcher(self):
        commands = [
            'cmd /c powershell -NoProfile -Command "Write-Host hi"',
            'cmd /d /c powershell -NoProfile -Command "Write-Host hi"',
        ]
        for command in commands:
            with mock.patch.object(worker, "truth", None), \
                    mock.patch.object(worker, "truth_problem", "helper missing"):
                outcome, patched = self.run_execute(command, never_run)
            self.assertFalse(patched.called, command)
            self.assertEqual(outcome["status"], "error", command)

    def test_cmd_prefix_cannot_hide_nested_powershell(self):
        outcome, patched = self.run_execute(
            'cmd /d /c powershell -NoProfile -Command "this is ((("',
            never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)

    def test_legacy_cmd_chain_cannot_hide_powershell(self):
        commands = [
            ('cmd /d /c start "" powershell -NoProfile -Command '
             '"this is ((("'),
            ('cmd /d /c echo ready & powershell -NoProfile -Command '
             '"this is ((("'),
            ('cmd /d /c echo ready && powershell -NoProfile -Command '
             '"this is ((("'),
            ('cmd /d /c echo ready || powershell -NoProfile -Command '
             '"this is ((("'),
            ('cmd /d /c echo ready | powershell -NoProfile -Command '
             '"this is ((("'),
            ('cmd /d /c echo ready ^^& powershell -NoProfile -Command '
             '"this is ((("'),
        ]
        for command in commands:
            with self.subTest(command=command):
                outcome, patched = self.run_execute(command, never_run)
                self.assertFalse(patched.called)
                self.assertEqual(outcome["status"], "error")
                self.assertNotEqual(outcome["exit_code"], 0)

    def test_direct_legacy_chain_cannot_hide_powershell(self):
        commands = [
            ('echo ready & powershell -NoProfile -Command '
             '"this is ((("'),
            ('start "" powershell -NoProfile -Command '
             '"this is ((("'),
        ]
        for command in commands:
            with self.subTest(command=command):
                outcome, patched = self.run_execute(command, never_run)
                self.assertFalse(patched.called)
                self.assertEqual(outcome["status"], "error")
                self.assertNotEqual(outcome["exit_code"], 0)

    def test_direct_legacy_chain_verdict_preserves_safe_and_refuses_ambiguous(self):
        safe_commands = [
            "echo ready & python --version",
            'start "" python --version',
        ]
        for command in safe_commands:
            with self.subTest(command=command):
                outcome, patched = self.run_execute(
                    command, lambda *a, **k: FakeProc(0, "ok\n"))
                self.assertEqual((outcome["status"], outcome["exit_code"]),
                                 ("ok", 0))
                self.assertEqual(patched.call_args[0][0], command)

        ambiguous = "(echo ready & python --version)"
        outcome, patched = self.run_execute(ambiguous, never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)

    def test_safe_legacy_cmd_chain_still_runs(self):
        commands = [
            "cmd /d /c echo ready & python --version",
            'cmd /d /c start "" python --version',
            "cmd /d /c echo ready ^& powershell documentation",
            'cmd /d /c echo "ready & powershell documentation"',
        ]
        for command in commands:
            with self.subTest(command=command):
                outcome, patched = self.run_execute(
                    command, lambda *a, **k: FakeProc(0, "ok\n"))
                self.assertEqual((outcome["status"], outcome["exit_code"]),
                                 ("ok", 0))
                self.assertEqual(patched.call_args[0][0], command)

    def test_ambiguous_legacy_cmd_chain_fails_closed(self):
        command = "cmd /d /c (echo ready & python --version)"
        outcome, patched = self.run_execute(command, never_run)
        self.assertFalse(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)

    def test_cmd_prefix_keeps_non_powershell_command_working(self):
        command = "cmd /d /c echo hi"
        outcome, patched = self.run_execute(
            command, lambda *a, **k: FakeProc(0, "hi\n"))
        self.assertEqual((outcome["status"], outcome["exit_code"]),
                         ("ok", 0))
        self.assertEqual(patched.call_args[0][0], command)

    def test_inline_quoted_powershell_executable_is_never_unwrapped(self):
        outcome, patched = self.run_execute(
            'po"wer"shell.exe -NoProfile -Command "this is ((("',
            legacy_parse_backend)
        self.assertTrue(patched.called)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)
        self.assertIn("-EncodedCommand ", patched.call_args[0][0])

    def test_legacy_caret_escaped_parse_error_is_never_false_success(self):
        outcome, patched = self.run_execute(
            'po^wer^shell.exe -NoProfile -Command "this is ((("',
            legacy_parse_backend)
        self.assertEqual(outcome["status"], "error")
        self.assertNotEqual(outcome["exit_code"], 0)
        self.assertIn("-EncodedCommand ", patched.call_args[0][0])

    def test_legacy_caret_escaped_valid_command_stays_ok(self):
        outcome, patched = self.run_execute(
            'po^wer^shell.exe -NoProfile -Command "Write-Host hi"',
            legacy_parse_backend)
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("ok", 0))
        self.assertEqual(decoded_inner(patched.call_args[0][0]), "Write-Host hi")

    def test_documentation_mention_is_not_an_executable_match(self):
        command = "echo powershell documentation"
        outcome, patched = self.run_execute(
            command, lambda *a, **k: FakeProc(0, "powershell documentation\n"))
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("ok", 0))
        self.assertEqual(patched.call_args[0][0], command)

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

    # --- unsupported launcher shapes never run unwrapped -----------------

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
