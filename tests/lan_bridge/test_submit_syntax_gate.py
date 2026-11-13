"""Submit syntax gate contract tests (issue #30): seam, dialect, fail-closed.

The caller knows only ``validate(command, dialect)`` and its verdict;
adapters are exercised through that seam. Stub adapters stand in for any
dialect so these tests prove the wiring (refuse before write, explicit
dialect, fail-closed, secret-free diagnostics, legacy pass-through)
without a PowerShell runtime.
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


def _stub_invalid(command):
    return gate.SyntaxVerdict(False, "stub", "stub syntax error", position=3)


def _stub_valid(command):
    return gate.SyntaxVerdict(True, "stub", "syntax ok")


def _stub_unavailable(command):
    raise gate.ValidatorUnavailable("stub backend missing")


def _jobs_files(queue):
    jobs = Path(queue) / "jobs"
    if not jobs.is_dir():
        return []
    return sorted(p for p in jobs.iterdir() if p.suffix == ".json")


class SeamContractTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(gate._ADAPTERS)
        self.addCleanup(self._restore)

    def _restore(self):
        gate._ADAPTERS.clear()
        gate._ADAPTERS.update(self._saved)

    def test_ambiguous_dialect_refused_without_guessing(self):
        gate._ADAPTERS["stub"] = _stub_valid
        for bad in ("", "auto", "powershell,cmd", None, 123):
            verdict = gate.validate("dir C:\\x", bad)
            self.assertFalse(verdict.ok, msg=repr(bad))
            self.assertIn("powershell|cmd", verdict.message, msg=repr(bad))

    def test_short_secret_is_redacted(self):
        # Regression: a length-4 token floor leaked short secrets.
        self.assertEqual(gate.sanitize_diagnostic("abc", "echo abc"),
                         "[redacted]")
        self.assertEqual(gate.sanitize_diagnostic("x", "echo x"),
                         "[redacted]")
        two = gate.sanitize_diagnostic("token 'xy' here", "run xy")
        self.assertNotIn("xy", two)

    def test_nul_and_disabled_gate(self):
        self.assertFalse(gate.validate('powershell -Command "x\0y"',
                                       'powershell').ok)
        self.assertIsNone(gate.check('anything', None))


class SubmitGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = str(self.root / "queue")
        self.key_file = self.root / "key.bin"
        self.key = os.urandom(32)
        self.key_file.write_bytes(self.key)
        self._saved = dict(gate._ADAPTERS)
        self.addCleanup(self._restore)

    def _restore(self):
        gate._ADAPTERS.clear()
        gate._ADAPTERS.update(self._saved)
        self.tmp.cleanup()

    def _cli_submit(self, command_text, *extra):
        cmd_file = self.root / "cmd.txt"
        cmd_file.write_text(command_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "submit",
             "--queue", self.queue,
             "--key-file", str(self.key_file),
             "--command-file", str(cmd_file),
             "--cwd", "C:\\lenovo-skill"] + list(extra),
            capture_output=True, text=True)

    def test_invalid_syntax_refuses_before_write(self):
        gate._ADAPTERS["stub"] = _stub_invalid
        with self.assertRaises(bridge.BridgeError) as ctx:
            bridge.submit_job(self.queue, self.key, "anything",
                              "C:\\s", dialect="stub")
        self.assertIn("stub", str(ctx.exception))
        self.assertEqual(_jobs_files(self.queue), [])

    def test_ambiguous_dialect_refuses_before_write(self):
        with self.assertRaises(bridge.BridgeError) as ctx:
            bridge.submit_job(self.queue, self.key, "dir C:\\x",
                              "C:\\s", dialect="auto")
        self.assertIn("powershell|cmd", str(ctx.exception))
        self.assertEqual(_jobs_files(self.queue), [])

    def test_unavailable_validator_fails_closed(self):
        gate._ADAPTERS["stub"] = _stub_unavailable
        with self.assertRaises(bridge.BridgeError) as ctx:
            bridge.submit_job(self.queue, self.key, "dir C:\\x",
                              "C:\\s", dialect="stub")
        self.assertIn("unavailable", str(ctx.exception))
        self.assertEqual(_jobs_files(self.queue), [])

    def test_valid_syntax_uses_legacy_seal_publish_path(self):
        gate._ADAPTERS["stub"] = _stub_valid
        job_id = bridge.submit_job(
            self.queue, self.key, "Invoke-NonexistentCmdlet-XYZ -Foo 1",
            "C:\\s", dialect="stub")
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        payload = bridge.open_envelope(
            bridge.read(str(Path(self.queue) / "jobs"), job_id),
            self.key, "job", job_id)
        self.assertEqual(payload["command"],
                         "Invoke-NonexistentCmdlet-XYZ -Foo 1")

    def test_disabled_gate_keeps_legacy_behavior(self):
        job_id = bridge.submit_job(self.queue, self.key, "& 'unclosed",
                                   "C:\\s")
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        self.assertEqual(len(_jobs_files(self.queue)), 1)

    def test_cli_cmd_refusal_before_write_without_secrets(self):
        secret = "TOPSECRET_ZXCV_9876"
        proc = self._cli_submit(f"& 'C:\\a.py' '{secret}'",
                                "--dialect", "cmd")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("JOBID=", proc.stdout)
        self.assertIn("dialect=cmd", proc.stderr)
        self.assertNotIn(secret, proc.stdout + proc.stderr)
        self.assertNotIn(self.key.hex(), proc.stdout + proc.stderr)
        self.assertEqual(_jobs_files(self.queue), [])

    def test_cli_valid_cmd_passes_legacy_path(self):
        proc = self._cli_submit("dir C:\\x", "--dialect", "cmd")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("JOBID=", proc.stdout)
        self.assertEqual(len(_jobs_files(self.queue)), 1)


if __name__ == "__main__":
    unittest.main()
