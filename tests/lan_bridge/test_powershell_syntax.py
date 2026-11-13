"""PowerShell syntax adapter tests (issues #29/#31): Parser.ParseInput only.

Proves, through the ``validate(command, dialect)`` seam, that PowerShell
syntax errors refuse before any JOBID or queue write, that well-formed
wrapped PowerShell (the ``& '...' '...'`` shape built by
``pipeline_observer/request.py`` inside an explicit launcher) passes down
the legacy seal/publish path, that a bare PowerShell line refuses as
ambiguous for the worker cmd shell, that a launcher cannot smuggle an
external cmd operator, and that a broken probe fails closed. A passing
check never claims the command exists or is safe: the
nonexistent-cmdlet case pins that boundary.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lan_bridge" / "lan_bridge.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))

import lan_bridge as bridge  # noqa: E402
import syntax as gate  # noqa: E402

PS_RUNTIME = gate._find_powershell() is not None

VALID_INNER = ("& 'C:\\Python\\python.exe' 'C:\\flow\\probe.py' '--url' "
               "'https://example/x' '--profile' 'probe'")
VALID_CALL = 'powershell -NoProfile -NonInteractive -Command "' + VALID_INNER + '"'


def _jobs_files(queue):
    jobs = Path(queue) / "jobs"
    if not jobs.is_dir():
        return []
    return sorted(p for p in jobs.iterdir() if p.suffix == ".json")


class LauncherBoundaryTests(unittest.TestCase):
    """Launcher shape is checked before any runtime is needed."""

    def test_external_cmd_operator_after_script_refuses(self):
        # Regression: this used to be accepted because only the inner
        # quoted script reached ParseInput while cmd.exe also ran `echo`.
        verdict = gate.validate(
            'powershell -Command "Write-Output ok" & echo injected',
            "powershell")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.dialect, "powershell")
        self.assertIn("closing quote", verdict.message)

    def test_pipe_after_script_refuses(self):
        verdict = gate.validate(
            'powershell -Command "Write-Output ok" | findstr ok',
            "powershell")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.dialect, "powershell")

    def test_bare_powershell_refuses_as_ambiguous(self):
        verdict = gate.validate(VALID_INNER, "powershell")
        self.assertFalse(verdict.ok)
        self.assertIn("-Command", verdict.message)

    def test_file_launcher_refuses(self):
        verdict = gate.validate(
            "powershell -NoProfile -File C:\\flow\\probe.ps1",
            "powershell")
        self.assertFalse(verdict.ok)

    def test_external_operator_refused_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = str(root / "queue")
            key = os.urandom(32)
            key_file = root / "key.bin"
            key_file.write_bytes(key)
            with self.assertRaises(bridge.BridgeError):
                bridge.submit_job(
                    queue, key,
                    'powershell -Command "Write-Output ok" & echo injected',
                    "C:\\s", dialect="powershell")
            self.assertEqual(_jobs_files(queue), [])


class ProbeFailClosedTests(unittest.TestCase):
    """A broken parser probe is unavailable, never a silent pass."""

    def _run_with(self, returncode, stdout):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], returncode,
                                               stdout=stdout, stderr="boom")
        return fake_run

    def test_nonzero_exit_with_parseable_stdout_fails_closed(self):
        # Regression: nonzero exit with `[]` used to be treated as "no
        # syntax errors" and skipped the check.
        saved = gate.subprocess.run
        gate.subprocess.run = self._run_with(1, "[]")
        try:
            with self.assertRaises(gate.ValidatorUnavailable):
                gate._parse_with_powershell("Write-Output ok", "fake")
        finally:
            gate.subprocess.run = saved

    def test_empty_or_unreadable_output_fails_closed(self):
        saved = gate.subprocess.run
        try:
            for stdout in ("", "not json", "{}"):
                gate.subprocess.run = self._run_with(0, stdout)
                with self.subTest(stdout=stdout):
                    with self.assertRaises(gate.ValidatorUnavailable):
                        gate._parse_with_powershell("Write-Output ok", "fake")
        finally:
            gate.subprocess.run = saved

    def test_parser_message_never_carries_command_secrets(self):
        secret = "SECRET_ZXCV_987654321"
        saved = gate._parse_with_powershell
        gate._parse_with_powershell = lambda script, exe: [
            {"message": f"Unexpected token '{secret}'",
             "start": 3, "errorId": "UnexpectedToken"}]
        try:
            verdict = gate.validate(
                f'powershell -Command "Get-Item {secret} )("',
                "powershell")
        finally:
            gate._parse_with_powershell = saved
        self.assertFalse(verdict.ok)
        self.assertNotIn(secret, verdict.message)


class AdapterRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key = os.urandom(32)
        self.key_file = self.root / "key.bin"
        self.key_file.write_bytes(self.key)
        self.addCleanup(self.tmp.cleanup)

    def _cli_submit(self, command_text, *extra, env=None):
        cmd_file = self.root / "cmd.txt"
        cmd_file.write_text(command_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "submit",
             "--queue", self.queue,
             "--key-file", str(self.key_file),
             "--command-file", str(cmd_file),
             "--cwd", "C:\\lenovo-skill"] + list(extra),
            capture_output=True, text=True, env=env)

    def test_missing_backend_fails_closed_before_write(self):
        saved = os.environ.get("TASK_RUNNER_POWERSHELL_BIN")
        os.environ["TASK_RUNNER_POWERSHELL_BIN"] = str(
            self.root / "no-such-powershell-xyz")
        try:
            proc = self._cli_submit(VALID_CALL, "--dialect", "powershell")
        finally:
            if saved is None:
                os.environ.pop("TASK_RUNNER_POWERSHELL_BIN", None)
            else:
                os.environ["TASK_RUNNER_POWERSHELL_BIN"] = saved
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("JOBID=", proc.stdout)
        self.assertIn("unavailable", proc.stderr)
        self.assertEqual(_jobs_files(self.queue), [])
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)

    @unittest.skipUnless(PS_RUNTIME, "no powershell runtime")
    def test_unclosed_quote_refuses_with_offset(self):
        verdict = gate.validate(
            'powershell -NoProfile -Command "Get-ChildItem \'C:\\x"',
            "powershell")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.dialect, "powershell")
        self.assertTrue(verdict.message)
        self.assertIsNotNone(verdict.position)

    @unittest.skipUnless(PS_RUNTIME, "no powershell runtime")
    def test_cli_syntax_error_refuses_before_write(self):
        proc = self._cli_submit(
            'powershell -NoProfile -Command "Get-ChildItem \'C:\\x"',
            "--dialect", "powershell")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("JOBID=", proc.stdout)
        self.assertIn("powershell", proc.stderr)
        self.assertEqual(_jobs_files(self.queue), [])

    @unittest.skipUnless(PS_RUNTIME, "no powershell runtime")
    def test_wrapped_call_operator_shape_passes(self):
        verdict = gate.validate(VALID_CALL, "powershell")
        self.assertTrue(verdict.ok, verdict.message)
        # A command file normally ends with a newline; only internal
        # line breaks are ambiguous for the worker cmd shell.
        self.assertTrue(gate.validate(VALID_CALL + "\n", "powershell").ok)
        proc = self._cli_submit(VALID_CALL, "--dialect", "powershell")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("JOBID=", proc.stdout)
        self.assertEqual(len(_jobs_files(self.queue)), 1)

    @unittest.skipUnless(PS_RUNTIME, "no powershell runtime")
    def test_nonexistent_cmdlet_is_not_blocked(self):
        verdict = gate.validate(
            'powershell -NoProfile -Command '
            '"Invoke-NonexistentCmdlet-XYZ-12345 -Foo 1"', "powershell")
        self.assertTrue(verdict.ok, verdict.message)


if __name__ == "__main__":
    unittest.main()
