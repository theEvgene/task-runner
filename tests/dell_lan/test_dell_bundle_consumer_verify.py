"""Consumer-side bundle verification before any mutation (issue #54, AC2).

The Lenovo intake (`lenovo_bundle.verify_bundle` + `import_once`) re-checks
the staged bundle SHA, the manifest digest/entries, and the bundle tip
against the signed `--expected-commit` before it fetches, writes a ref, or
touches a worktree. These fixtures prove each corruption class is refused
with no checkout mutation:

- post-stage byte corruption of the bundle → ``artifact-mismatch``;
- incomplete manifest (no entries, or an entry for a missing file) → refused;
- tampered file entry that no longer matches the manifest digest → refused;
- bundle tip that is not the signed expected commit → refused before any
  ref or worktree is created.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "dell_lan"

sys.path.insert(0, str(SCRIPTS))

import lenovo_bundle  # noqa: E402  (modules under test)
import stage_bundle  # noqa: E402  (staging helper)
from dell_lan_common import Stop, manifest_digest  # noqa: E402


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_ok(*args: str, cwd: Path) -> bool:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=False)
    return proc.returncode == 0


class ConsumerBundleVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-consumer-verify-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "work"
        self.shared = self.root / "share"
        self.shared.mkdir()
        git("init", "-q", "-b", "main", str(self.repo), cwd=str(self.root))
        git("config", "user.email", "consumer-fixture@example.invalid",
            cwd=self.repo)
        git("config", "user.name", "consumer-fixture", cwd=self.repo)
        git("config", "core.autocrlf", "false", cwd=self.repo)
        (self.repo / "work.txt").write_text("consumer\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.repo)
        git("commit", "-qm", "consumer work", cwd=self.repo)
        self.tip = git("rev-parse", "--verify", "HEAD", cwd=self.repo)
        receipt = self.root / "receipt.json"
        staged = stage_bundle.stage(stage_bundle.parse([
            "--worktree", str(self.repo), "--shared", str(self.shared),
            "--bundle-name", "transfer.bundle",
            "--manifest-name", "transfer.manifest.json",
            "--expected-tip", self.tip,
            "--receipt", str(receipt),
        ]))
        self.assertEqual(staged["outcome"], "staged")
        self.bundle = self.shared / "transfer.bundle"
        self.manifest_path = self.shared / "transfer.manifest.json"

    def request_for(self) -> tuple[dict, dict]:
        return ({"bundle": str(self.bundle),
                 "manifest": str(self.manifest_path)}, {})

    def test_sound_bundle_verifies_with_bound_sha_and_digest(self):
        request, result = self.request_for()
        verified = lenovo_bundle.verify_bundle(request, result)
        self.assertEqual(verified, self.bundle)
        bound = result["bundle"]
        self.assertTrue(bound["verified"])
        self.assertEqual(bound["bytes"], self.bundle.stat().st_size)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(bound["manifestDigest"],
                         manifest["manifestDigest"])
        self.assertEqual(bound["manifestDigest"],
                         manifest_digest(manifest["files"]))

    def test_post_stage_corruption_is_mismatch_not_import(self):
        raw = self.bundle.read_bytes()
        corrupted = bytearray(raw)
        corrupted[len(corrupted) // 2] ^= 0xFF
        self.bundle.write_bytes(bytes(corrupted))
        request, result = self.request_for()
        with self.assertRaises(Stop) as caught:
            lenovo_bundle.verify_bundle(request, result)
        self.assertEqual(caught.exception.reason, "artifact-mismatch")
        self.assertNotIn("bundle", result)

    def test_incomplete_manifest_with_no_entries_is_refused(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["files"] = []
        manifest["manifestDigest"] = manifest_digest([])
        self.manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":")) + "\n",
            encoding="utf-8")
        request, result = self.request_for()
        with self.assertRaises(Stop) as caught:
            lenovo_bundle.verify_bundle(request, result)
        self.assertEqual(caught.exception.reason, "manifest-mismatch")
        self.assertNotIn("bundle", result)

    def test_incomplete_artifact_for_a_manifest_entry_is_missing(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        ghost = {"path": "ghost.bundle", "sha256": "0" * 64, "bytes": 1}
        manifest["files"] = [*manifest["files"], ghost]
        manifest["manifestDigest"] = manifest_digest(manifest["files"])
        self.manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":")) + "\n",
            encoding="utf-8")
        request, result = self.request_for()
        with self.assertRaises(Stop) as caught:
            lenovo_bundle.verify_bundle(request, result)
        self.assertEqual(caught.exception.reason, "artifact-missing")
        self.assertNotIn("bundle", result)

    def test_tampered_entry_that_breaks_the_digest_is_refused(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["bytes"] += 1
        # The stored digest still covers the original entries.
        self.manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":")) + "\n",
            encoding="utf-8")
        request, result = self.request_for()
        with self.assertRaises(Stop) as caught:
            lenovo_bundle.verify_bundle(request, result)
        self.assertEqual(caught.exception.reason, "manifest-mismatch")
        self.assertNotIn("bundle", result)

    def test_commit_mismatch_is_refused_before_any_ref_or_worktree(self):
        checkout = self.root / "lenovo-checkout"
        git("init", "-q", "-b", "main", str(checkout), cwd=str(self.root))
        git("config", "user.email", "lenovo-fixture@example.invalid",
            cwd=checkout)
        git("config", "user.name", "lenovo-fixture", cwd=checkout)
        git("config", "core.autocrlf", "false", cwd=checkout)
        (checkout / "base.txt").write_text("base\n", encoding="utf-8")
        git("add", "base.txt", cwd=checkout)
        git("commit", "-qm", "base", cwd=checkout)
        base = git("rev-parse", "--verify", "HEAD", cwd=checkout)
        worktree = self.root / "lenovo-task-worktree"
        request = {
            "token": "0" * 16,
            "authorized": ["import"],
            "lenovoCheckout": str(checkout),
            "lenovoWorktree": str(worktree),
            "branch": "task/consumer-tip",
            "expectedCommit": "a" * 40,
            "expectedBaseSha": base,
            "expectedPreviousCommit": None,
            "reconcileOnly": False,
        }
        result: dict = {}
        with self.assertRaises(Stop) as caught:
            lenovo_bundle.import_once(request, self.bundle, "b" * 40,
                                      result)
        self.assertEqual(caught.exception.reason, "artifact-mismatch")
        self.assertFalse(git_ok("rev-parse", "--verify",
                                "refs/heads/task/consumer-tip",
                                cwd=checkout),
                         "a refused tip must not create the branch ref")
        self.assertFalse(worktree.exists(),
                          "a refused tip must not create the worktree")
        self.assertNotIn("import", result)

    def test_no_toolkit_fallback_in_default_resolution(self):
        source = (SCRIPTS / "dell_lan_common.py").read_text(encoding="utf-8")
        for name in ("default_bridge_dir", "default_flow_dir"):
            start = source.index(f"def {name}(")
            try:
                end = source.index("\ndef ", start + 1)
            except ValueError:
                end = len(source)
            body = source[start:end]
            self.assertNotIn("personal-agent-toolkit", body,
                             f"{name} must not fall back into another checkout")
            self.assertNotIn("t-bank", body,
                             f"{name} must not fall back into another checkout")


if __name__ == "__main__":
    unittest.main()
