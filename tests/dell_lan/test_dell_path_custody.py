"""Key custody: the queue key is proven outside every protected root.

The regression this module locks: a key file placed inside the shared LAN
folder was accepted, and a path alias could hide a key that the plain spelling
would have refused. Every check here must resolve the path first — symlinks,
junctions, ``subst`` drives, 8.3 short names, case and separators — and must
refuse the key when the separation cannot be proven instead of assuming it.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan"
sys.path.insert(0, str(SCRIPTS))

import dell_lan_common as common  # noqa: E402  (script module under test)

from dell_fixture import Workspace  # noqa: E402  (shared disposable fixture)


def short_path(value: Path) -> str | None:
    """The Windows 8.3 spelling of an existing path, when the volume has one."""

    if os.name != "nt":
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    written = ctypes.windll.kernel32.GetShortPathNameW(str(value), buffer, len(buffer))
    if written == 0:
        return None
    short = buffer.value
    if os.path.normcase(short) == os.path.normcase(str(value)):
        return None
    return short


def link_directory(target: Path, link: Path) -> None:
    """A directory link: a symlink where allowed, otherwise an unprivileged NTFS junction."""

    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not link.is_dir():
        raise OSError(f"mklink /J failed: {(proc.stderr or proc.stdout).strip()}")


class ContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-custody-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = self.root / "lan-share"
        self.shared.mkdir()
        self.key = self.root / "key.bin"
        self.key.write_bytes(b"k" * 32)

    def test_a_key_outside_the_root_is_outside(self):
        self.assertFalse(common.containment(self.key, self.shared))

    def test_a_key_directly_inside_the_root_is_inside(self):
        inside = self.shared / "key.bin"
        inside.write_bytes(b"k" * 32)
        self.assertTrue(common.containment(inside, self.shared))

    def test_a_sibling_with_the_root_as_prefix_is_not_inside(self):
        sibling = self.root / "lan-share-other"
        sibling.mkdir()
        candidate = sibling / "key.bin"
        candidate.write_bytes(b"k" * 32)
        self.assertFalse(common.containment(candidate, self.shared))

    def test_a_dot_dot_spelling_is_normalised(self):
        (self.shared / "sub").mkdir()
        inside = self.shared / "sub" / ".." / "key.bin"
        inside.write_bytes(b"k" * 32)
        self.assertTrue(common.containment(inside, self.shared))

    def test_a_case_and_separator_alias_is_inside(self):
        if os.name != "nt":
            self.skipTest("case-insensitive aliasing is a Windows property")
        (self.shared / "key.bin").write_bytes(b"k" * 32)
        alias = Path(str(self.shared).upper().replace("\\", "/")) / "key.bin"
        self.assertTrue(common.containment(alias, self.shared))

    def test_an_8_3_short_name_alias_is_inside(self):
        long_dir = self.shared / "a-rather-long-shared-folder-name"
        long_dir.mkdir()
        (long_dir / "queue-key.bin").write_bytes(b"k" * 32)
        short = short_path(long_dir)
        if short is None:
            self.skipTest("this volume exposes no 8.3 short name")
        self.assertTrue(common.containment(Path(short) / "queue-key.bin", self.shared))

    def test_a_symlinked_parent_is_inside(self):
        try:
            link_directory(self.shared, self.root / "share-link")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        (self.shared / "key.bin").write_bytes(b"k" * 32)
        self.assertTrue(common.containment(self.root / "share-link" / "key.bin",
                                          self.shared))

    def test_a_link_entry_inside_the_root_is_inside(self):
        outside = self.root / "keys"
        outside.mkdir()
        (outside / "key.bin").write_bytes(b"k" * 32)
        entry = self.shared / "innocent.bin"
        try:
            os.link(outside / "key.bin", entry)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"hard link creation unavailable: {error}")
        self.assertTrue(common.containment(entry, self.shared))

    def test_an_unresolvable_path_is_unproven_not_outside(self):
        def boom(self, strict=False):
            raise OSError("access denied")

        with mock.patch.object(Path, "resolve", boom):
            self.assertIsNone(common.containment(self.key, self.shared))

    def test_read_key_refuses_an_unproven_root(self):
        with mock.patch.object(common, "containment", lambda *args: None):
            with self.assertRaises(common.Stop) as caught:
                common.read_key(object(), self.root / "queue", self.key, "raw",
                                repos=[self.root / "checkout"],
                                shared=self.shared)
        self.assertEqual(caught.exception.reason, "key-refused")
        self.assertIn("cannot prove", caught.exception.detail)

    def test_read_key_refuses_when_no_shared_root_is_named(self):
        with self.assertRaises(common.Stop) as caught:
            common.read_key(object(), self.root / "queue", self.key, "raw",
                            repos=[self.root / "checkout"])
        self.assertEqual(caught.exception.reason, "key-refused")
        self.assertIn("shared LAN folder root", caught.exception.detail)

    def test_read_key_refuses_when_no_checkout_root_is_named(self):
        with self.assertRaises(common.Stop) as caught:
            common.read_key(object(), self.root / "queue", self.key, "raw",
                            shared=self.shared)
        self.assertEqual(caught.exception.reason, "key-refused")
        self.assertIn("checkout root", caught.exception.detail)

    def test_read_key_refuses_a_relative_root(self):
        with self.assertRaises(common.Stop) as caught:
            common.read_key(object(), self.root / "queue", self.key, "raw",
                            repos=[self.root / "checkout"],
                            shared=Path("lan-share"))
        self.assertEqual(caught.exception.reason, "key-refused")
        self.assertIn("absolute", caught.exception.detail)


class BaseImportAliasTests(unittest.TestCase):
    """The real importer refuses a shared-folder key spelled through any alias."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = Workspace().build()
        cls.job = cls.ws.request_base("00000000000000ee")
        cls.ws.run_job(cls.job)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def test_a_key_reached_through_a_symlinked_parent_is_refused(self):
        key = self.ws.shared / "aliased-key.bin"
        key.write_bytes(self.ws.key_bytes)
        try:
            link_directory(self.ws.shared, self.ws.root / "share-alias")
        except (OSError, NotImplementedError) as error:
            key.unlink(missing_ok=True)
            self.skipTest(f"symlink creation unavailable: {error}")
        try:
            proc, data = self.ws.import_base_to(
                self.ws.root / "copy-alias", self.job,
                "--key-file", str(self.ws.root / "share-alias" / "aliased-key.bin"))
        finally:
            key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("shared", data["detail"])
        self.assertFalse((self.ws.root / "copy-alias").exists())

    def test_a_key_reached_through_a_short_name_is_refused(self):
        key = self.ws.shared / "short-key.bin"
        key.write_bytes(self.ws.key_bytes)
        short = short_path(self.ws.shared)
        if short is None:
            key.unlink(missing_ok=True)
            self.skipTest("this volume exposes no 8.3 short name")
        try:
            proc, data = self.ws.import_base_to(
                self.ws.root / "copy-short", self.job,
                "--key-file", str(Path(short) / "short-key.bin"))
        finally:
            key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")
        self.assertIn("shared", data["detail"])

    def test_a_case_alias_of_a_shared_key_is_refused(self):
        if os.name != "nt":
            self.skipTest("case-insensitive aliasing is a Windows property")
        key = self.ws.shared / "case-key.bin"
        key.write_bytes(self.ws.key_bytes)
        alias = Path(str(self.ws.shared).upper()) / "case-key.bin"
        try:
            proc, data = self.ws.import_base_to(
                self.ws.root / "copy-case", self.job, "--key-file", str(alias))
        finally:
            key.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "key-refused")


if __name__ == "__main__":
    unittest.main()
