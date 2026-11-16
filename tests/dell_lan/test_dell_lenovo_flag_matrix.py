"""Focused flag matrix: base has checkout only, transfer/publish add worktree.

Guards the review correction on the verified Lenovo path commit: `base`
exports from the checkout and never takes `--lenovo-worktree`, while
`transfer` (like `publish`/`publish-existing`) takes both. Also pins
that no `dell_request.py` subcommand takes `--lenovo-client-dir` and
that the bridge doc keeps the task-runner Jira CLI separate from the
managed external read.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dell_fixture import REQUEST_CLI, ROOT

import sys

sys.path.insert(0, str(ROOT / "scripts" / "dell_lan"))
from dell_request import build_parser  # noqa: E402


def options(subcommand: str) -> set[str]:
    parser = build_parser()
    for action in parser._actions:
        pass
    # Find the subparser for the named command and list its option strings.
    sub = None
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            sub = action
            break
    assert sub is not None
    subparser = sub.choices[subcommand]
    names: set[str] = set()
    for act in subparser._actions:
        names.update(getattr(act, "option_strings", ()))
    return names


class LenovoFlagMatrixTests(unittest.TestCase):
    def test_base_has_checkout_without_worktree(self):
        opts = options("base")
        self.assertIn("--lenovo-checkout", opts)
        self.assertNotIn("--lenovo-worktree", opts)
        self.assertNotIn("--lenovo-flow-dir", opts)
        self.assertNotIn("--lenovo-client-dir", opts)

    def test_transfer_has_checkout_and_worktree_without_flow(self):
        opts = options("transfer")
        self.assertIn("--lenovo-checkout", opts)
        self.assertIn("--lenovo-worktree", opts)
        self.assertNotIn("--lenovo-flow-dir", opts)
        self.assertNotIn("--lenovo-client-dir", opts)

    def test_publish_and_publish_existing_have_worktree_and_flow(self):
        for command in ("publish", "publish-existing"):
            with self.subTest(command=command):
                opts = options(command)
                self.assertIn("--lenovo-checkout", opts)
                self.assertIn("--lenovo-worktree", opts)
                self.assertIn("--lenovo-flow-dir", opts)
                self.assertNotIn("--lenovo-client-dir", opts)

    def test_first_has_neither_checkout_nor_worktree(self):
        opts = options("first")
        self.assertNotIn("--lenovo-checkout", opts)
        self.assertNotIn("--lenovo-worktree", opts)
        self.assertNotIn("--lenovo-flow-dir", opts)
        self.assertNotIn("--lenovo-client-dir", opts)

    def test_bridge_doc_keeps_jira_transports_separate(self):
        text = (ROOT / "docs" / "lan-bridge-config.md").read_text(encoding="utf-8")
        self.assertIn("scripts/jira-protocol.mjs", text)
        self.assertIn("nessy-jira-workflow", text)
        lowered = text.lower()
        self.assertIn("no lan", lowered)
        # The doc must never claim base takes a worktree.
        self.assertNotIn("take the same skill/driver/python", text)
        self.assertIn("`base` takes `--lenovo-checkout`", text)
        # Username-free: no drive user profile is embedded.
        self.assertNotIn("C:\\Users\\", text)
        self.assertNotIn("C:/Users/", text)

    def test_request_cli_exists(self):
        self.assertTrue(Path(REQUEST_CLI).is_file())


if __name__ == "__main__":
    unittest.main()
