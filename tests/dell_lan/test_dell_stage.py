"""Staging script: bundle create plus verify, hashed manifest, atomic write.

Exercises the real ``scripts/dell_lan/stage_bundle.py`` interface the Lenovo
driver verifies: a sound bundle stages with a digest-covered manifest, and an
existing bundle, a missing worktree, or a tip mismatch refuses without
overwriting anything.
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

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "dell_lan"
STAGE = SCRIPTS / "stage_bundle.py"


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

    def stage(self, *extra: str):
        return subprocess.run(
            [sys.executable, str(STAGE), "--worktree", str(self.repo),
             "--shared", str(self.shared), "--bundle-name", "transfer.bundle",
             "--manifest-name", "transfer.manifest.json", *extra,
             "--receipt", str(self.receipt)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False)

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

    def test_tip_mismatch_refuses(self):
        proc = self.stage("--expected-tip", "0" * 40)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["reason"], "bundle-tip-mismatch")

    def test_missing_worktree_is_usage(self):
        proc = subprocess.run(
            [sys.executable, str(STAGE), "--worktree", str(self.root / "absent"),
             "--shared", str(self.shared), "--receipt", str(self.receipt)],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_raw_sha_ref_is_refused(self):
        proc = self.stage("--ref", self.tip)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
