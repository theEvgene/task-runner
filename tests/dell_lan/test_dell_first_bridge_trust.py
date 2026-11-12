"""Bridge trust regression for the first verified import (#21, #22).

The bridge client must be hashed at its resolved configured path and
compared to the trusted pin *before* any byte is executed; the common
dependency digest must never come from the mutable ``module.__file__``
attribute. Every case runs the real signed queue and the real drivers
against a disposable fixture (local bare remote, no network).
"""

from __future__ import annotations

import hashlib
import sys
import types
import unittest
from pathlib import Path

from first_fixture import (
    SCRIPTS, FirstWorkspace, bridge_digest, common_digest, dell_digest,
)

sys.path.insert(0, str(SCRIPTS))

import dell_lan_common as common_mod  # noqa: E402
import dell_first_import as first_import  # noqa: E402

REAL_BRIDGE_DIR = SCRIPTS.parent / "lan_bridge"


def _args(bridge_pin: str, common_pin: str | None = None):
    return types.SimpleNamespace(
        expected_dell_driver_sha256=dell_digest(),
        expected_dell_common_sha256=common_pin or common_digest(),
        expected_bridge_sha256=bridge_pin,
    )


def _blank_receipt():
    return {"dellDriver": {}, "bridge": {}}


class BridgeTrustRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = FirstWorkspace().build()
        cls.job = cls.ws.request_first("0000000000000b01")
        cls.ws.run_job(cls.job)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    # A mismatched bridge must never run: its import-time side effect
    # must leave no marker behind.
    def test_mismatched_bridge_never_executes(self):
        evil_dir = self.ws.root / "evil-bridge"
        evil_dir.mkdir()
        marker = self.ws.root / "evil-bridge-marker.txt"
        (evil_dir / "lan_bridge.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('pwned\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        wrong = "0" * 64 if bridge_digest() != "0" * 64 else "1" * 64
        proc, data = self.ws.import_to(
            self.ws.root / "copy-evil-bridge", self.job,
            "--bridge-dir", str(evil_dir),
            "--expected-bridge-sha256", wrong)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "driver-stale")
        self.assertFalse(marker.exists(), "mismatched bridge was executed")
        self.assertFalse((self.ws.root / "copy-evil-bridge").exists())

    # A syntactically broken bridge whose pin matches is still a bounded
    # receipt, never an unhandled traceback with no receipt.
    def test_malformed_bridge_is_a_bounded_failure(self):
        broken_dir = self.ws.root / "broken-bridge"
        broken_dir.mkdir()
        broken = broken_dir / "lan_bridge.py"
        broken.write_text("def broken(:\n  this is not python\n", encoding="utf-8")
        pin = hashlib.sha256(broken.read_bytes()).hexdigest()
        proc, data = self.ws.import_to(
            self.ws.root / "copy-broken-bridge", self.job,
            "--bridge-dir", str(broken_dir),
            "--expected-bridge-sha256", pin)
        self.assertEqual(proc.returncode, 1)
        self.assertIsNotNone(data, f"no receipt written: {proc.stderr}")
        self.assertEqual(data["reason"], "driver-stale")
        self.assertIn("bridge import failed", data["detail"])
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse((self.ws.root / "copy-broken-bridge").exists())

    # A rewritten ``dell_lan_common.__file__`` must neither forge a pass
    # nor break a valid check: the digest comes from the spec origin.
    def test_spoofed_common_file_attr_is_ignored(self):
        decoy = self.ws.root / "decoy-common.py"
        decoy.write_bytes(b"# decoy, never the dependency\n")
        decoy_pin = hashlib.sha256(decoy.read_bytes()).hexdigest()
        real = common_mod.__file__
        try:
            common_mod.__file__ = str(decoy)
            # Correct pins still pass: the spoofed attribute is not read.
            receipt = _blank_receipt()
            first_import.verify_dell_driver(
                _args(bridge_digest()), receipt, REAL_BRIDGE_DIR)
            self.assertEqual(receipt["dellDriver"]["freshness"], "verified-pinned")
            # The decoy's digest is not accepted: only the real file can pass.
            with self.assertRaises(common_mod.Stop) as raised:
                first_import.verify_dell_driver(
                    _args(bridge_digest(), common_pin=decoy_pin),
                    _blank_receipt(), REAL_BRIDGE_DIR)
            self.assertEqual(raised.exception.reason, "driver-stale")
        finally:
            common_mod.__file__ = real


if __name__ == "__main__":
    unittest.main()
