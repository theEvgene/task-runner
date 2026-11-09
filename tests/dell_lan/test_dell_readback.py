"""Local readback checks: anchoring, worktree, bundle, usage guards.

These cases need no queue key and no Lenovo host, but they start from the
imported base copy so the anchoring check is the real one. They prove that the
readback refuses an unanchored copy, a foreign worktree, a drifted or escaping
bundle, and an unusable receipt path, and that a clean local state is reported
as transfer-ready only.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from dell_fixture import (
    READBACK, SHA_B, SHA_D, Workspace, command_file, run, sha256_bytes,
)


class LocalReadbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = Workspace().build().build_ready()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def test_ready_is_transfer_ready_only(self):
        proc, data = self.ws.readback()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["schema"], "dell-lan-readback/2")
        self.assertEqual(data["verdict"], "ready")
        self.assertEqual(data["reason"], "ready")
        self.assertEqual(data["provenance"], "fixture")
        self.assertTrue(data["baseMatch"])
        self.assertEqual(data["actualHead"], self.ws.base)
        self.assertTrue(data["worktree"]["registered"])
        self.assertTrue(data["worktree"]["headMatchesTaskCommit"])
        self.assertTrue(data["bundle"]["match"])
        self.assertTrue(data["bundle"]["tipBound"])
        self.assertFalse(data["queue"]["provided"])
        self.assertFalse(data["publication"]["bound"])
        self.assertIsNone(data["publication"]["headSha"])
        self.assertEqual(data["freshness"]["stale"], False)
        self.assertIn("mr_identity.py capture", data["evidence"]["gitlab"])
        stdout_lines = [line for line in proc.stdout.strip().splitlines() if line]
        self.assertEqual(len(stdout_lines), 1, proc.stdout)
        self.assertEqual(json.loads(stdout_lines[0])["verdict"], "ready")
        self.assertIn("dell readback ready", proc.stderr)

    def test_base_mismatch_fails_closed(self):
        proc, data = self.ws.readback("--expected-base", SHA_B)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["verdict"], "mismatch")
        self.assertEqual(data["reason"], "base-mismatch")
        self.assertFalse(data["publication"]["bound"])

    def test_unregistered_worktree_fails_closed(self):
        proc, data = self.ws.readback("--worktree", str(self.ws.root / "other-worktree"))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "worktree-unregistered")

    def test_worktree_at_another_commit_fails_closed(self):
        proc, data = self.ws.readback("--task-commit", SHA_D)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "worktree-head-mismatch")

    def test_bundle_hash_drift_fails_closed(self):
        staged = Path(self.ws.shared) / "transfer.bundle"
        original = staged.read_bytes()
        try:
            staged.write_bytes(original + b"tampered")
            proc, data = self.ws.readback()
        finally:
            staged.write_bytes(original)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "bundle-mismatch")

    def test_bundle_path_escape_refused(self):
        escaping = Path(self.ws.root) / "escaping.manifest.json"
        manifest = json.loads((Path(self.ws.shared) / "transfer.manifest.json").read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../escape.bundle"
        manifest["manifestDigest"] = sha256_bytes(json.dumps(
            manifest["files"], separators=(",", ":")).encode("utf-8"))
        try:
            escaping.write_text(json.dumps(manifest), encoding="utf-8")
            proc, data = self.ws.readback("--bundle-manifest", str(escaping))
        finally:
            escaping.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "bundle-path-refused")

    def test_missing_bundle_stops_before_claiming(self):
        staged = Path(self.ws.shared) / "transfer.bundle"
        original = staged.read_bytes()
        try:
            staged.unlink()
            proc, data = self.ws.readback()
        finally:
            staged.write_bytes(original)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "bundle-missing")

    def test_stale_bundle_is_flagged_not_hidden(self):
        manifest = Path(self.ws.shared) / "transfer.manifest.json"
        old = time.time() - 30 * 3600
        try:
            os.utime(manifest, (old, old))
            proc, data = self.ws.readback()
        finally:
            os.utime(manifest, None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "stale")
        self.assertTrue(data["freshness"]["stale"])
        self.assertIn("re-sync", data["nextAction"])

    def test_bundle_tip_not_the_task_commit_fails_closed(self):
        from dell_fixture import git

        git("commit", "--allow-empty", "-qm", "later work", cwd=self.ws.worktree)
        tip = git("rev-parse", "--verify", "HEAD", cwd=self.ws.worktree)
        try:
            proc, data = self.ws.readback("--task-commit", tip)
        finally:
            git("reset", "--hard", self.ws.task, cwd=self.ws.worktree)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "bundle-tip-mismatch")

    def test_usage_error_writes_no_receipt(self):
        receipt = Path(self.ws.root) / "usage-receipt.json"
        proc, _ = self.ws.readback("--expected-base", "not-a-sha", receipt=receipt)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("dell readback usage", proc.stderr)
        self.assertFalse(receipt.exists())

    def test_receipt_inside_checkout_refused(self):
        receipt = Path(self.ws.dell) / "readback.json"
        proc, _ = self.ws.readback(receipt=receipt)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(receipt.exists())

    def test_manifest_digest_must_cover_the_entries(self):
        manifest_path = Path(self.ws.shared) / "transfer.manifest.json"
        original = manifest_path.read_bytes()
        try:
            manifest = json.loads(original)
            manifest["files"][0]["path"] = "phase.bundle"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proc, data = self.ws.readback()
        finally:
            manifest_path.write_bytes(original)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "bundle-manifest-malformed")
        self.assertIn("manifestDigest", data["detail"])

    def test_key_inside_the_shared_folder_is_refused(self):
        inside = Path(self.ws.shared) / "key.bin"
        inside.write_bytes(b"0" * 32)
        try:
            proc, data = self.ws.readback(
                *self.ws.queue_args(self.ws.new_job_id()),
                "--key-file", str(inside),
            )
        finally:
            inside.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("shared", data["detail"])

    def test_a_key_outside_every_protected_root_is_accepted(self):
        job_id = self.ws.submit(command_file(self.ws))
        self.ws.seal_result(job_id, json.dumps(self.ws.identity()))
        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(data["queue"]["jobVerified"])
        self.assertTrue(data["queue"]["resultVerified"])

    def test_key_inside_repository_refused(self):
        inside = Path(self.ws.dell) / "key.bin"
        inside.write_bytes(b"0" * 32)
        try:
            proc, data = self.ws.readback(
                "--queue", str(self.ws.queue), "--key-file", str(inside),
                "--job-id", self.ws.new_job_id(),
            )
        finally:
            inside.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")

    def test_a_queue_readback_without_the_shared_root_is_refused_at_usage(self):
        """The shared folder is a protected root even when no bundle is checked."""

        receipt = Path(self.ws.root) / "no-shared.json"
        proc = run(self.ws.readback_command(
            "--queue", str(self.ws.queue), "--key-file", str(self.ws.key),
            "--job-id", self.ws.new_job_id(), receipt=receipt), env=self.ws.env())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--shared", proc.stderr)
        self.assertFalse(receipt.exists())

    def test_a_shared_key_is_refused_when_the_bundle_arguments_are_omitted(self):
        """Dropping --bundle-dir and --bundle-manifest cannot drop the share check."""

        inside = Path(self.ws.shared) / "unchecked-key.bin"
        inside.write_bytes(self.ws.key_bytes)
        receipt = Path(self.ws.root) / "shared-queue-key.json"
        try:
            proc = run(self.ws.readback_command(
                "--shared", str(self.ws.shared),
                "--queue", str(self.ws.queue), "--key-file", str(inside),
                "--job-id", self.ws.new_job_id(), receipt=receipt), env=self.ws.env())
        finally:
            inside.unlink(missing_ok=True)
        data = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("shared", data["detail"])
        self.assertFalse(data["queue"]["jobVerified"], "no queue read may start on a refused key")

    def test_a_key_outside_every_root_is_accepted_without_bundle_arguments(self):
        job_id = self.ws.submit(command_file(self.ws))
        self.ws.seal_result(job_id, json.dumps(self.ws.identity()))
        receipt = Path(self.ws.root) / "shared-root-only.json"
        proc = run(self.ws.readback_command(
            "--shared", str(self.ws.shared),
            "--queue", str(self.ws.queue), "--key-file", str(self.ws.key),
            "--job-id", job_id, receipt=receipt), env=self.ws.env())
        data = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(data["queue"]["jobVerified"])
        self.assertTrue(data["publication"]["bound"])
        self.assertFalse(data["bundle"]["provided"])


if __name__ == "__main__":
    unittest.main()
