"""Fast unit tests for real-CLI compatibility (no network, no git).

Covers the blocking gap: real GitLab API MR responses carry
``diff_refs:{base_sha,head_sha,start_sha}``, ``sha``, ``state``,
``source_branch``/``target_branch`` and no flat ``targetSha``. The open
route must fetch the live target tip separately (diff base is stale), the
merged route must read the diff base, identity must bind to the exact
requested MR URL/IID, and the CLI route must be ``glab api`` (never
``glab mr view --output json``).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = TESTS_DIR.parents[1] / "scripts" / "mirror_mr"
sys.path.insert(0, str(SCRIPTS))

import lenovo_mr_export as open_driver  # noqa: E402
import lenovo_mr_history_export as hist_driver  # noqa: E402
from mirror_mr_common import Stop, check_export_identity  # noqa: E402

HOST = "gitlab.example.invalid"
PROJECT = "group/project"
IID = 7
MR_URL = f"https://{HOST}/{PROJECT}/-/merge_requests/{IID}"
HEAD = "a" * 40
DIFF_BASE = "b" * 40
START = "c" * 40
LIVE_TIP = "d" * 40

REQUEST = {"gitlabHost": HOST, "gitlabProject": PROJECT, "mrIid": IID,
           "mrUrl": MR_URL}


def open_api_record() -> dict:
    return {"id": 1, "iid": IID, "state": "opened",
            "source_branch": "feature/open-142", "target_branch": "main",
            "sha": HEAD,
            "diff_refs": {"base_sha": DIFF_BASE, "head_sha": HEAD,
                          "start_sha": START},
            "web_url": MR_URL, "title": "t", "description": "d"}


def merged_api_record() -> dict:
    return {"id": 1, "iid": IID, "state": "merged",
            "source_branch": "feature/open-142", "target_branch": "main",
            "sha": HEAD,
            "diff_refs": {"base_sha": DIFF_BASE, "head_sha": HEAD,
                          "start_sha": START},
            "web_url": MR_URL, "title": "t", "description": "d"}


class RealCliTests(unittest.TestCase):
    def test_open_real_response_uses_live_tip_not_diff_base(self) -> None:
        """The live target tip wins; a stale diff base never becomes target."""

        observed = open_driver.normalise(open_api_record(), REQUEST, LIVE_TIP)
        self.assertEqual(observed["sourceSha"], HEAD)
        self.assertEqual(observed["targetSha"], LIVE_TIP)
        self.assertNotEqual(observed["targetSha"], DIFF_BASE)

    def test_open_head_divergence_refuses_as_moved(self) -> None:
        """sha vs diff head divergence is a move, never a silent snapshot."""

        record = open_api_record()
        record["diff_refs"] = {"base_sha": DIFF_BASE, "head_sha": "e" * 40,
                               "start_sha": START}
        with self.assertRaises(Stop) as ctx:
            open_driver.normalise(record, REQUEST, LIVE_TIP)
        self.assertEqual(ctx.exception.reason, "mr-moved")

    def test_open_missing_live_tip_refuses(self) -> None:
        """Without the live tip the diff base alone proves nothing."""

        with self.assertRaises(Stop) as ctx:
            open_driver.normalise(open_api_record(), REQUEST, None)
        self.assertEqual(ctx.exception.reason, "mr-unreadable")

    def test_open_identity_binds_exact_mr(self) -> None:
        """Another IID or URL never satisfies this request."""

        bad_iid = open_api_record()
        bad_iid["iid"] = IID + 1
        with self.assertRaises(Stop):
            open_driver.normalise(bad_iid, REQUEST, LIVE_TIP)
        bad_url = open_api_record()
        bad_url["web_url"] = bad_url["web_url"].replace(f"/{IID}", "/99")
        with self.assertRaises(Stop):
            open_driver.normalise(bad_url, REQUEST, LIVE_TIP)

    def test_open_route_uses_glab_api_and_branch_tip(self) -> None:
        """The CLI route is glab api for MR plus live branch tip."""

        seen: list[list[str]] = []

        class Proc:
            def __init__(self, stdout: str):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(argv, **kwargs):
            seen.append(list(argv))
            if "/merge_requests/" in argv[2]:
                return Proc(json.dumps(open_api_record()))
            if "/repository/branches/" in argv[2]:
                return Proc(json.dumps({"commit": {"id": LIVE_TIP}}))
            raise AssertionError(f"unexpected endpoint: {argv}")

        with patch.object(open_driver.subprocess, "run", side_effect=fake_run):
            observed = open_driver.via_glab(REQUEST)
        self.assertEqual(observed["sourceSha"], HEAD)
        self.assertEqual(observed["targetSha"], LIVE_TIP)
        self.assertEqual(len(seen), 2)
        for argv in seen:
            self.assertEqual(argv[:2], ["glab", "api"])
            self.assertIn("--hostname", argv)
            self.assertIn(HOST, argv)
            self.assertNotIn("view", argv)
        self.assertTrue(any("/merge_requests/7" in a for a in seen[0]))
        self.assertTrue(any("/repository/branches/" in a for a in seen[1]))

    def test_merged_real_response_reads_diff_base(self) -> None:
        """Merged history uses diff base/head, not a flat target SHA."""

        observed = hist_driver.normalise(merged_api_record(), REQUEST)
        self.assertEqual(observed["headSha"], HEAD)
        self.assertEqual(observed["baseSha"], DIFF_BASE)

    def test_merged_route_uses_glab_api(self) -> None:
        """Merged reads go through glab api, never mr view json."""

        seen: list[list[str]] = []

        class Proc:
            def __init__(self, stdout: str):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(argv, **kwargs):
            seen.append(list(argv))
            return Proc(json.dumps(merged_api_record()))

        with patch.object(hist_driver.subprocess, "run", side_effect=fake_run):
            observed = hist_driver.via_glab(REQUEST)
        self.assertEqual(observed["headSha"], HEAD)
        self.assertEqual(seen[0][:2], ["glab", "api"])
        self.assertNotIn("view", seen[0])

    def test_mr_head_ref_helper_exists_for_deleted_source(self) -> None:
        """Deleted source branches fall back to refs/merge-requests/<iid>/head."""

        self.assertTrue(hasattr(hist_driver, "ensure_head"))
        import inspect

        source = inspect.getsource(hist_driver.ensure_head)
        self.assertIn("refs/merge-requests/", source)
        self.assertIn("/head", source)

    def test_payload_identity_binds_mr_url_and_iid(self) -> None:
        """A sealed payload for another MR proves nothing for this one."""

        good = {"mrUrl": MR_URL, "mrIid": IID}
        check_export_identity(good, MR_URL, IID)
        with self.assertRaises(Stop):
            check_export_identity({"mrUrl": MR_URL, "mrIid": IID + 1},
                                  MR_URL, IID)
        with self.assertRaises(Stop):
            check_export_identity(
                {"mrUrl": MR_URL.replace(f"/{IID}", "/99"), "mrIid": 99},
                MR_URL, IID)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
