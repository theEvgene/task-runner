"""Task-runner home for the Dell LAN loop: layout defaults and driver options.

The moved drivers keep their legacy ``<lenovo-skill-dir>/scripts/<driver>``
construction when no driver directory is named, and resolve the signed bridge
and the external publication flow from this repository otherwise. These cases
pin that contract: the in-checkout defaults, the explicit overrides, and the
refusal of a relative driver directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dell_fixture import REQUEST_CLI, ROOT, SCRIPTS, run

sys.path.insert(0, str(SCRIPTS))

import dell_lan_common as common  # noqa: E402  (module under test)


class TaskRunnerHomeTests(unittest.TestCase):
    def test_default_bridge_dir_is_the_task_runner_sibling(self):
        self.assertEqual(common.default_bridge_dir(),
                         ROOT / "scripts" / "lan_bridge")

    def test_bridge_dir_env_override_is_honored(self):
        with mock.patch.dict(os.environ,
                             {"DELL_LAN_BRIDGE_DIR": str(ROOT / "elsewhere")}):
            self.assertEqual(common.default_bridge_dir(),
                             Path(str(ROOT / "elsewhere")))

    def test_flow_dir_env_override_is_honored(self):
        with mock.patch.dict(os.environ,
                             {"DELL_LAN_FLOW_DIR": str(ROOT / "flow-elsewhere")}):
            self.assertEqual(common.default_flow_dir(),
                             Path(str(ROOT / "flow-elsewhere")))

    def test_default_flow_dir_is_external_not_vendored(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DELL_LAN_FLOW_DIR", None)
            flow = common.default_flow_dir()
        self.assertNotEqual(flow.parents[1].name, "dell_lan",
                            "the publication flow must not resolve inside the moved drivers")

    def test_legacy_skill_dir_construction_is_preserved(self):
        root = Path(tempfile.mkdtemp(prefix="dell-home-legacy-"))
        self.addCleanup(shutil.rmtree, root, True)
        shared = root / "lan-share"
        shared.mkdir()
        skill = root / "lenovo-skill"
        skill.mkdir()
        command_file = root / "command.txt"
        proc = run([sys.executable, str(REQUEST_CLI), "base",
                    "--shared", str(shared), "--token", "0123456789abcdef",
                    "--lenovo-skill-dir", str(skill),
                    "--python", str(Path(sys.executable).resolve()),
                    "--lenovo-checkout", str(root / "lenovo"),
                    "--ref-full", "refs/heads/main",
                    "--request-name", "base-request.json",
                    "--command-file", str(command_file)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        command = command_file.read_text(encoding="utf-8")
        self.assertIn(str(skill / "scripts" / "lenovo_base_export.py"), command)

    def test_driver_dir_embeds_the_direct_driver_path(self):
        root = Path(tempfile.mkdtemp(prefix="dell-home-driver-"))
        self.addCleanup(shutil.rmtree, root, True)
        shared = root / "lan-share"
        shared.mkdir()
        command_file = root / "command.txt"
        driver_dir = root / "lenovo-drivers"
        driver_dir.mkdir()
        proc = run([sys.executable, str(REQUEST_CLI), "base",
                    "--shared", str(shared), "--token", "0123456789abcdef",
                    "--lenovo-skill-dir", str(root / "lenovo-skill"),
                    "--lenovo-driver-dir", str(driver_dir),
                    "--python", str(Path(sys.executable).resolve()),
                    "--lenovo-checkout", str(root / "lenovo"),
                    "--ref-full", "refs/heads/main",
                    "--request-name", "base-request.json",
                    "--command-file", str(command_file)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        command = command_file.read_text(encoding="utf-8")
        self.assertIn(str(driver_dir / "lenovo_base_export.py"), command)

    def test_relative_driver_dir_is_refused(self):
        root = Path(tempfile.mkdtemp(prefix="dell-home-relative-"))
        self.addCleanup(shutil.rmtree, root, True)
        shared = root / "lan-share"
        shared.mkdir()
        command_file = root / "command.txt"
        proc = run([sys.executable, str(REQUEST_CLI), "base",
                    "--shared", str(shared), "--token", "0123456789abcdef",
                    "--lenovo-skill-dir", str(root / "lenovo-skill"),
                    "--lenovo-driver-dir", "relative/drivers",
                    "--python", str(Path(sys.executable).resolve()),
                    "--lenovo-checkout", str(root / "lenovo"),
                    "--ref-full", "refs/heads/main",
                    "--request-name", "base-request.json",
                    "--command-file", str(command_file)])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--lenovo-driver-dir", proc.stderr)
        self.assertFalse(command_file.exists())


if __name__ == "__main__":
    unittest.main()
