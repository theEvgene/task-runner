"""Receipt gate: published only on ok + readback + matching head_sha.

Deterministic unit tests over the structured driver envelope. No live
Lenovo/GitLab, no queue, no network. Covers the #36 gates: ok path,
mismatch, absent/unavailable readback, and unknown staying exit 3
with a same-id reconcile (never a blind retry or a second MR).
"""

from __future__ import annotations

import sys
import unittest
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan"
sys.path.insert(0, str(SCRIPTS))

from publish_receipt_gate import (
    classify_driver, documented_command_kind, is_published, reconcile_action,
)
import dell_readback_checks as checks
import lenovo_publish

TASK = "a" * 40
OTHER = "b" * 40


def driver(outcome="ok", stage="readback", head=TASK, schema="dell-lan-publish-result/1"):
    return {"schema": schema, "outcome": outcome, "stage": stage,
            "identity": {"head_sha": head, "mr_url": "https://x/mr/1", "iid": 1},
            "push": {"observedRemoteSha": head},
            "mr": {"url": "https://x/mr/1", "iid": 1,
                   "descriptionVerified": True,
                   "descriptionExpectedSha256": "c" * 64,
                   "descriptionObservedSha256": "c" * 64},
            "error": None}


class PublishedGateTests(unittest.TestCase):
    def test_ok_readback_match_is_published(self):
        self.assertTrue(is_published(driver(), TASK))
        verdict, code, _ = classify_driver(driver(), TASK)
        self.assertEqual((verdict, code), ("published", 0))

    def test_mismatch_is_not_success(self):
        self.assertFalse(is_published(driver(head=OTHER), TASK))
        verdict, code, reason = classify_driver(driver(head=OTHER), TASK)
        self.assertNotEqual(verdict, "published")
        self.assertEqual(code, 1)
        self.assertEqual(reason, "publication-head-mismatch")

    def test_absent_identity_is_not_success(self):
        doc = driver()
        doc["identity"] = None
        self.assertFalse(is_published(doc, TASK))
        verdict, code, reason = classify_driver(doc, TASK)
        self.assertEqual(code, 1)
        self.assertEqual(reason, "publication-absent")

    def test_wrong_stage_is_not_success(self):
        doc = driver(stage="mr")
        self.assertFalse(is_published(doc, TASK))
        verdict, code, _ = classify_driver(doc, TASK)
        self.assertNotEqual(verdict, "published")

    def test_unknown_stays_unknown_exit_3(self):
        doc = driver(outcome="unknown", stage="mr")
        doc["error"] = {"reason": "mr-write-unknown"}
        verdict, code, reason = classify_driver(doc, TASK)
        self.assertEqual((verdict, code), ("unknown", 3))
        self.assertEqual(reason, "outcome-unknown")
        action = reconcile_action(reason)
        self.assertIn("SAME job id", action)
        self.assertNotIn("new job id", action.lower().replace("then a new job id", ""))

    def test_secret_free_reasons(self):
        verdict, code, reason = classify_driver(driver(head=OTHER), TASK)
        self.assertNotIn("SECRET", reason)


class AuthenticatedCommandTests(unittest.TestCase):
    def test_driver_binds_to_actual_script_position(self):
        command = ('"C:\\Python\\python.exe" '
                   '"C:\\skill\\scripts\\lenovo_publish.py" --request "x"\n')
        self.assertEqual(documented_command_kind(
            command, r"C:\skill\scripts\lenovo_publish.py"), "publish-driver")

    def test_driver_same_basename_at_attacker_path_cannot_forge_binding(self):
        command = r'python C:\attacker\lenovo_publish.py --request x'
        self.assertIsNone(documented_command_kind(
            command, r"C:\skill\scripts\lenovo_publish.py"))

    def test_driver_name_in_argument_cannot_forge_binding(self):
        self.assertIsNone(documented_command_kind(
            'python evil.py --label lenovo_publish.py', r"C:\skill\lenovo_publish.py"))
        self.assertIsNone(documented_command_kind(
            'echo lenovo_publish.py', r"C:\skill\lenovo_publish.py"))

    def test_identity_capture_binds_script_and_subcommand_positions(self):
        self.assertEqual(documented_command_kind(
            r'python C:\flow\mr_identity.py capture --url "https://example/mr/1"',
            r"C:\flow\mr_identity.py"),
            "direct-identity")
        self.assertIsNone(documented_command_kind(
            'python evil.py mr_identity.py capture', r"C:\flow\mr_identity.py"))

    def test_identity_same_basename_at_attacker_path_cannot_forge_binding(self):
        command = r'python C:\attacker\mr_identity.py capture --url https://x'
        self.assertIsNone(documented_command_kind(
            command, r"C:\flow\mr_identity.py"))


class UnwrapDriverStageTests(unittest.TestCase):
    def _args(self):
        return SimpleNamespace(task_commit=TASK)

    def test_ok_outside_readback_refused(self):
        doc = driver(outcome="ok", stage="push")
        with self.assertRaises(checks.Stop) as ctx:
            checks.unwrap_driver(doc, self._args(), queue={})
        self.assertEqual(ctx.exception.reason, "publication-readback-malformed")

    def test_unknown_maps_to_exit_3(self):
        doc = driver(outcome="unknown", stage="mr")
        doc["error"] = {"reason": "mr-write-unknown"}
        with self.assertRaises(checks.Stop) as ctx:
            checks.unwrap_driver(doc, self._args(), queue={})
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(ctx.exception.verdict, "unknown")

    def test_observed_reconcile_still_accepts_identity(self):
        doc = driver(outcome="observed", stage="reconcile")
        identity, _ = checks.unwrap_driver(doc, self._args(), queue={})
        self.assertEqual(identity["head_sha"], TASK)


class PublishDriverGateTests(unittest.TestCase):
    """Exercise the receipt gate through ``lenovo_publish.main`` itself."""

    def _run(self, publish_effect):
        request = {
            "token": "fixture-token", "authorized": ["push", "mr"],
            "reconcileOnly": False, "lenovoCheckout": ".",
            "lenovoWorktree": ".", "expectedCommit": TASK,
        }
        args = SimpleNamespace(request="request.json", request_sha256="a" * 64,
                               reconcile=False, flow_dir=Path("."))
        stdout, stderr = io.StringIO(), io.StringIO()
        publish_patch = (mock.patch.object(lenovo_publish, "publish",
                                           side_effect=publish_effect)
                         if isinstance(publish_effect, BaseException)
                         else mock.patch.object(lenovo_publish, "publish",
                                                return_value=publish_effect))
        patches = (
            mock.patch.object(lenovo_publish, "parse", return_value=args),
            mock.patch.object(lenovo_publish, "validated", return_value=request),
            mock.patch.object(lenovo_publish, "load_description",
                              return_value="fixture description\n"),
            mock.patch.object(lenovo_publish, "verify_bundle", return_value={}),
            mock.patch.object(lenovo_publish, "bundle_tip", return_value=TASK),
            mock.patch.object(lenovo_publish, "import_once", return_value=Path(".")),
            mock.patch.object(lenovo_publish, "preflight"),
            mock.patch.object(lenovo_publish, "push"),
            publish_patch,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = lenovo_publish.main([])
        return code, json.loads(stdout.getvalue())

    def test_matching_readback_is_the_only_success(self):
        code, result = self._run({"head_sha": TASK})
        self.assertEqual(code, 0)
        self.assertEqual((result["outcome"], result["stage"]),
                         ("ok", "readback"))

    def test_mismatched_readback_is_not_success(self):
        code, result = self._run({"head_sha": OTHER})
        self.assertNotEqual(code, 0)
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["error"]["reason"],
                         "publication-head-mismatch")

    def test_unavailable_readback_stays_unknown_exit_three(self):
        unavailable = lenovo_publish.Stop("mr-readback-unavailable",
                                          verdict="unknown", code=3)
        code, result = self._run(unavailable)
        self.assertEqual(code, 3)
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["error"]["reason"],
                         "mr-readback-unavailable")


if __name__ == "__main__":
    unittest.main()
