"""Fail-closed readback contracts for standalone transfer and publication."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan"
sys.path.insert(0, str(SCRIPTS))

from dell_readback_checks import Stop, check_result, documented_command
from dell_standalone_common import (
    PUBLISH_EXISTING_RESULT_SCHEMA, TRANSFER_RESULT_SCHEMA,
)
from test_dell_failclosed import FakeBridge, MiniRepo, payload, unknown_args

TRANSFER_SCRIPT = r"C:\skill\scripts\dell_lan\lenovo_transfer.py"
PUBLISH_EXISTING_SCRIPT = r"C:\skill\scripts\dell_lan\lenovo_publish_existing.py"
TRANSFER_COMMAND = (f'"python" "{TRANSFER_SCRIPT}" '
                    '--request "x" --request-sha256 "y"')
PUBLISH_EXISTING_COMMAND = (f'"python" "{PUBLISH_EXISTING_SCRIPT}" '
                            '--request "x" --request-sha256 "y"')
DRIVER_SCRIPT = r"C:\skill\scripts\dell_lan\lenovo_publish.py"
IDENTITY_SCRIPT = r"C:\flow\gitlab-mr-flow\scripts\mr_identity.py"
DRIVER_COMMAND = (f'"python" "{DRIVER_SCRIPT}" '
                  '--request "x" --request-sha256 "y"')
IDENTITY_COMMAND = (f'"python" "{IDENTITY_SCRIPT}" capture '
                    '--url "https://gitlab.example/mr/1"')


def identity_receipt() -> dict:
    return {
        "schema_version": 1, "gitlab_host": "gitlab.example",
        "project_path": "group/project", "source_branch": "task/test",
        "target_branch": "main", "mr_url": "https://gitlab.example/mr/1",
        "iid": 1, "state": "opened", "head_sha": "a" * 40,
        "target_sha": "c" * 40,
        "captured_at": "2026-11-14T00:00:00+00:00",
    }


class ReadbackCommandSchemaBindingTests(unittest.TestCase):
    def run_check(self, command: str, document: dict,
                  receipt_script_path: str | None = None):
        root = Path(tempfile.mkdtemp(prefix="dell-schema-binding-"))
        (root / "key.bin").write_bytes(b"k" * 32)
        bridge = FakeBridge(
            b"k" * 32, command,
            payload("ok", 0, stdout=json.dumps(document)),
            receipt_script_path or (IDENTITY_SCRIPT if "mr_identity" in command
                                    else DRIVER_SCRIPT))
        return check_result(unknown_args(root), bridge)

    def test_publish_driver_cannot_bypass_gate_with_raw_identity_stdout(self):
        with self.assertRaises(Stop) as caught:
            self.run_check(DRIVER_COMMAND, identity_receipt())
        self.assertEqual(caught.exception.reason,
                         "publication-readback-malformed")
        self.assertIn("structured result envelope", caught.exception.detail)

    def test_direct_identity_command_still_accepts_raw_identity_stdout(self):
        result = self.run_check(IDENTITY_COMMAND, identity_receipt())
        self.assertTrue(result["publication"]["bound"])
        self.assertIsNone(result["publication"]["driver"])

    def test_publish_driver_accepts_gated_structured_stdout(self):
        identity = identity_receipt()
        description_sha = "d" * 64
        document = {
            "schema": "dell-lan-publish-result/1", "outcome": "ok",
            "stage": "readback", "identity": identity,
            "push": {"observedRemoteSha": "a" * 40},
            "mr": {"url": identity["mr_url"], "iid": identity["iid"],
                   "descriptionVerified": True,
                   "descriptionExpectedSha256": description_sha,
                   "descriptionObservedSha256": description_sha},
        }
        result = self.run_check(DRIVER_COMMAND, document)
        self.assertTrue(result["publication"]["bound"])
        self.assertEqual(result["publication"]["driverStage"], "readback")

    def test_attacker_driver_basename_is_not_authenticated_evidence(self):
        command = r"python C:\attacker\lenovo_publish.py --request x"
        with self.assertRaises(Stop) as caught:
            self.run_check(command, identity_receipt(), DRIVER_SCRIPT)
        self.assertEqual(caught.exception.reason, "queue-job-mismatch")

    def test_attacker_identity_basename_is_not_authenticated_evidence(self):
        command = r"python C:\attacker\mr_identity.py capture --url https://x"
        with self.assertRaises(Stop) as caught:
            self.run_check(command, identity_receipt(), IDENTITY_SCRIPT)
        self.assertEqual(caught.exception.reason, "queue-job-mismatch")


class StandaloneReadbackTests(unittest.TestCase):
    def test_authenticates_both_standalone_commands(self):
        self.assertTrue(documented_command(TRANSFER_COMMAND, TRANSFER_SCRIPT))
        self.assertTrue(documented_command(
            PUBLISH_EXISTING_COMMAND, PUBLISH_EXISTING_SCRIPT))

    def test_unknown_results_keep_the_same_job_slot(self):
        for schema, command, script in (
            (TRANSFER_RESULT_SCHEMA, TRANSFER_COMMAND, TRANSFER_SCRIPT),
            (PUBLISH_EXISTING_RESULT_SCHEMA,
             PUBLISH_EXISTING_COMMAND, PUBLISH_EXISTING_SCRIPT),
        ):
            with self.subTest(schema=schema):
                root = Path(tempfile.mkdtemp(prefix="dell-standalone-unknown-"))
                (root / "key.bin").write_bytes(b"k" * 32)
                document = {"schema": schema, "outcome": "unknown",
                            "stage": "reconcile",
                            "error": {"reason": "state-unreadable"}}
                bridge = FakeBridge(
                    b"k" * 32, command,
                    payload("error", 1, stdout=json.dumps(document)), script)
                with self.assertRaises(Stop) as caught:
                    check_result(unknown_args(root), bridge)
                self.assertEqual(caught.exception.reason, "outcome-unknown")
                self.assertEqual(caught.exception.verdict, "unknown")
                self.assertEqual(caught.exception.code, 3)

    def test_transfer_result_binds_sha_branch_and_clean_worktree(self):
        root = Path(tempfile.mkdtemp(prefix="dell-transfer-readback-"))
        (root / "key.bin").write_bytes(b"k" * 32)
        args = unknown_args(root)
        args.source_branch = MiniRepo.BRANCH
        args.lenovo_worktree = str(root / "task-worktree")
        document = {
            "schema": TRANSFER_RESULT_SCHEMA, "outcome": "ok", "stage": "import",
            "authorized": ["import"], "push": None, "mr": None,
            "import": {"branch": MiniRepo.BRANCH, "commit": args.task_commit,
                       "worktree": str(root / "task-worktree"), "matches": True,
                       "clean": True},
        }
        bridge = FakeBridge(
            b"k" * 32, TRANSFER_COMMAND,
            payload("ok", 0, stdout=json.dumps(document)), TRANSFER_SCRIPT)

        observed = check_result(args, bridge)

        self.assertTrue(observed["transfer"]["bound"])
        self.assertEqual(observed["transfer"]["commit"], args.task_commit)
        self.assertEqual(observed["transfer"]["branch"], MiniRepo.BRANCH)
        self.assertTrue(observed["transfer"]["clean"])
        self.assertFalse(observed["publication"]["bound"])

    def test_transfer_result_refuses_a_different_lenovo_worktree(self):
        root = Path(tempfile.mkdtemp(prefix="dell-transfer-readback-"))
        (root / "key.bin").write_bytes(b"k" * 32)
        args = unknown_args(root)
        args.source_branch = MiniRepo.BRANCH
        args.lenovo_worktree = str(root / "expected-lenovo-worktree")
        document = {
            "schema": TRANSFER_RESULT_SCHEMA, "outcome": "ok", "stage": "import",
            "authorized": ["import"], "push": None, "mr": None,
            "import": {"branch": MiniRepo.BRANCH, "commit": args.task_commit,
                       "worktree": str(root / "other-lenovo-worktree"),
                       "matches": True, "clean": True},
        }
        bridge = FakeBridge(
            b"k" * 32, TRANSFER_COMMAND,
            payload("ok", 0, stdout=json.dumps(document)), TRANSFER_SCRIPT)

        with self.assertRaises(Stop) as caught:
            check_result(args, bridge)

        self.assertEqual(caught.exception.reason, "transfer-worktree-mismatch")

    def test_publish_existing_result_uses_publication_consumer(self):
        root = Path(tempfile.mkdtemp(prefix="dell-existing-readback-"))
        (root / "key.bin").write_bytes(b"k" * 32)
        args = unknown_args(root)
        args.project = "group/project"
        args.source_branch = MiniRepo.BRANCH
        args.target_branch = "main"
        identity = {
            "schema_version": 1, "gitlab_host": "gitlab.example.invalid",
            "project_path": args.project, "source_branch": args.source_branch,
            "target_branch": args.target_branch,
            "mr_url": "https://example.invalid/mr/1", "state": "opened",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "head_sha": args.task_commit, "target_sha": "c" * 40, "iid": 1,
        }
        document = {
            "schema": PUBLISH_EXISTING_RESULT_SCHEMA, "outcome": "ok",
            "stage": "readback", "authorized": ["push", "mr"],
            "push": {"observedRemoteSha": args.task_commit},
            "mr": {"url": identity["mr_url"], "iid": 1, "created": False},
            "identity": identity,
        }
        bridge = FakeBridge(
            b"k" * 32, PUBLISH_EXISTING_COMMAND,
            payload("ok", 0, stdout=json.dumps(document)),
            PUBLISH_EXISTING_SCRIPT)

        observed = check_result(args, bridge)

        self.assertTrue(observed["publication"]["bound"])
        self.assertEqual(
            observed["publication"]["driver"],
            "scripts/dell_lan/lenovo_publish_existing.py",
        )


if __name__ == "__main__":
    unittest.main()
