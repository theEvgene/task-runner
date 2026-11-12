"""Minimal unit tests for the release request builder.

Fast, stdlib only, no queue, no network, no second host.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "release"))

from request import ReleaseInputError, build_command, build_request, main


NODE = "C:/lenovo/node.exe"
ENTRY = "C:/lenovo/release-tag.mjs"
CHECKOUT = "C:/lenovo/checkout"


class BuildCommandTests(unittest.TestCase):
    def test_preview_is_bare_cmd_form(self) -> None:
        cmd = build_command(NODE, ENTRY)
        self.assertTrue(cmd.startswith('cmd /c "set NODE_OPTIONS=--use-system-ca'))
        self.assertIn(f'"{NODE}" "{ENTRY}"', cmd)
        self.assertNotIn("echo", cmd)
        self.assertNotIn("|", cmd)

    def test_version_requires_confirm(self) -> None:
        with self.assertRaises(ReleaseInputError):
            build_command(NODE, ENTRY, "1.2.3")
        with self.assertRaises(ReleaseInputError):
            build_command(NODE, ENTRY, "1.2.3", None)

    def test_version_is_opaque_to_driver(self) -> None:
        # No semver check here: release-tag owns version handling.
        cmd = build_command(NODE, ENTRY, "v9 custom", "yes")
        self.assertIn("v9 custom", cmd)

    def test_confirm_requires_version(self) -> None:
        with self.assertRaises(ReleaseInputError):
            build_command(NODE, ENTRY, None, "yes")

    def test_confirm_pipes_exact_text_in_cmd_form(self) -> None:
        cmd = build_command(NODE, ENTRY, "1.2.3", "yes")
        self.assertIn("echo yes | ", cmd)
        self.assertIn(f'"{ENTRY}" "1.2.3"', cmd)
        self.assertTrue(cmd.startswith('cmd /c "'))

    def test_line_breaks_refused(self) -> None:
        with self.assertRaises(ReleaseInputError):
            build_command(NODE, ENTRY, "1.0\nrm", "yes")
        with self.assertRaises(ReleaseInputError):
            build_command(NODE, ENTRY, "1.0", "a\nb")

    def test_relative_paths_refused(self) -> None:
        with self.assertRaises(ReleaseInputError):
            build_command("node", ENTRY)
        with self.assertRaises(ReleaseInputError):
            build_command(NODE, "rel/entry.mjs")

    def test_bridge_chars_refused(self) -> None:
        for bad in ('"', "$", "`", "%"):
            with self.assertRaises(ReleaseInputError):
                build_command(NODE, ENTRY, f"1.2{bad}", "yes")
            with self.assertRaises(ReleaseInputError):
                build_command(NODE, ENTRY, "1.2.3", f"ye{bad}s")
            with self.assertRaises(ReleaseInputError):
                build_command(f"C:/len{bad}ovo/node.exe", ENTRY)


class RequestFileTests(unittest.TestCase):
    def test_preview_mode_and_single_line_file(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = main(["--node", NODE, "--release-tag", ENTRY,
                         "--lenovo-checkout", CHECKOUT,
                         "--request-file", str(root / "req.json"),
                         "--command-file", str(root / "cmd.txt")])
            self.assertEqual(code, 0)
            req_text = (root / "req.json").read_text(encoding="utf-8")
            self.assertIn('"mode": "preview"', req_text)
            raw = (root / "cmd.txt").read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(raw.decode("utf-8").count("\n"), 1)

    def test_version_without_confirm_refuses(self) -> None:
        with self.assertRaises(ReleaseInputError):
            build_request(NODE, ENTRY, CHECKOUT, "1.2.3")

    def test_versioned_mode_marks_confirmed(self) -> None:
        req, _ = build_request(NODE, ENTRY, CHECKOUT, "1.2.3", "yes")
        self.assertEqual(req["mode"], "versioned")
        self.assertTrue(req["confirmed"])

    def test_no_key_material_in_request(self) -> None:
        req, cmd = build_request(NODE, ENTRY, CHECKOUT, "1.2.3", "yes")
        blob = str(req) + cmd
        self.assertNotIn("key", blob.lower())


class CmdInjectionTests(unittest.TestCase):
    def test_cmd_metachars_refused_in_version(self) -> None:
        for bad in ("|", "&", "<", ">", "^", "!"):
            with self.assertRaises(ReleaseInputError, msg=f"version {bad!r}"):
                build_command(NODE, ENTRY, f"1.2.3{bad}", "yes")

    def test_cmd_metachars_refused_in_paths(self) -> None:
        for bad in ("|", "&", "<", ">", "^", "!"):
            with self.assertRaises(ReleaseInputError, msg=f"node {bad!r}"):
                build_command(f"C:/len{bad}ovo/node.exe", ENTRY)
            with self.assertRaises(ReleaseInputError, msg=f"checkout {bad!r}"):
                build_request(NODE, ENTRY, f"C:/len{bad}ovo/checkout",
                               "1.2.3", "yes")

    def test_confirm_allows_only_explicit_safe_answer(self) -> None:
        expected = {"yes": "yes", "y": "y", "YES": "yes", "Y": "y"}
        for raw, canonical in expected.items():
            cmd = build_command(NODE, ENTRY, "1.2.3", raw)
            self.assertIn(f"echo {canonical} | ", cmd)
        cmd = build_command(NODE, ENTRY, "1.2.3", "  yes  ")
        self.assertIn("echo yes | ", cmd)

    def test_confirm_canonical_tokens(self) -> None:
        self.assertIn("echo y | ", build_command(NODE, ENTRY, "1.2.3", "Y"))
        self.assertIn("echo yes | ", build_command(NODE, ENTRY, "1.2.3", "YES"))

    def test_confirm_negative_states_never_build_release(self) -> None:
        for bad in ("да", "no", "n", "нет", "exit", "e", "yes please", "yess",
                    "ye s", "yes & dir", "yes|dir", ""):
            with self.assertRaises(ReleaseInputError, msg=f"confirm {bad!r}"):
                build_command(NODE, ENTRY, "1.2.3", bad)

    def test_versioned_line_has_single_pipe_and_quoted_version(self) -> None:
        cmd = build_command(NODE, ENTRY, "1.2.3", "yes")
        self.assertEqual(cmd.count("|"), 1)
        self.assertIn(f'"{ENTRY}" "1.2.3"', cmd)

    def test_parens_in_paths_stay_allowed(self) -> None:
        cmd = build_command("C:/Program Files (x86)/nodejs/node.exe", ENTRY)
        self.assertIn('"C:/Program Files (x86)/nodejs/node.exe"', cmd)


if __name__ == "__main__":
    unittest.main()
