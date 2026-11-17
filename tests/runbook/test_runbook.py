"""Static contract tests for the agent runbook (issue #64).

These tests read the checked-in document only. They start no process, open no
queue or share, use no key, and reach no host: a document check is not a live
check.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "runbook.md"
AGENTS = ROOT / "AGENTS.md"

ENTRY_HEADER = (
    "Fact",
    "Host role",
    "Concrete source",
    "Revision / launcher / readback",
    "Status",
)
STATE_HEADER = (
    "Case",
    "Observed state",
    "Host role",
    "Source (repo path)",
    "Trusted receipt field",
    "Allowed read-only action",
    "Stop / readback",
)
FIXTURE_HEADER = ("Case", "Proving test", "What it proves")

CASES = {
    "queue-missing",
    "bad-mac-or-id",
    "unknown-no-result",
    "stale-pid",
    "worker-absent",
    "worker-foreign-pid",
    "smb-auth",
    "smb-network",
    "unsupported-runtime",
    "wrong-cwd",
    "drifted-checkout",
    "consent-required",
}
STATUSES = {"OBSERVED-LOCAL", "NOT_VERIFIED", "UNKNOWN"}
HOST_ROLES = {"Dell", "Lenovo", "Dell+Lenovo"}
LIVE_FACTS = {"worker-process", "cleaner-process", "launcher-shortcut"}
PATH_TOKEN = re.compile(r"(?:scripts|tests|docs|skills)/[A-Za-z0-9_./-]+")
FILE_TOKEN = re.compile(r"[A-Za-z0-9_.\-/]+\.(?:py|md|mjs|ps1|bat)")
SCHEMA_TOKEN = re.compile(r"\b[a-z][a-z0-9-]*/\d+\b")
WRITE_VERBS = re.compile(
    r"\b(submit|publish|resubmit|spawn|start|stop|install|delete|remove|kill|"
    r"overwrite|create)\b"
)
DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:\\")
UNC_PATH = re.compile(r"\\\\[A-Za-z0-9._-]+\\")
LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
LOG_FRAME = re.compile(r'^\s+File ".+"')
FORBIDDEN_TEXT = (
    "c:\\users",
    "%localappdata%",
    "%userprofile%",
    "%appdata%",
    "~/.agents",
    "~/.config",
    ".secrets",
    "personal-agent-toolkit",
    "t3-subagents",
    "opencode",
    "id_rsa",
    "-----begin",
    "traceback (most recent call last)",
)
THREAD_FORBIDDEN = (
    "launch.py",
    "launch-worker.bat",
    "worker.py",
    "cleaner.py",
    "scripts/lenovo_worker/launch",
    "submit_job",
    "--authorize-lenovo-check",
)
# Every receipt fact quoted by the runbook, and the module that emits it.
# The runbook may not name a schema or marker that its own modules never
# produce, and the module must still emit every quoted fact.
RECEIPT_FACTS = (
    ("lenovo-launch/1", "scripts/lenovo_worker/launch.py"),
    ("lan-readiness-report/1", "scripts/readiness/check.py"),
    ("launch ready (", "scripts/lenovo_worker/launch.py"),
    ("already-running", "scripts/lenovo_worker/launch.py"),
    ("queue unavailable", "scripts/lenovo_worker/launch.py"),
    ("queue directory is absent", "scripts/readiness/check.py"),
    ("bad credentials", "scripts/lenovo_worker/launch.py"),
    ("share unavailable", "scripts/lenovo_worker/launch.py"),
    ("exited at once", "scripts/lenovo_worker/launch.py"),
    ("exact selected readiness checklist", "scripts/readiness/check.py"),
    ("consent-missing", "scripts/dell_lan/lenovo_remote.py"),
    ("JOBID=", "scripts/lan_bridge/lan_bridge.py"),
)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _section(text: str, title: str) -> str:
    marker = f"## {title}"
    lines = text.splitlines()
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


def _tables(section: str) -> list[tuple[tuple[str, ...], list[list[str]]]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in section.splitlines():
        if line.strip().startswith("|"):
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    tables = []
    for block in blocks:
        rows = [_cells(line) for line in block]
        header, separator, body = rows[0], rows[1], rows[2:]
        if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            raise AssertionError(f"malformed table separator: {separator}")
        tables.append((tuple(header), body))
    return tables


def _table(section: str, header: tuple[str, ...]) -> list[list[str]]:
    for table_header, rows in _tables(section):
        if table_header == header:
            return rows
    raise AssertionError(f"missing table with header {header}")


def _source_files(cell: str) -> list[str]:
    return [match.group(0).rstrip(".") for match in FILE_TOKEN.finditer(cell)]


class RunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not RUNBOOK.is_file():
            raise unittest.SkipTest(f"runbook not present: {RUNBOOK}")
        cls.text = RUNBOOK.read_text(encoding="utf-8")

    def test_runbook_is_linked_from_agents(self) -> None:
        agents = AGENTS.read_text(encoding="utf-8")
        self.assertIn("docs/runbook.md", agents)

    def test_entry_facts_carry_role_source_identity_and_status(self) -> None:
        rows = _table(_section(self.text, "Entry facts"), ENTRY_HEADER)
        self.assertGreaterEqual(len(rows), 5)
        facts = set()
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(len(row), len(ENTRY_HEADER))
                for cell in row:
                    self.assertTrue(cell, f"empty cell in {row}")
                facts.add(row[0])
                self.assertIn(row[1], HOST_ROLES)
                self.assertIn(row[4], STATUSES)
                self.assertRegex(
                    row[3].lower(),
                    r"rev-parse|launch|readback|"
                    r"lan-readiness-report/1|lenovo-launch/1",
                )
                candidates = _source_files(row[2])
                self.assertTrue(candidates, f"no source file in {row[2]}")
                for candidate in candidates:
                    self.assertTrue(
                        (ROOT / candidate).exists(),
                        f"missing source {candidate}",
                    )
        self.assertEqual(
            facts,
            {
                "diagnose-entry",
                "signed-queue",
                "worker-process",
                "cleaner-process",
                "launcher-shortcut",
                "readiness-report",
                "checkout-revision",
            },
        )

    def test_live_process_facts_are_not_verified(self) -> None:
        rows = _table(_section(self.text, "Entry facts"), ENTRY_HEADER)
        by_fact = {row[0]: row for row in rows}
        for fact in LIVE_FACTS:
            self.assertIn(fact, by_fact)
            self.assertIn(by_fact[fact][4], {"NOT_VERIFIED", "UNKNOWN"})

    def test_state_table_covers_every_case_once(self) -> None:
        rows = _table(_section(self.text, "Observed states"), STATE_HEADER)
        cases = [row[0] for row in rows]
        self.assertEqual(len(cases), len(set(cases)))
        self.assertEqual(set(cases), CASES)

    def test_state_rows_are_read_only_and_stop_bounded(self) -> None:
        rows = _table(_section(self.text, "Observed states"), STATE_HEADER)
        for row in rows:
            with self.subTest(case=row[0]):
                self.assertEqual(len(row), len(STATE_HEADER))
                for cell in row:
                    self.assertTrue(cell, f"empty cell in {row[0]}")
                    self.assertNotIn("TODO", cell.upper())
                    self.assertNotIn("TBD", cell.upper())
                self.assertIn(row[2], HOST_ROLES)
                self.assertIsNone(
                    WRITE_VERBS.search(row[5]),
                    f"read-only action mutates: {row[5]}",
                )
                self.assertRegex(
                    row[6],
                    r"^(readback|refusal):",
                    f"stop is not a readback or refusal: {row[6]}",
                )
                candidates = _source_files(row[3])
                self.assertTrue(candidates, f"no source file in {row[3]}")
                for candidate in candidates:
                    self.assertTrue(
                        (ROOT / candidate).exists(),
                        f"missing source {candidate}",
                    )
                self.assertTrue(row[4], "trusted receipt field is empty")

    def test_fixture_map_proves_existing_tests(self) -> None:
        rows = _table(_section(self.text, "Local verification"), FIXTURE_HEADER)
        cases = [row[0] for row in rows]
        self.assertEqual(set(cases), CASES, "every case needs a fixture row")
        for row in rows:
            with self.subTest(case=row[0]):
                self.assertEqual(len(row), len(FIXTURE_HEADER))
                self.assertIn(row[0], CASES)
                self.assertTrue(row[2], "fixture row states no proof")
                parts = [part.strip().strip("`") for part in row[1].split("::")]
                self.assertEqual(len(parts), 3, f"unbound test token: {row[1]}")
                path, class_name, method = parts
                target = ROOT / path
                self.assertTrue(target.is_file(), f"missing test file {path}")
                body = target.read_text(encoding="utf-8")
                self.assertIsNotNone(
                    re.search(
                        rf"^class {re.escape(class_name)}\(", body, re.M
                    ),
                    f"{path} has no class {class_name}",
                )
                self.assertIsNotNone(
                    re.search(rf"^\s+def {re.escape(method)}\(", body, re.M),
                    f"{path} has no method {method}",
                )

    def test_thread_reconnect_starts_no_process_and_requires_consent(
        self,
    ) -> None:
        section = _section(self.text, "Thread reconnect").lower()
        for token in THREAD_FORBIDDEN:
            self.assertNotIn(token, section)
        self.assertIn("never starts a process", section)
        self.assertIn("consent", section)

    def test_no_private_paths_secrets_logs_or_toolkit_runtime(self) -> None:
        lowered = self.text.lower()
        for forbidden in FORBIDDEN_TEXT:
            self.assertNotIn(forbidden, lowered)
        self.assertIsNone(DRIVE_PATH.search(self.text), "drive-absolute path")
        self.assertIsNone(UNC_PATH.search(self.text), "UNC host path")
        self.assertIsNone(LONG_HEX.search(self.text), "literal key or digest")
        self.assertNotIn("```log", lowered, "copied log block")
        for number, line in enumerate(self.text.splitlines(), start=1):
            self.assertIsNone(
                LOG_FRAME.match(line), f"copied log frame at line {number}"
            )

    def test_every_referenced_repo_path_exists(self) -> None:
        for match in PATH_TOKEN.finditer(self.text):
            candidate = match.group(0).rstrip("./")
            if candidate.endswith("::"):
                continue
            self.assertTrue(
                (ROOT / candidate).exists(), f"missing reference {candidate}"
            )

    def test_receipt_fields_resolve_against_their_modules(self) -> None:
        declared = {token for token, _ in RECEIPT_FACTS}
        unowned = set(SCHEMA_TOKEN.findall(self.text)) - declared
        self.assertEqual(
            unowned, set(), "runbook names a schema token with no owning module"
        )
        for token, module in RECEIPT_FACTS:
            with self.subTest(token=token):
                self.assertIn(token, self.text, f"runbook no longer states {token}")
                body = (ROOT / module).read_text(encoding="utf-8")
                self.assertIn(token, body, f"{module} does not emit {token}")


if __name__ == "__main__":
    unittest.main()
