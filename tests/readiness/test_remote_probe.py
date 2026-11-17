"""Fast contract tests for the extracted Lenovo-side probe program.

The readiness check builds exactly one signed command from
``remote_probe.PROGRAM``.  These tests prove the extraction kept that program
byte-identical, runnable and the one the Dell side actually submits.  They use
no queue, host, share, key or network.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READINESS_DIR = ROOT / "scripts" / "readiness"
sys.path.insert(0, str(READINESS_DIR))

import check  # noqa: E402
import remote_probe  # noqa: E402


class RemoteProbeTests(unittest.TestCase):
    def test_the_submitted_command_carries_the_extracted_program(self):
        config = {"scenario": "lan-bridge",
                  "lenovo": {"python": sys.executable, "cwd": ".",
                             "components": [], "tools": []}}
        encoded = base64.b64encode(
            remote_probe.PROGRAM.encode("utf-8")).decode("ascii")
        self.assertEqual(check.REMOTE_PROBE, remote_probe.PROGRAM)
        self.assertIn(encoded, check._remote_command(config))

    def test_the_program_still_emits_only_states_and_the_fixed_schema(self):
        config = {"scenario": "lan-bridge", "components": [], "tools": []}
        data = base64.b64encode(
            json.dumps(config).encode("utf-8")).decode("ascii")
        proc = subprocess.run(
            [sys.executable, "-c", remote_probe.PROGRAM, data],
            capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         {"schema": "lenovo-readiness-probe/1",
                          "scenario": "lan-bridge", "components": []})


if __name__ == "__main__":
    unittest.main()
