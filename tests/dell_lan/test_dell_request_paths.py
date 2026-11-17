"""Request writer: every Lenovo-side path it embeds must be absolute there.

The command file crosses to Lenovo and is executed as written, so an
interpreter name the writer merely hoped to resolve (`python` on the worker's
PATH) is not a pin. The SKILL documents `--python` as the absolute interpreter
path on the Lenovo machine; these cases prove the writer enforces exactly that
instead of falling back, and that a safe request embeds the interpreter that
was actually supplied.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from dell_fixture import BRIDGE_CLI, REQUEST_CLI, ROOT, lan_bridge, run
from publish_receipt_gate import documented_command_kind
from shell_command import (
    ShellDialectError, build as build_shell_command, build_for_transport,
)

WORKER_CLI = ROOT / "scripts" / "lenovo_worker" / "worker.py"

SHA_A = "a" * 40
SHA_B = "b" * 40
TOKEN = "0123456789abcdef"


class RequestPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-request-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = self.root / "lan-share"
        self.shared.mkdir()
        self.skill_dir = self.root / "lenovo-skill"
        self.skill_dir.mkdir()
        self.lenovo = self.root / "lenovo-checkout"
        self.lenovo.mkdir()
        self.command_file = self.root / "command.txt"
        self.python = str(Path(sys.executable).resolve())

    def base_args(self, **overrides: str) -> list[str]:
        values = {
            "--shared": str(self.shared),
            "--token": TOKEN,
            "--lenovo-skill-dir": str(self.skill_dir),
            "--python": self.python,
            "--lenovo-checkout": str(self.lenovo),
            "--ref-full": "refs/heads/main",
            "--request-name": "base-request.json",
            "--command-file": str(self.command_file),
        }
        values.update(overrides)
        args = [sys.executable, str(REQUEST_CLI), "base"]
        for name, value in values.items():
            if value is not None:
                args += [name, value]
        return args

    def publish_args(self, **overrides: str) -> list[str]:
        description = self.shared / "mr-description.md"
        description.write_text("Fixture change.\n", encoding="utf-8")
        (self.shared / "transfer.manifest.json").write_text("{}", encoding="utf-8")
        (self.shared / "transfer.bundle").write_bytes(b"bundle")
        values = {
            "--shared": str(self.shared),
            "--token": TOKEN,
            "--lenovo-skill-dir": str(self.skill_dir),
            "--python": self.python,
            "--dialect": "cmd",
            "--manifest": str(self.shared / "transfer.manifest.json"),
            "--bundle": str(self.shared / "transfer.bundle"),
            "--description-file": str(description),
            "--expected-base": SHA_A,
            "--expected-commit": SHA_B,
            "--branch": "task/iteration-1",
            "--target-branch": "main",
            "--host": "gitlab.fixture.invalid",
            "--project": "group/project",
            "--change-type": "feat",
            "--jira-key": "ABC-1",
            "--subject": "fixture task",
            "--state": "draft",
            "--authorize": "import,push,mr",
            "--lenovo-checkout": str(self.lenovo),
            "--lenovo-worktree": str(self.root / "lenovo-task-worktree"),
            "--request-name": "publish-request.json",
            "--command-file": str(self.command_file),
        }
        values.update(overrides)
        args = [sys.executable, str(REQUEST_CLI), "publish"]
        for name, value in values.items():
            if value is not None:
                args += [name, value]
        return args

    def test_a_relative_python_is_refused_for_a_base_request(self):
        proc = run(self.base_args(**{"--python": "python"}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--python", proc.stderr)
        self.assertIn("absolute", proc.stderr)
        self.assertFalse(self.command_file.exists())

    def test_a_missing_python_is_refused_for_a_base_request(self):
        proc = run(self.base_args(**{"--python": None}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--python", proc.stderr)
        self.assertFalse(self.command_file.exists())

    def test_a_relative_lenovo_checkout_is_refused(self):
        proc = run(self.base_args(**{"--lenovo-checkout": "lenovo-checkout"}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("absolute", proc.stderr)

    def test_a_relative_python_is_refused_for_a_publish_request(self):
        proc = run(self.publish_args(**{"--python": "python"}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--python", proc.stderr)
        self.assertFalse(self.command_file.exists())

    def test_a_missing_python_is_refused_for_a_publish_request(self):
        proc = run(self.publish_args(**{"--python": None}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--python", proc.stderr)
        self.assertFalse(self.command_file.exists())

    def test_a_safe_base_request_embeds_the_exact_interpreter(self):
        proc = run(self.base_args())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        command = self.command_file.read_text(encoding="utf-8")
        self.assertEqual(command.strip(), result["command"])
        self.assertIn(f'"{self.python}"', command)
        self.assertIn("lenovo_base_export.py", command)
        self.assertNotIn('"python"', command.replace(f'"{self.python}"', ""))

    def test_the_writer_pins_the_expected_target_working_directory(self):
        proc = run(self.publish_args())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["expectedCwd"],
                         str(self.lenovo))
        override = self.root / "other-lenovo-worktree"
        override.mkdir()
        proc = run(self.publish_args(**{"--expected-cwd": str(override)}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["expectedCwd"], str(override))
        refused = run(self.publish_args(**{"--expected-cwd": "worktree"}))
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("absolute", refused.stderr)
        first = run([sys.executable, str(REQUEST_CLI), "first",
                     "--shared", str(self.shared), "--token", TOKEN,
                     "--lenovo-skill-dir", str(self.skill_dir),
                     "--python", self.python,
                     "--remote-url", "https://example.invalid/group/project.git",
                     "--ref-full", "refs/heads/main",
                     "--request-name", f"first-request-{TOKEN}.json",
                     "--command-file", str(self.command_file)])
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIsNone(json.loads(first.stdout)["expectedCwd"])

    def test_a_relative_expected_cwd_refuses_before_either_file_exists(self):
        request = self.shared / "publish-request.json"
        proc = run(self.publish_args(**{"--expected-cwd": "worktree"}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--expected-cwd", proc.stderr)
        self.assertFalse(request.exists())
        self.assertFalse(self.command_file.exists())

    def test_a_safe_publish_request_embeds_the_exact_interpreter(self):
        proc = run(self.publish_args())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        command = self.command_file.read_text(encoding="utf-8")
        self.assertIn(f'"{self.python}"', command)
        self.assertIn("lenovo_publish.py", command)

    def test_publish_requires_one_declared_dialect_before_writing(self):
        request = self.shared / "publish-request.json"
        proc = run(self.publish_args(**{"--dialect": None}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--dialect", proc.stderr)
        self.assertNotIn("JOBID=", proc.stdout + proc.stderr)
        self.assertFalse(request.exists())
        self.assertFalse(self.command_file.exists())
        self.assertFalse((self.root / "jobs").exists())

        proc = run(self.publish_args(**{"--dialect": "auto"}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("dialect", proc.stderr)
        self.assertNotIn("JOBID=", proc.stdout + proc.stderr)
        self.assertFalse(request.exists())
        self.assertFalse(self.command_file.exists())
        self.assertFalse((self.root / "jobs").exists())

        proc = run(self.publish_args() + ["--dialect", "powershell"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("exactly once", proc.stderr)
        self.assertNotIn("JOBID=", proc.stdout + proc.stderr)
        self.assertFalse(request.exists())
        self.assertFalse(self.command_file.exists())
        self.assertFalse((self.root / "jobs").exists())

    def test_generated_publish_command_executes_in_both_declared_shells(self):
        driver_dir = self.root / "driver dir's fixture"
        driver_dir.mkdir()
        driver = driver_dir / "lenovo_publish.py"
        driver_source = (
            "import argparse\n"
            "from pathlib import Path\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--request', required=True)\n"
            "p.add_argument('--request-sha256', required=True)\n"
            "p.add_argument('--reconcile', action='store_true')\n"
            "a = p.parse_args()\n"
            "Path(a.request + '.ran').write_text("
            "('reconciled' if a.reconcile else 'executed') + '\\n', encoding='utf-8')\n")
        driver.write_text(driver_source, encoding="utf-8")
        powershell_driver_dir = self.root / "driver %literal! dir's fixture"
        powershell_driver_dir.mkdir()
        (powershell_driver_dir / "lenovo_publish.py").write_text(
            driver_source, encoding="utf-8")
        self.assertIsNotNone(
            os.environ.get("ComSpec") or shutil.which("cmd.exe"),
            "cmd.exe is required by this Windows fixture")
        self.assertIsNotNone(
            shutil.which("powershell.exe") or shutil.which("pwsh.exe"),
            "PowerShell is required by this fixture")
        queue = self.root / "queue"
        (queue / "jobs").mkdir(parents=True)
        (queue / "results").mkdir()
        key_file = self.root / "queue-key.bin"
        key = os.urandom(32)
        key_file.write_bytes(key)

        for dialect in ("cmd", "powershell"):
            for reconcile in (False, True):
                with self.subTest(dialect=dialect, reconcile=reconcile):
                    request_name = f"publish-{dialect}-{reconcile}.json"
                    command_file = self.root / f"command-{dialect}-{reconcile}.txt"
                    proc = run(self.publish_args(**{
                        "--dialect": dialect,
                        "--lenovo-driver-dir": str(
                            powershell_driver_dir if dialect == "powershell"
                            else driver_dir),
                        "--request-name": request_name,
                        "--command-file": str(command_file),
                    }))
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    result = json.loads(proc.stdout)
                    self.assertEqual(result["dialect"], dialect)
                    if reconcile:
                        command_file.write_text(
                            result["reconcileCommand"], encoding="utf-8")
                    command = command_file.read_text(encoding="utf-8").strip()
                    if dialect == "powershell":
                        self.assertTrue(command.startswith("powershell.exe "), command)
                        self.assertIn('-Command "& \'', command)
                    else:
                        self.assertTrue(command.startswith('"'), command)
                        self.assertFalse(command.startswith("&"), command)
                    self.assertEqual(documented_command_kind(
                        command, result["receiptScriptPath"]), "publish-driver")
                    submitted = run([
                        sys.executable, str(BRIDGE_CLI), "submit",
                        "--queue", str(queue), "--key-file", str(key_file),
                        "--command-file", str(command_file), "--cwd", str(self.root),
                        "--dialect", dialect, "--receipt-script-path",
                        result["receiptScriptPath"],
                    ])
                    self.assertEqual(submitted.returncode, 0, submitted.stderr)
                    self.assertIn("JOBID=", submitted.stdout)
                    job_id = submitted.stdout.strip().split("JOBID=", 1)[1]
                    job = lan_bridge.open_envelope(
                        (queue / "jobs" / f"{job_id}.json").read_bytes(),
                        key, "job", job_id)
                    self.assertEqual(job["command"], command)
                    self.assertEqual(job["dialect"], dialect)
                    worker = run([
                        sys.executable, str(WORKER_CLI), "--queue", str(queue),
                        "--key-file", str(key_file), "--shared", str(self.shared),
                        "--once",
                    ])
                    self.assertEqual(worker.returncode, 0, worker.stderr)
                    sealed = (queue / "results" / f"{job_id}.json").read_bytes()
                    outcome = lan_bridge.open_envelope(sealed, key, "result", job_id)
                    self.assertEqual(outcome["status"], "ok", (command, outcome))
                    self.assertEqual(outcome["exit_code"], 0, (command, outcome))
                    marker = self.shared / f"{request_name}.ran"
                    expected = "reconciled\n" if reconcile else "executed\n"
                    self.assertEqual(marker.read_text(encoding="utf-8"), expected)

    def test_powershell_builder_is_literal_before_transport_adaptation(self):
        parts = [self.python, r"C:\100%!\driver's fixture.py", 'literal"quote']
        program = build_shell_command(parts, "powershell")
        self.assertTrue(program.startswith("& "), program)
        self.assertIn("100%!", program)
        self.assertIn("driver''s fixture.py", program)
        self.assertIn('literal"quote', program)

        safe_transport = build_for_transport(parts[:-1], "powershell")
        self.assertTrue(safe_transport.startswith("powershell.exe "), safe_transport)
        self.assertIn('-Command "& \'', safe_transport)
        with self.assertRaisesRegex(ShellDialectError, "transport-unsafe"):
            build_for_transport(parts, "powershell")

    def test_cmd_expansion_markers_refuse_before_any_write(self):
        request = self.shared / "publish-request.json"
        injected = self.root / "injected.txt"
        env = dict(os.environ)
        env["T3_QUOTE_SENTINEL"] = (
            f'literal" & echo INJECTED>"{injected}" & rem "')
        proc = run(self.publish_args(**{
            "--dialect": "cmd",
            "--lenovo-driver-dir": str(self.root / "%T3_QUOTE_SENTINEL%"),
        }), env=env)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("cmd", proc.stderr)
        self.assertIn("environment expansion", proc.stderr)
        self.assertNotIn("T3_QUOTE_SENTINEL", proc.stderr)
        self.assertNotIn("JOBID=", proc.stdout + proc.stderr)
        self.assertFalse(request.exists())
        self.assertFalse(self.command_file.exists())
        self.assertFalse(injected.exists())
        self.assertFalse((self.root / "jobs").exists())

        for unsafe in ("%NAME%", "!NAME!", 'literal" & echo injected'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ShellDialectError):
                build_shell_command([self.python, unsafe], "cmd")


if __name__ == "__main__":
    unittest.main()
