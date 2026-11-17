"""Static contract tests for the operator START HERE page (issue #63).

These tests read the checked-in page and the modules it quotes. They start no
process, open no queue or share, use no key, and reach no host: a document
check is not a live check.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "start-here.md"
AGENTS = ROOT / "AGENTS.md"

PATH_TOKEN = re.compile(r"(?:scripts|tests|docs|skills)/[A-Za-z0-9_./-]+")
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:\\")
UNC_PATH = re.compile(r"\\\\[A-Za-z0-9._-]+\\")
LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
FORBIDDEN_TEXT = (
    "c:\\users",
    ".secrets",
    "-----begin",
    "traceback (most recent call last)",
)
PLACEHOLDERS = {
    "<python-on-dell>",
    "<readiness-config.json>",
    "<lenovo-config.json>",
    "<shortcut>",
    "<job-id>",
}
ENTRYPOINTS = (
    "docs/readiness.md",
    "docs/lenovo-worker-shortcut.md",
    "docs/lan-bridge.md",
    "docs/runbook.md",
    "docs/release.md",
    "scripts/readiness/check.py",
    "scripts/lenovo_worker/Launch-Worker.bat",
    "skills/release/SKILL.md",
)
RECEIPT_TOKENS = (
    ("lan-readiness-report/1", "scripts/readiness/check.py"),
    ("lenovo-launch/1", "scripts/lenovo_worker/launch.py"),
    ("already-running", "scripts/lenovo_worker/launch.py"),
    ("queue-missing", "scripts/lenovo_worker/reasons.py"),
    ("smb-share-unconfirmed", "scripts/lenovo_worker/reasons.py"),
    ("role-ambiguous", "scripts/lenovo_worker/reasons.py"),
)


class StartHereTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = PAGE.read_text(encoding="utf-8")

    def test_page_is_linked_from_agents(self) -> None:
        agents = AGENTS.read_text(encoding="utf-8")
        self.assertIn("docs/start-here.md", agents)

    def test_every_referenced_repo_path_exists(self) -> None:
        for entrypoint in ENTRYPOINTS:
            self.assertTrue(
                (ROOT / entrypoint).exists(), f"missing entrypoint {entrypoint}"
            )
            self.assertIn(entrypoint, self.text, f"page lost {entrypoint}")
        for match in PATH_TOKEN.finditer(self.text):
            candidate = match.group(0).rstrip("./")
            self.assertTrue(
                (ROOT / candidate).exists(), f"missing reference {candidate}"
            )

    def test_placeholders_are_explicit_and_not_invented_facts(self) -> None:
        for placeholder in PLACEHOLDERS:
            self.assertIn(placeholder, self.text)
        self.assertIn("are placeholders", self.text)

    def test_both_hosts_show_success_one_failure_action_and_runbook_route(
        self,
    ) -> None:
        lowered = self.text.lower()
        dell = self.section("Dell: one readiness check and one test submit")
        lenovo = self.section("Lenovo: start worker and cleaner")
        for section in (dell, lenovo):
            self.assertIn("Success:", section)
            self.assertIn("exit `2`", section)
        self.assertIn("scripts/readiness/check.py", dell)
        self.assertIn("<shortcut>", lenovo)
        self.assertIn("docs/runbook.md", self.text)
        self.assertIn("issue #64", self.text)
        self.assertIn("never submit again", lowered)
        self.assertIn("second worker", lowered)

    def test_live_states_stay_unverified(self) -> None:
        lowered = self.text.lower()
        self.assertIn("`not_run`", lowered)
        self.assertIn("`unknown`", lowered)
        self.assertIn("never `pass`", lowered)
        self.assertNotIn("status: pass", lowered)

    def test_no_private_paths_secrets_or_copied_logs(self) -> None:
        lowered = self.text.lower()
        for forbidden in FORBIDDEN_TEXT:
            self.assertNotIn(forbidden, lowered)
        self.assertIsNone(DRIVE_PATH.search(self.text), "drive-absolute path")
        self.assertIsNone(UNC_PATH.search(self.text), "UNC host path")
        self.assertIsNone(LONG_HEX.search(self.text), "literal key or digest")
        self.assertNotIn("```log", lowered, "copied log block")

    def test_receipt_tokens_resolve_against_their_modules(self) -> None:
        for token, module in RECEIPT_TOKENS:
            with self.subTest(token=token):
                self.assertIn(token, self.text, f"page no longer states {token}")
                body = (ROOT / module).read_text(encoding="utf-8")
                self.assertIn(token, body, f"{module} does not emit {token}")

    def test_page_stays_a_short_operator_sheet(self) -> None:
        self.assertLessEqual(len(self.text.splitlines()), 140)

    def section(self, title: str) -> str:
        marker = f"## {title}"
        lines = self.text.splitlines()
        try:
            start = lines.index(marker)
        except ValueError as error:
            raise AssertionError(f"missing section {marker!r}") from error
        body = []
        for line in lines[start + 1 :]:
            if line.startswith("## "):
                break
            body.append(line)
        return "\n".join(body)


if __name__ == "__main__":
    unittest.main()
