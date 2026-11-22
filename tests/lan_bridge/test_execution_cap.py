"""Execution cap: one command runs at most 600 s from spawn (issue #168).

Client side: new submissions accept 1..600 and refuse 601+; smaller
requested timeouts pass through unchanged; legacy signed deadlines report
the enforced 600 s while keeping the requested value for transparency.
No ten-minute waits: boundaries are proved by validation, not by sleeping.
"""

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "lan_bridge"
sys.path.insert(0, str(SCRIPT_DIR))

import lan_bridge as bridge  # noqa: E402
import job_payload as payload  # noqa: E402
import wait  # noqa: E402


class ExecutionCapValidationTests(unittest.TestCase):
    def test_boundaries_599_600_601(self):
        self.assertEqual(bridge.validate_timeout(599), 599)
        self.assertEqual(bridge.validate_timeout(600), 600)
        for bad in (601, 3600, 0, -1, True, False, "600", 600.0):
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                bridge.validate_timeout(bad)

    def test_payload_boundaries(self):
        job_id = "c" * 32
        for good in (1, 59, 599, 600):
            payload.build_job_payload(
                job_id=job_id, command="echo hi",
                working_directory="C:\\work", timeout_seconds=good)
        for bad in (601, 3600, 0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                payload.build_job_payload(
                    job_id=job_id, command="echo hi",
                    working_directory="C:\\work", timeout_seconds=bad)

    def test_smaller_timeout_preserved(self):
        job_id = "d" * 32
        built = payload.build_job_payload(
            job_id=job_id, command="echo hi",
            working_directory="C:\\work", timeout_seconds=45)
        self.assertEqual(built["timeout_seconds"], 45)
        self.assertEqual(bridge.validate_timeout(45), 45)

    def test_effective_helper_clamps_legacy(self):
        self.assertEqual(payload.effective_timeout_seconds(599), 599)
        self.assertEqual(payload.effective_timeout_seconds(600), 600)
        self.assertEqual(payload.effective_timeout_seconds(601), 600)
        self.assertEqual(payload.effective_timeout_seconds(3600), 600)
        self.assertEqual(payload.effective_timeout_seconds(45), 45)
        for bad in (0, -1, 3601, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                payload.effective_timeout_seconds(bad)

    def test_legacy_readback_rebuild_never_widens_the_submit_gate(self):
        """Only a same-id readback may rebuild a legacy 601..3600 payload."""
        legacy = payload.build_job_payload(
            job_id="e" * 32, command="echo hi",
            working_directory="C:\\work", timeout_seconds=660,
            allow_legacy_timeout=True)
        self.assertEqual(legacy["timeout_seconds"], 660)
        for bad in (0, 601, 3600, 3601, True):
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                payload.build_job_payload(
                    job_id="e" * 32, command="echo hi",
                    working_directory="C:\\work", timeout_seconds=bad)
        for bad in (0, 3601, True):
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                payload.build_job_payload(
                    job_id="e" * 32, command="echo hi",
                    working_directory="C:\\work", timeout_seconds=bad,
                    allow_legacy_timeout=True)

    def test_signed_deadline_reports_effective_cap(self):
        capped = wait._signed_deadline({"timeout_seconds": 3600})
        self.assertIsNotNone(capped)
        assert capped is not None
        self.assertEqual(capped["seconds"], 600)
        self.assertEqual(capped["requested_seconds"], 3600)
        small = wait._signed_deadline({"timeout_seconds": 45})
        self.assertIsNotNone(small)
        assert small is not None
        self.assertEqual(small["seconds"], 45)
        self.assertEqual(small["requested_seconds"], 45)
        boundary = wait._signed_deadline({"timeout_seconds": 601})
        assert boundary is not None
        self.assertEqual(boundary["seconds"], 600)
        # Queue waiting consumes nothing: the deadline carries no clock.
        self.assertNotIn("started", capped)
        self.assertIsNone(wait._signed_deadline(None))
        self.assertIsNone(wait._signed_deadline({"timeout_seconds": 0}))


if __name__ == "__main__":
    unittest.main()
