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
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from dell_fixture import REQUEST_CLI, run

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

    def test_a_safe_publish_request_embeds_the_exact_interpreter(self):
        proc = run(self.publish_args())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        command = self.command_file.read_text(encoding="utf-8")
        self.assertIn(f'"{self.python}"', command)
        self.assertIn("lenovo_publish.py", command)


if __name__ == "__main__":
    unittest.main()
