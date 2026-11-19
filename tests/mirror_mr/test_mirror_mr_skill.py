"""Focused tests for the mirror-mr skill and single entry (issue #144).

Disposable fixtures only: the `Workspace` two-remote rig, the real signed
queue, and the documented `gitlab_mr_reader`/`github_ops` doubles. No live
GitLab, GitHub, or Lenovo. Each test names its contract first.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "mirror_mr"

sys.path.insert(0, str(TESTS_DIR))

from mirror_fixture import MR_URL, Workspace, git  # noqa: E402


def cli(ws: Workspace, *extra: str) -> tuple:
    from mirror_fixture import run
    state = ws.root / "cli-state.json"
    receipt = ws.root / "cli-receipt.json"
    cmd = [sys.executable, str(SCRIPTS / "mirror_mr.py"),
           "--mr-url", MR_URL, "--mapping-file", str(ws.mapping),
           "--shared", str(ws.shared), "--mirror-dir", str(ws.mirror),
           "--queue", str(ws.queue), "--key-file", str(ws.key),
           "--lenovo-checkout", str(ws.lenovo), "--python", sys.executable,
           "--lenovo-driver-dir", str(SCRIPTS),
           "--lenovo-flow-dir", str(TESTS_DIR),
           "--state", str(state), "--receipt", str(receipt),
           "--fetch-timeout", "0.4", "--poll", "0.05",
           "--github-dir", str(TESTS_DIR), *extra]
    proc = run(cmd, env=ws.env())
    data = None
    if receipt.is_file():
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
    cli_state = None
    if state.is_file():
        cli_state = json.loads(state.read_text(encoding="utf-8"))
    return proc, data, cli_state


def run_job_for_cli(ws: Workspace, cli_state: dict, mode: str = "open",
                    expect_ok: bool = True) -> None:
    job_id = cli_state["attempts"][mode]["jobId"]
    _, proc = ws.run_job(job_id)
    if expect_ok:
        assert proc.returncode == 0, proc.stderr


def mark_merged(ws: Workspace) -> None:
    state = json.loads(ws.gitlab_state.read_text(encoding="utf-8"))
    record = state["mrs"]["7"]
    record["status"] = "merged"
    ws.gitlab_state.write_text(json.dumps(state), encoding="utf-8")


class SkillRoutingTests(unittest.TestCase):
    def test_skill_triggers_and_names_canonical_command(self) -> None:
        """A natural open/copy request reaches the single CLI entry."""

        body = (ROOT / "skills" / "mirror-mr" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("open", body.lower())
        self.assertIn("scripts/mirror_mr/mirror_mr.py", body)
        self.assertIn("--mr-url", body)

    def test_skill_distinguishes_url_from_ambiguous_and_missing(self) -> None:
        """An explicit URL proceeds; ambiguity and missing mapping stop."""

        body = (ROOT / "skills" / "mirror-mr" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("ambiguous", body.lower())
        self.assertIn("no mirror configured", body.lower() + "missing mapping")
        self.assertIn("Never guess", body)

    def test_skill_follows_private_evidence_recovery_contracts(self) -> None:
        """Private readback, same-id resume, and no-merge limits are stated."""

        body = (ROOT / "skills" / "mirror-mr" / "SKILL.md").read_text(encoding="utf-8")
        lowered = body.lower()
        for token in ("private", "same", "never duplicates", "never merge",
                      "pr url", "opened", "merged"):
            self.assertIn(token, lowered)


class InstallerDiscoveryTests(unittest.TestCase):
    def test_skill_is_discoverable_with_unique_leaf(self) -> None:
        leaves: dict[str, Path] = {}
        for skill_md in sorted((ROOT / "skills").rglob("SKILL.md")):
            try:
                relative = skill_md.parent.relative_to(ROOT / "skills")
            except ValueError:
                continue
            if len(relative.parts) not in (1, 2):
                continue
            leaf = relative.parts[-1]
            self.assertNotIn(leaf, leaves)
            leaves[leaf] = skill_md
        self.assertIn("mirror-mr", leaves)
        self.assertIn("release", leaves)

    def test_agent_entry_and_docs_name_skill_and_command(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("skills/mirror-mr/SKILL.md", agents)
        mirror = (ROOT / "docs" / "mirror-mr.md").read_text(encoding="utf-8")
        self.assertIn("scripts/mirror_mr/mirror_mr.py", mirror)
        self.assertIn("--mode auto", mirror)


class SingleEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace().build()

    def tearDown(self) -> None:
        self.ws.teardown()

    def test_open_mr_completes_and_resume_reconciles(self) -> None:
        """One URL yields one verified PR; an unknown first pass resumes it."""

        proc, _, cli_state = cli(self.ws, "--mode", "open")
        self.assertEqual(proc.returncode, 3)  # no worker yet: unknown, same id kept
        job_id = cli_state["attempts"]["open"]["jobId"]
        run_job_for_cli(self.ws, cli_state, "open")
        proc2, receipt2, cli_state2 = cli(self.ws, "--mode", "open")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn(receipt2["verdict"], ("mirrored", "reconciled"))
        self.assertTrue(str(receipt2["prUrl"]).startswith("https://"))
        self.assertEqual(receipt2["sourceSha"], self.ws.feature)
        self.assertEqual(receipt2["targetSha"], self.ws.base)
        self.assertEqual(cli_state2["attempts"]["open"]["jobId"], job_id)
        proc3, receipt3, _ = cli(self.ws, "--mode", "open")
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        self.assertEqual(receipt3["verdict"], "reconciled")
        self.assertEqual(receipt3["prUrl"], receipt2["prUrl"])
        state = json.loads((self.ws.root / "github-state.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(len(state["prs"]), 1)

    def test_auto_mode_completes_merged_mr_on_history_route(self) -> None:
        """Auto determines merged status through the signed route (new id)."""

        mark_merged(self.ws)
        git("checkout", "-q", "main", cwd=self.ws.lenovo)
        git("push", "-q", "origin", ":feature/open-142", cwd=self.ws.lenovo)
        proc, _, cli_state = cli(self.ws)  # auto: open job submitted, unknown
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("open", cli_state["attempts"])
        run_job_for_cli(self.ws, cli_state, "open", expect_ok=False)  # sealed refusal: merged
        proc2, _, cli_state2 = cli(self.ws)  # auto: merged job submitted, unknown
        self.assertEqual(proc2.returncode, 3, proc2.stderr)
        self.assertIn("merged", cli_state2["attempts"])
        self.assertNotEqual(cli_state2["attempts"]["merged"]["jobId"],
                            cli_state["attempts"]["open"]["jobId"])
        run_job_for_cli(self.ws, cli_state2, "merged")
        proc3, receipt3, _ = cli(self.ws)
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        self.assertIn(receipt3["verdict"], ("mirrored", "reconciled"))
        self.assertTrue(str(receipt3["prUrl"]).startswith("https://"))
        self.assertEqual(receipt3["historicalHead"], self.ws.feature)
        self.assertEqual(receipt3["comparison"], "exact")

    def test_explicit_mode_refuses_other_status_without_write(self) -> None:
        """A pinned open route refuses a merged MR before any publication."""

        mark_merged(self.ws)
        git("checkout", "-q", "main", cwd=self.ws.lenovo)
        proc, _, cli_state = cli(self.ws, "--mode", "open")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        run_job_for_cli(self.ws, cli_state, "open", expect_ok=False)  # sealed refusal
        proc2, receipt2, _ = cli(self.ws, "--mode", "open",
                                 "--fetch-timeout", "3")
        self.assertEqual(proc2.returncode, 1, proc2.stderr)
        self.assertEqual(receipt2["reason"], "export-job-failed")
        self.assertIn("mr-not-open", str(receipt2.get("detail") or ""))
        self.assertIsNone(receipt2["prUrl"])
        remote = git("ls-remote", "--heads", str(self.ws.github_origin))
        self.assertEqual(remote, "")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
