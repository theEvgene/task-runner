"""Fast local fixtures for one host-bound selected check (Dell and receipt)."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "readiness" / "selected.py"
READINESS_DIR = ROOT / "scripts" / "readiness"
sys.path.insert(0, str(READINESS_DIR))
import selected  # noqa: E402
import selected_hosts  # noqa: E402


def _head() -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()


class SelectedCheckDellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="selected-check-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.cwd = self.temp / "target"
        self.cwd.mkdir()
        self.marker = self.temp / "spawned.txt"
        self.head = _head()
        self.descriptor_path = self.temp / "descriptor.json"

    def _descriptor(self, argv: list[str], *, cwd: Path | None = None,
                    expected: str | None = None, host: str = "dell",
                    **extra: object) -> dict:
        data = {
            "schema": "lan-selected-check/1",
            "check": {
                "name": "fixture", "host": host, "effect": "read-only",
                "argv": argv, "cwd": str(cwd or self.cwd),
                "expectedRevision": expected or self.head,
                "timeoutSeconds": 30,
            },
            "dell": {"revisionRepository": str(ROOT)},
        }
        data.update(extra)
        return data

    def _run(self, data: dict, *extra: str) -> subprocess.CompletedProcess:
        self.descriptor_path.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--descriptor",
             str(self.descriptor_path), *extra],
            capture_output=True, text=True, check=False)

    def _marker_argv(self) -> list[str]:
        script = ("from pathlib import Path; "
                  f"Path({str(self.marker)!r}).write_text('spawned')")
        return [sys.executable, "-c", script]

    def test_dell_positive_is_one_bounded_path_free_receipt(self) -> None:
        proc = self._run(self._descriptor(
            [sys.executable, "-c", "print('selected-check-ok')"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["schema"], "lan-selected-check-receipt/1")
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertEqual(receipt["requestedHost"], "dell")
        self.assertEqual(receipt["observedHost"], "dell")
        self.assertTrue(receipt["hostBound"])
        self.assertEqual(receipt["execution"]["exitCode"], 0)
        self.assertEqual(receipt["execution"]["kind"], "local-child")
        self.assertIsNone(receipt["execution"]["jobId"])
        self.assertEqual(receipt["revision"]["observed"], self.head)
        self.assertEqual(receipt["revision"]["state"], "confirmed")
        self.assertRegex(receipt["check"]["commandDigest"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.temp), proc.stdout + proc.stderr)
        self.assertNotIn("spawned", proc.stdout)

    def test_stale_revision_is_not_run_and_spawns_nothing(self) -> None:
        proc = self._run(self._descriptor(self._marker_argv(),
                                          expected="0" * 40))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["outcome"], "NOT_RUN")
        self.assertEqual(receipt["preflight"]["reason"], "revision-stale")
        self.assertEqual(receipt["revision"]["state"], "stale")
        self.assertIsNone(receipt["observedHost"])
        self.assertFalse(receipt["hostBound"])
        self.assertFalse(self.marker.exists(), "a refused preflight spawned the check")

    def test_missing_cwd_and_tool_are_not_run(self) -> None:
        cases = (
            {"cwd": self.temp / "absent", "argv": self._marker_argv(),
             "reason": "cwd-missing"},
            {"cwd": self.cwd,
             "argv": ["definitely-not-a-real-executable-xyz", "--version"],
             "reason": "tool-missing"},
        )
        for case in cases:
            with self.subTest(reason=case["reason"]):
                proc = self._run(self._descriptor(
                    case["argv"], cwd=case["cwd"]))
                self.assertEqual(proc.returncode, 1, proc.stderr)
                receipt = json.loads(proc.stdout)
                self.assertEqual(receipt["outcome"], "NOT_RUN")
                self.assertEqual(receipt["preflight"]["reason"], case["reason"])
                self.assertIsNone(receipt["execution"]["exitCode"])
                self.assertFalse(self.marker.exists())

    def test_nonzero_exit_is_fail_and_never_pass(self) -> None:
        proc = self._run(self._descriptor(
            [sys.executable, "-c", "raise SystemExit(7)"]))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["outcome"], "FAIL")
        self.assertEqual(receipt["execution"]["exitCode"], 7)
        self.assertEqual(receipt["preflight"]["state"], "confirmed")

    def test_descriptor_refusals(self) -> None:
        write_effect = self._descriptor([sys.executable, "-c", "pass"])
        write_effect["check"]["effect"] = "write"
        unknown_host = self._descriptor([sys.executable, "-c", "pass"])
        unknown_host["check"]["host"] = "mac"
        extra_field = self._descriptor([sys.executable, "-c", "pass"])
        extra_field["check"]["checks"] = []
        for data in (write_effect, unknown_host, extra_field):
            with self.subTest(data=data):
                proc = self._run(data)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn("selected check refused", proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_wrong_host_and_non_integer_exit_cannot_pass(self) -> None:
        preflight = selected_hosts._confirmed("a" * 40)
        execution = {"verified": True, "observedHost": "dell",
                     "revisionState": "confirmed", "state": "run",
                     "exitCode": 0, "timedOut": False, "truncated": False,
                     "jobId": "b" * 32, "kind": "signed-queue"}
        self.assertEqual(selected.decide("lenovo", preflight, execution),
                         "UNKNOWN")
        execution["observedHost"] = "lenovo"
        execution["exitCode"] = 0.0
        self.assertNotEqual(selected.decide("lenovo", preflight, execution),
                            "PASS")

    def _wrapper(self, cfg: dict) -> subprocess.CompletedProcess:
        data = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")
        program = base64.b64encode(
            selected_hosts.REMOTE_PROGRAM.encode("utf-8")).decode("ascii")
        code = "import base64;exec(base64.b64decode('" + program + "'))"
        return subprocess.run([sys.executable, "-c", code, data],
                              capture_output=True, text=True, check=False)

    def test_target_preamble_returns_the_real_child_exit(self) -> None:
        base = {"cwd": str(ROOT), "expectedRevision": self.head,
                "timeoutSeconds": 20, "git": "git"}
        for wanted in (0, 7):
            with self.subTest(wanted=wanted):
                proc = self._wrapper({
                    **base, "argv": [sys.executable, "-c",
                                     f"raise SystemExit({wanted})"]})
                self.assertEqual(proc.returncode, wanted, proc.stderr)
                receipt = json.loads(proc.stdout)
                self.assertEqual(receipt["state"], "RUN")
                self.assertEqual(receipt["exitCode"], wanted)
                self.assertEqual(receipt["observedRevision"], self.head)

    def test_target_preamble_fails_a_check_timeout(self) -> None:
        proc = self._wrapper({
            "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
            "cwd": str(ROOT), "expectedRevision": self.head,
            "timeoutSeconds": 1, "git": "git"})
        self.assertEqual(proc.returncode, 79, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["state"], "FAIL")
        self.assertEqual(receipt["reason"], "timeout")

    def test_target_preamble_refuses_stale_revision_before_the_check(self) -> None:
        proc = self._wrapper({
            "argv": self._marker_argv(), "cwd": str(ROOT),
            "expectedRevision": "0" * 40, "timeoutSeconds": 20, "git": "git"})
        self.assertEqual(proc.returncode, 78, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["state"], "NOT_RUN")
        self.assertEqual(receipt["reason"], "revision-stale")
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
