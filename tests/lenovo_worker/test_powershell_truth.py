"""Executor truth: PowerShell errors are exit codes, never stderr text.

Deterministic and fast: a stub runner simulates both a truthful
backend and the legacy exit-0-on-parse-error backend. No live
Lenovo/GitLab, no network. One live PowerShell probe runs only when a
runtime exists and never fails the suite when absent.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "lenovo_worker"
sys.path.insert(0, str(SCRIPTS))

import powershell_truth as truth


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


def stub_truthful(command, **kwargs):
    """A truthful backend: parse errors and swallowed errors are non-zero."""
    inner = decoded_inner(command) or command
    if "this is (((" in inner:
        return FakeProc(1, "", "ParserError: Expected expression")
    if "NoSuchPathXYZ" in inner and "Write-Host hi" in inner:
        if "$ErrorActionPreference='Stop'" in decoded_outer(command):
            return FakeProc(1, "", "ObjectNotFound")
        return FakeProc(0, "hi\n", "ObjectNotFound (swallowed, legacy)")
    if "Write-Host hi" in inner:
        return FakeProc(0, "hi\n", "")
    return FakeProc(0, "ok\n", "")


def stub_legacy_parse_ok(command, **kwargs):
    """Simulate an executor that returns 0 when one parse unit is invalid."""
    inner = decoded_inner(command)
    if inner and "this is (((" in inner:
        return FakeProc(1, "", "ParserError")
    if "this is (((" in command:
        return FakeProc(0, "", "")
    if inner and "Write-Host hi" in inner:
        return FakeProc(0, "hi\n", "")
    return FakeProc(0, "ok\n", "")


class LauncherDetectionTests(unittest.TestCase):
    def test_powershell_launcher_detected(self):
        self.assertTrue(truth.is_powershell_command(
            'powershell -NoProfile -Command "Write-Host hi"'))
        self.assertTrue(truth.is_powershell_command(
            'pwsh -NoProfile -NonInteractive -Command "Write-Host hi"'))
        self.assertTrue(truth.is_powershell_command(
            '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "hi"'))

    def test_cmd_not_powershell(self):
        self.assertFalse(truth.is_powershell_command('"C:\\x\\py.exe" "--version"'))
        self.assertFalse(truth.is_powershell_command("echo hi"))


class PropagationShapeTests(unittest.TestCase):
    def test_wrap_builds_valid_outer_parse_boundary(self):
        wrapped = truth.wrap_powershell(
            'powershell -NoProfile -Command "Write-Host hi"')
        outer = decoded_outer(wrapped)
        self.assertIn("$ErrorActionPreference='Stop'", outer)
        self.assertIn("[ScriptBlock]::Create($innerText)", outer)
        self.assertIn("exit $LASTEXITCODE", outer)
        self.assertEqual(decoded_inner(wrapped), "Write-Host hi")

    def test_cmd_untouched(self):
        cmd = '"C:\\x\\py.exe" "--version"'
        self.assertEqual(truth.wrap_powershell(cmd), cmd)

    def test_wrap_preserves_quoting(self):
        wrapped = truth.wrap_powershell(
            'powershell -NoProfile -Command "Get-Item X; Write-Host hi"')
        self.assertIn("-EncodedCommand ", wrapped)
        self.assertEqual(decoded_inner(wrapped),
                         "Get-Item X; Write-Host hi")


class VerdictIsExitOnlyTests(unittest.TestCase):
    def test_noisy_stderr_with_exit_zero_stays_ok(self):
        out = truth.execute(
            'powershell -NoProfile -Command "Write-Host hi"',
            ".", 30, 65536,
            runner=lambda *a, **k: FakeProc(0, "hi\n", "scary warning\nboom\n"))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["exit_code"], 0)

    def test_clean_stderr_with_nonzero_is_error(self):
        out = truth.execute(
            'powershell -NoProfile -Command "this is ((("',
            ".", 30, 65536,
            runner=lambda *a, **k: FakeProc(1, "", ""))
        self.assertEqual(out["status"], "error")
        self.assertNotEqual(out["exit_code"], 0)

    def test_cmd_scary_stderr_stays_ok(self):
        out = truth.execute(
            '"C:\\x\\py.exe" "--version"', ".", 30, 65536,
            runner=lambda *a, **k: FakeProc(0, "3.14\n", "warning: noisy\n"))
        self.assertEqual(out["status"], "ok")


class ParseErrorTests(unittest.TestCase):
    def test_parse_error_is_error_on_truthful_backend(self):
        out = truth.execute(
            'powershell -NoProfile -Command "this is ((("',
            ".", 30, 65536, runner=stub_truthful)
        self.assertEqual(out["status"], "error")
        self.assertNotEqual(out["exit_code"], 0)

    def test_wrapper_fixes_legacy_exit_zero_parse_error(self):
        malformed = 'powershell -NoProfile -Command "this is ((("'
        legacy_same_unit = malformed.replace(
            '"this is ((("',
            '"$ErrorActionPreference=\'Stop\'; this is (((; exit 1"')
        legacy = stub_legacy_parse_ok(legacy_same_unit)
        self.assertEqual(legacy.returncode, 0)
        out = truth.execute(
            malformed,
            ".", 30, 65536, runner=stub_legacy_parse_ok)
        self.assertEqual(out["status"], "error")
        self.assertNotEqual(out["exit_code"], 0)

    def test_swallowed_error_becomes_error(self):
        legacy_cmd = 'powershell -NoProfile -Command "Get-Item NoSuchPathXYZ; Write-Host hi"'
        legacy = stub_truthful(
            'powershell -NoProfile -Command "Get-Item NoSuchPathXYZ; Write-Host hi"')
        # Sanity: without the wrapper marker the legacy shape exits 0.
        self.assertEqual(legacy.returncode, 0)
        out = truth.execute(legacy_cmd, ".", 30, 65536, runner=stub_truthful)
        self.assertEqual(out["status"], "error")

    def test_correct_command_stays_ok(self):
        out = truth.execute(
            'powershell -NoProfile -Command "Write-Host hi"',
            ".", 30, 65536, runner=stub_truthful)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["exit_code"], 0)


class UnsupportedLauncherShapeTests(unittest.TestCase):
    """A launcher that cannot be wrapped refuses instead of running raw."""

    SHAPES = {
        "powershell -NoProfile -File script.ps1": "launcher-file-flag",
        "powershell -EncodedCommand AAAA": "launcher-encoded-command",
        "powershell -NoProfile -Command 'Write-Host hi'": "single-quoted-script",
        'powershell -NoProfile -Command "Write-Host hi" -NoProfile':
            "trailing-token-after-script",
        'powershell -NoProfile "Write-Host hi"': "missing-command-flag",
        'powershell -NoProfile -Command Write-Host "hi"':
            "bare-script-with-quotes",
        'cmd /c powershell -NoProfile -Command "Write-Host hi"':
            "nested-cmd-powershell",
        'cmd /d /c powershell -NoProfile -Command "Write-Host hi"':
            "nested-cmd-powershell",
        'cmd /d /s /c "powershell -NoProfile -Command Write-Host"':
            "nested-cmd-powershell",
        'cmd /future /c powershell -Command "Write-Host hi"':
            "nested-cmd-ambiguous",
        '& powershell -NoProfile -Command "Write-Host hi"':
            "nested-call-operator-powershell",
    }
    FIRST_TOKEN_LAUNCHERS = [
        "powershell -NoProfile -File script.ps1",
        "powershell -EncodedCommand AAAA",
        "powershell -NoProfile -Command 'Write-Host hi'",
        'powershell -NoProfile -Command "Write-Host hi" -NoProfile',
        'powershell -NoProfile "Write-Host hi"',
        'powershell -NoProfile -Command Write-Host "hi"',
    ]

    def test_kind_reports_fixed_reason(self):
        for command, reason in self.SHAPES.items():
            self.assertEqual(truth.launcher_kind(command), reason, command)

    def test_supported_shapes_are_wrappable(self):
        self.assertEqual(truth.launcher_kind(
            'powershell -NoProfile -Command "Write-Host hi"'), "wrappable")
        self.assertEqual(truth.launcher_kind(
            "pwsh -NoProfile -Command Get-Date"), "wrappable")
        self.assertIsNone(truth.launcher_kind("echo hi"))
        self.assertIsNone(truth.launcher_kind("cmd /d /c echo hi"))
        self.assertIsNone(truth.launcher_kind(
            "cmd /future /c echo powershell documentation"))

    def test_indirect_launcher_shapes_are_classified(self):
        commands = [
            'call powershell -NoProfile -Command "Write-Host hi"',
            '@powershell -NoProfile -Command "Write-Host hi"',
            ('cmd /d /s /c ""C:\\Windows\\System32\\WindowsPowerShell'
             '\\v1.0\\powershell.exe" -NoProfile -Command '
             '"Write-Host hi""'),
            ('cmd /d /s /c "call @powershell -NoProfile -Command '
             '"Write-Host hi""'),
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(truth.launcher_kind(command))

    def test_indirect_non_powershell_analogues_are_not_classified(self):
        commands = [
            'call python -c "print(1)"',
            '@python --version',
            ('cmd /d /s /c ""C:\\Program Files\\Python\\python.exe" '
             '--version"'),
            'cmd /d /s /c "call @python --version"',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(truth.launcher_kind(command))

    def test_wrap_refuses_instead_of_returning_unwrapped(self):
        for command in self.FIRST_TOKEN_LAUNCHERS:
            with self.assertRaises(truth.UnsupportedLauncher):
                truth.wrap_powershell(command)

    def test_execute_refuses_without_running_the_command(self):
        for command in self.SHAPES:
            out = truth.execute(
                command, ".", 30, 65536,
                runner=lambda *a, **k: self.fail("must not execute"))
            self.assertEqual(out["status"], "error", command)
            self.assertNotEqual(out["exit_code"], 0, command)
            self.assertNotIn("Write-Host", out["stderr"], command)
            self.assertNotIn("script.ps1", out["stderr"], command)


class LiveProbeTests(unittest.TestCase):
    def test_live_powershell_parse_error_when_available(self):
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if exe is None:
            self.skipTest("no PowerShell runtime on this host")
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", "this is ((("],
            capture_output=True, text=True, timeout=30, check=False)
        wrapped = truth.wrap_powershell(
            f'"{exe}" -NoProfile -NonInteractive -Command "this is ((("')
        proc2 = subprocess.run(
            wrapped, shell=True, capture_output=True, text=True,
            timeout=30, check=False)
        self.assertNotEqual(proc2.returncode, 0)


class SecretSafetyTests(unittest.TestCase):
    def test_diagnostic_carries_no_command(self):
        secret = "SUPERSECRET-token-123"
        msg = truth.sanitize_diagnostic("boom")
        self.assertNotIn(secret, msg)
        self.assertIn("powershell", msg.lower())

    def test_spawn_failure_hides_detail(self):
        def bad_runner(*a, **k):
            raise OSError("cannot find SUPERSECRET path")
        out = truth.execute("echo hi", ".", 30, 100, runner=bad_runner)
        self.assertEqual(out["status"], "error")
        self.assertNotIn("SUPERSECRET", out["stderr"])


if __name__ == "__main__":
    unittest.main()
