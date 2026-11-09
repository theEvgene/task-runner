"""Staging script: bundle create plus verify, hashed manifest, atomic write.

Exercises the real ``scripts/dell_lan/stage_bundle.py`` interface the Lenovo
driver verifies: a sound bundle stages with a digest-covered manifest, and an
existing bundle, manifest, or receipt is never replaced. Custody cases prove
the receipt stays outside the worktree, the shared folder, and any checkout;
race cases prove a concurrent name winner wins instead of being overwritten
and a handled failure leaves no staged orphan.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "dell_lan"
STAGE = SCRIPTS / "stage_bundle.py"

sys.path.insert(0, str(SCRIPTS))
import stage_bundle  # noqa: E402  (module under test for race plants)


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class StageBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-stage-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "work"
        self.shared = self.root / "share"
        self.shared.mkdir()
        git("init", "-q", "-b", "main", str(self.repo), cwd=str(self.root))
        git("config", "user.email", "stage-fixture@example.invalid", cwd=self.repo)
        git("config", "user.name", "stage-fixture", cwd=self.repo)
        git("config", "core.autocrlf", "false", cwd=self.repo)
        (self.repo / "work.txt").write_text("staged\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.repo)
        git("commit", "-qm", "staged work", cwd=self.repo)
        self.tip = git("rev-parse", "--verify", "HEAD", cwd=self.repo)
        self.receipt = self.root / "receipt.json"

    def stage(self, *extra: str, bundle_name: str = "transfer.bundle",
              manifest_name: str = "transfer.manifest.json",
              receipt: Path | None = None):
        return subprocess.run(
            [sys.executable, str(STAGE), "--worktree", str(self.repo),
             "--shared", str(self.shared), "--bundle-name", bundle_name,
             "--manifest-name", manifest_name, *extra,
             "--receipt", str(receipt or self.receipt)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False)

    def args(self, receipt: Path | None = None, bundle_name: str = "transfer.bundle",
             manifest_name: str = "transfer.manifest.json"):
        return stage_bundle.parse([
            "--worktree", str(self.repo), "--shared", str(self.shared),
            "--bundle-name", bundle_name, "--manifest-name", manifest_name,
            "--receipt", str(receipt or self.receipt),
        ])

    def shared_names(self) -> list[str]:
        return sorted(path.name for path in self.shared.iterdir())

    def test_sound_bundle_stages_with_digest_covered_manifest(self):
        proc = self.stage("--expected-tip", self.tip)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(data["outcome"], "staged")
        self.assertEqual(data["tip"], self.tip)
        bundle = self.shared / "transfer.bundle"
        manifest = json.loads((self.shared / "transfer.manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "git-bundle-manifest/1")
        self.assertEqual(manifest["bundle"], "transfer.bundle")
        self.assertEqual(len(manifest["files"]), 1)
        entry = manifest["files"][0]
        raw = bundle.read_bytes()
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(entry["bytes"], len(raw))
        digest = hashlib.sha256(json.dumps(
            manifest["files"], ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(manifest["manifestDigest"], digest)
        self.assertEqual(data["manifestDigest"], digest)
        verify = subprocess.run(
            ["git", "bundle", "verify", str(bundle)], cwd=str(self.repo),
            capture_output=True, text=True, check=False)
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_existing_bundle_is_never_replaced(self):
        before = self.shared / "transfer.bundle"
        before.write_bytes(b"pinned")
        proc = self.stage()
        self.assertNotEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["reason"], "artifact-exists")
        self.assertEqual(before.read_bytes(), b"pinned")
        self.assertEqual(self.shared_names(), ["transfer.bundle"])

    def test_existing_manifest_is_never_replaced(self):
        pinned = self.shared / "transfer.manifest.json"
        pinned.write_text("pinned\n", encoding="utf-8")
        proc = self.stage()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reason"], "artifact-exists")
        self.assertEqual(pinned.read_text(encoding="utf-8"), "pinned\n")
        self.assertEqual(self.shared_names(), ["transfer.manifest.json"])

    def test_existing_receipt_is_never_replaced(self):
        self.receipt.write_text("pinned\n", encoding="utf-8")
        proc = self.stage()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reason"], "receipt-exists")
        self.assertEqual(self.receipt.read_text(encoding="utf-8"), "pinned\n")
        self.assertEqual(self.shared_names(), [])

    def test_manifest_collision_race_removes_the_bundle_this_run_published(self):
        args = self.args()
        real = stage_bundle._commit_new

        def racing(temp, final, reason):
            if final.name == "transfer.bundle":
                (self.shared / "transfer.manifest.json").write_text(
                    "pinned\n", encoding="utf-8")
            return real(temp, final, reason)

        with mock.patch.object(stage_bundle, "_commit_new", racing):
            with self.assertRaises(stage_bundle.Stop) as caught:
                stage_bundle.stage(args)
        self.assertEqual(caught.exception.reason, "artifact-exists")
        self.assertFalse((self.shared / "transfer.bundle").exists(),
                         "a bundle this run created must not outlive its refusal")
        self.assertEqual((self.shared / "transfer.manifest.json")
                         .read_text(encoding="utf-8"), "pinned\n")
        self.assertEqual(self.shared_names(), ["transfer.manifest.json"])

    def test_receipt_collision_race_removes_both_staged_artifacts(self):
        args = self.args()
        real = stage_bundle._commit_new

        def racing(temp, final, reason):
            if final == self.receipt:
                final.write_text("pinned\n", encoding="utf-8")
            return real(temp, final, reason)

        with mock.patch.object(stage_bundle, "_commit_new", racing):
            with self.assertRaises(stage_bundle.Stop) as caught:
                stage_bundle.stage(args)
        self.assertEqual(caught.exception.reason, "receipt-exists")
        self.assertEqual(self.receipt.read_text(encoding="utf-8"), "pinned\n")
        self.assertEqual(self.shared_names(), [],
                         "bundle and manifest must be removed when the receipt is refused")

    def test_tip_mismatch_leaves_no_partial_artifact_or_temporary_file(self):
        proc = self.stage("--expected-tip", "0" * 40)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reason"], "bundle-tip-mismatch")
        self.assertEqual(self.shared_names(), [],
                         "the failed attempt must remove its bundle and temporaries")

    def test_receipt_inside_the_shared_folder_is_refused(self):
        receipt = self.shared / "receipt.json"
        proc = self.stage(receipt=receipt)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reason"], "receipt-refused")
        self.assertEqual(self.shared_names(), [])

    def test_receipt_inside_the_worktree_is_refused(self):
        receipt = self.repo / "receipt.json"
        proc = self.stage(receipt=receipt)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reason"], "receipt-refused")
        self.assertFalse(receipt.exists())
        self.assertEqual(self.shared_names(), [])

    def test_receipt_inside_another_checkout_is_refused(self):
        checkout = self.root / "other-checkout"
        (checkout / ".git").mkdir(parents=True)
        proc = self.stage(receipt=checkout / "receipt.json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reason"], "receipt-refused")
        self.assertEqual(self.shared_names(), [])

    def test_relative_receipt_is_refused(self):
        proc = self.stage(receipt=Path("receipt.json"))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["reason"], "usage")

    def test_colliding_bundle_and_manifest_names_are_refused(self):
        proc = self.stage(bundle_name="transfer.bundle",
                          manifest_name="transfer.bundle")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["reason"], "usage")
        self.assertEqual(self.shared_names(), [])

    def test_bundle_and_manifest_names_have_no_fixed_defaults(self):
        proc = subprocess.run(
            [sys.executable, str(STAGE), "--worktree", str(self.repo),
             "--shared", str(self.shared), "--receipt", str(self.receipt)],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_missing_worktree_is_usage(self):
        proc = subprocess.run(
            [sys.executable, str(STAGE), "--worktree", str(self.root / "absent"),
             "--shared", str(self.shared), "--bundle-name", "transfer.bundle",
             "--manifest-name", "transfer.manifest.json",
             "--receipt", str(self.receipt)],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_raw_sha_ref_is_refused(self):
        proc = self.stage("--ref", self.tip)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
