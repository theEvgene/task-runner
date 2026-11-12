"""Contract tests for the standalone release skill rollout.

Fast, stdlib only, no queue, no network, no second host. Mirrors the
installer rules: frontmatter identity, safety strings, ownership
manifest, discovery uniqueness, and the per-file token budget.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "release"
SKILL_MD = SKILL_DIR / "SKILL.md"
INSTALL_PS1 = SKILL_DIR / "scripts" / "install.ps1"
VERIFY_PS1 = SKILL_DIR / "scripts" / "verify-install.ps1"
MANIFEST = SKILL_DIR / "migrations.json"
UPDATE_MD = SKILL_DIR / "UPDATE.md"

# 5,500 tokens at floor(bytes / 4), same budget as check-file-tokens.mjs.
MAX_BYTES = 5500 * 4


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SkillIdentityTests(unittest.TestCase):
    def test_skill_md_names_release(self) -> None:
        lines = read_text(SKILL_MD).splitlines()
        self.assertGreater(len(lines), 2)
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:end])
        self.assertIn("name: release", frontmatter)

    def test_skill_is_thin_pointer(self) -> None:
        body = read_text(SKILL_MD)
        self.assertIn("docs/release.md", body)
        self.assertIn("scripts/release/request.py", body)
        self.assertIn("UPDATE.md", body)

    def test_agent_entry_points_at_skill(self) -> None:
        agents = read_text(ROOT / "AGENTS.md")
        self.assertIn("skills/release/SKILL.md", agents)


class InstallerSafetyTests(unittest.TestCase):
    def test_installer_emits_versioned_receipt(self) -> None:
        self.assertIn("task-runner/skill-rollout@1", read_text(INSTALL_PS1))

    def test_installer_never_fetches(self) -> None:
        # No network: the revision must resolve locally.
        self.assertNotIn("'fetch'", read_text(INSTALL_PS1))

    def test_installer_never_overwrites_foreign_entries(self) -> None:
        body = read_text(INSTALL_PS1)
        self.assertIn("Left unchanged", body)
        self.assertIn("Foreign entries left unchanged", body)

    def test_old_link_migration_is_opt_in_and_content_proven(self) -> None:
        body = read_text(INSTALL_PS1)
        self.assertIn("AllowOldLinkMigration", body)
        self.assertIn("lenovo-release", body)
        self.assertIn("migrations.json", body)
        self.assertIn("frontmatterName", body)
        self.assertIn("bodyMustContain", body)

    def test_verifier_changes_nothing(self) -> None:
        body = read_text(VERIFY_PS1)
        self.assertIn("Changes nothing", body)
        for verb in ("New-Item", "::Delete(", "checkout', '--detach'",
                     "Remove-Item", "Set-Content", "WriteAllText"):
            self.assertNotIn(verb, body)


class MigrationManifestTests(unittest.TestCase):
    def test_manifest_schema_and_proof(self) -> None:
        manifest = json.loads(read_text(MANIFEST))
        self.assertEqual(manifest["schema"], "task-runner/skill-migrations@1")
        self.assertEqual(len(manifest["migrations"]), 1)
        migration = manifest["migrations"][0]
        self.assertEqual(migration["skill"], "release")
        self.assertEqual(migration["fromSkill"], "lenovo-release")
        self.assertEqual(migration["ownershipProof"]["frontmatterName"],
                         "lenovo-release")
        self.assertIn("scripts/release/request.py",
                      migration["ownershipProof"]["bodyMustContain"])


class MigrationProofTests(unittest.TestCase):
    # Reads the old pointer skill only when a personal-agent-toolkit
    # checkout happens to exist alongside; never modifies it. The proof
    # fields in migrations.json must match that file, otherwise the
    # opt-in migration could never fire (or could fire on a stranger).
    PAT_POINTERS = (
        Path("C:/Users/rb199787/Documents/repositories/personal-agent-toolkit")
        / "skills" / "t-bank" / "lenovo-release" / "SKILL.md",
    )

    def test_manifest_proof_matches_old_pointer_when_present(self) -> None:
        present = [p for p in self.PAT_POINTERS if p.is_file()]
        if not present:
            self.skipTest("no personal-agent-toolkit checkout alongside")
        proof = json.loads(read_text(MANIFEST))["migrations"][0]["ownershipProof"]
        for pointer in present:
            text = pointer.read_text(encoding="utf-8").replace("\r\n", "\n")
            lines = text.splitlines()
            end = lines.index("---", 1)
            frontmatter = "\n".join(lines[1:end])
            self.assertIn(f"name: {proof['frontmatterName']}", frontmatter)
            self.assertIn(proof["bodyMustContain"], text)


class RemoteNormalizationTests(unittest.TestCase):
    def test_installer_accepts_ssh_and_https_task_runner(self) -> None:
        body = read_text(INSTALL_PS1)
        self.assertIn("git@", body)
        self.assertIn("ssh://", body)
        self.assertIn("Get-GitHubSlug", body)
        self.assertIn("theEvgene/task-runner", body)

    def test_installer_slug_ignores_git_suffix_and_case(self) -> None:
        body = read_text(INSTALL_PS1)
        self.assertIn(".git", body)
        self.assertIn("ToLowerInvariant", body)


class CheckoutSafetyTests(unittest.TestCase):
    def test_conflicts_fail_before_checkout_advance(self) -> None:
        body = read_text(INSTALL_PS1)
        preflight = body.lower().index("before the checkout moves")
        advance = body.index("'--detach'")
        self.assertLess(preflight, advance)
        self.assertIn("preflightConflicts", body)

    def test_failed_advance_restores_recorded_head(self) -> None:
        body = read_text(INSTALL_PS1)
        self.assertIn("branchBefore", body)
        self.assertIn("symbolic-ref", body)
        self.assertIn("'restored'", body)
        self.assertIn("'restore-failed'", body)
        self.assertIn("rollback = $rollback", body)


class PatOwnershipTests(unittest.TestCase):
    def test_migration_requires_pat_remote_not_only_text(self) -> None:
        body = read_text(INSTALL_PS1)
        self.assertIn("ExpectedPatRemoteUrl", body)
        self.assertIn("personal-agent-toolkit", body)
        self.assertIn("Test-PatOwnership", body)
        self.assertIn("'rev-parse' '--show-toplevel'", body)
        self.assertIn("'remote' 'get-url' 'origin'", body)
        # Strangers stay for the PAT owner; content alone never deletes.
        self.assertIn("leftUnchanged", body)

    def test_skill_doc_states_remote_proof(self) -> None:
        self.assertIn("origin remote", read_text(SKILL_MD).lower())


class UpdateDocTests(unittest.TestCase):
    def test_update_shows_junction_lookup_fetch_pin_verify(self) -> None:
        body = read_text(UPDATE_MD)
        self.assertIn(".Target", body)
        self.assertIn("Split-Path", body)
        self.assertIn("git fetch origin", body)
        self.assertIn("install.ps1", body)
        self.assertIn("verify-install.ps1", body)
        self.assertIn("lenovo-release", body)


class VerifierDuplicateTests(unittest.TestCase):
    def test_verifier_refuses_duplicate_leaf(self) -> None:
        body = read_text(VERIFY_PS1)
        self.assertIn("duplicate", body.lower())
        self.assertIn("leaf name", body)


class DiscoveryTests(unittest.TestCase):
    def test_every_own_skill_is_discoverable_with_unique_leaf(self) -> None:
        skills_root = ROOT / "skills"
        leaves: dict[str, Path] = {}
        for skill_md in sorted(skills_root.rglob("SKILL.md")):
            try:
                relative = skill_md.parent.relative_to(skills_root)
            except ValueError:
                continue
            if len(relative.parts) not in (1, 2):
                continue
            leaf = relative.parts[-1]
            self.assertNotIn(leaf, leaves,
                             f"duplicate leaf name '{leaf}': {leaves.get(leaf)} vs {skill_md}")
            leaves[leaf] = skill_md
        self.assertIn("release", leaves)


class TokenBudgetTests(unittest.TestCase):
    def test_new_files_fit_single_file_budget(self) -> None:
        for path in (SKILL_MD, INSTALL_PS1, VERIFY_PS1, MANIFEST, UPDATE_MD,
                     Path(__file__)):
            size = path.stat().st_size
            self.assertLessEqual(size, MAX_BYTES, f"{path.name}: {size} bytes")


if __name__ == "__main__":
    unittest.main()
