"""MR list enumeration: a match past the first page must not duplicate.

``find_open_mr`` reads the open-MR list through the flow client's own paged
reader, or pages the legacy ``api_get`` surface when that reader is absent. A
single-page read would miss a match on a later page and create a second MR for
one branch; these cases pin multi-page accumulation, the incomplete-list
refusal, both legacy fallback outcomes, and the ambiguity refusal. No network:
a stub client plus the fixture paged reader.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qsl

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "dell_lan"
FLOW = TESTS_DIR / "fixture_flow"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(FLOW))

import lenovo_publish_flow  # noqa: E402  (module under test)
import gitlab_api  # noqa: E402  (fixture reader)
from dell_lan_common import Stop  # noqa: E402  (module under test)

HOST = "remote.fixture.invalid"
PROJECT = "group/project"


def record(iid: int, branch: str) -> dict:
    return {
        "iid": iid,
        "source_branch": branch,
        "target_branch": "main",
        "title": f"Draft: feat(ABC-1): fixture {iid}",
        "description": "fixture",
        "draft": True,
        "head_sha": "a" * 40,
        "target_sha": "b" * 40,
        "web_url": gitlab_api.merge_request_url(HOST, PROJECT, iid),
    }


class PagedReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dell-mr-list-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.old_state = os.environ.get("DELL_FIXTURE_STATE")
        os.environ["DELL_FIXTURE_STATE"] = str(self.state)

    def tearDown(self) -> None:
        if self.old_state is None:
            os.environ.pop("DELL_FIXTURE_STATE", None)
        else:
            os.environ["DELL_FIXTURE_STATE"] = self.old_state

    def write_state(self, records: list[dict]) -> None:
        self.state.write_text(json.dumps({
            "host": HOST, "project": PROJECT,
            "nextIid": 1000, "mrs": {str(r["iid"]): r for r in records},
        }), encoding="utf-8")

    def test_match_on_second_page_is_found_not_duplicated(self):
        others = [record(iid, f"task/other-{iid}") for iid in range(1, 101)]
        wanted = record(777, "task/wanted")
        self.write_state(others + [wanted])
        found = lenovo_publish_flow.find_open_mr(
            gitlab_api, HOST, PROJECT, "task/wanted")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["iid"], 777)
        self.assertEqual(found["source_branch"], "task/wanted")

    def test_incomplete_list_is_refused_not_treated_as_empty(self):
        class Truncated:
            GitlabUnavailable = gitlab_api.GitlabUnavailable

            @staticmethod
            def project_api_base(host: str, project: str) -> str:
                return f"projects/{project}"

            @staticmethod
            def paginate(host: str, path: str):
                return {"items": [], "pages": 3, "complete": False,
                        "truncated": True, "error": None}

        with self.assertRaises(Stop) as raised:
            lenovo_publish_flow.find_open_mr(
                Truncated, HOST, PROJECT, "task/wanted")
        self.assertEqual(raised.exception.reason, "mr-list-incomplete")

    def test_legacy_client_without_paginate_reads_a_short_page_once(self):
        calls: list[str] = []

        class Legacy:
            GitlabUnavailable = gitlab_api.GitlabUnavailable

            @staticmethod
            def project_api_base(host: str, project: str) -> str:
                return f"projects/{project}"

            @staticmethod
            def api_get(host: str, path: str):
                calls.append(path)
                return [record(7, "task/wanted")]

        found = lenovo_publish_flow.find_open_mr(
            Legacy, HOST, PROJECT, "task/wanted")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["iid"], 7)
        self.assertEqual(len(calls), 1)
        self.assertIn("page=1", calls[0])

    def test_legacy_client_fallback_finds_a_match_on_a_later_page(self):
        """Regression: one ``api_get`` call would return page one only."""

        calls: list[str] = []

        class LegacyPaged:
            GitlabUnavailable = gitlab_api.GitlabUnavailable

            @staticmethod
            def project_api_base(host: str, project: str) -> str:
                return f"projects/{project}"

            @staticmethod
            def api_get(host: str, path: str):
                calls.append(path)
                page = int(dict(parse_qsl(path.partition("?")[2])).get("page", "1"))
                if page == 1:
                    return [record(iid, f"task/other-{iid}") for iid in range(1, 101)]
                if page == 2:
                    return [record(777, "task/wanted")]
                return []

        found = lenovo_publish_flow.find_open_mr(
            LegacyPaged, HOST, PROJECT, "task/wanted")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["iid"], 777)
        self.assertEqual(len(calls), 2, "a full page must be followed, not trusted")
        self.assertIn("page=2", calls[1])

    def test_legacy_client_fallback_refuses_an_unreadable_later_page(self):
        calls: list[str] = []

        class LegacyFailing:
            GitlabUnavailable = gitlab_api.GitlabUnavailable

            @staticmethod
            def project_api_base(host: str, project: str) -> str:
                return f"projects/{project}"

            @staticmethod
            def api_get(host: str, path: str):
                calls.append(path)
                if len(calls) <= 2:
                    return [record(iid, f"task/other-{iid}")
                            for iid in range(1, 101)]
                raise LegacyFailing.GitlabUnavailable("page three lost")

        with self.assertRaises(Stop) as raised:
            lenovo_publish_flow.find_open_mr(
                LegacyFailing, HOST, PROJECT, "task/wanted")
        self.assertEqual(raised.exception.reason, "mr-list-incomplete")
        self.assertIn("page 3", raised.exception.detail)

    def test_legacy_client_fallback_refuses_a_non_array_page(self):
        class LegacyMalformed:
            GitlabUnavailable = gitlab_api.GitlabUnavailable

            @staticmethod
            def project_api_base(host: str, project: str) -> str:
                return f"projects/{project}"

            @staticmethod
            def api_get(host: str, path: str):
                return {"items": [record(7, "task/wanted")], "complete": True}

        with self.assertRaises(Stop) as raised:
            lenovo_publish_flow.find_open_mr(
                LegacyMalformed, HOST, PROJECT, "task/wanted")
        self.assertEqual(raised.exception.reason, "mr-list-incomplete")

    def test_two_matches_stay_ambiguous(self):
        class Doubled:
            GitlabUnavailable = gitlab_api.GitlabUnavailable

            @staticmethod
            def project_api_base(host: str, project: str) -> str:
                return f"projects/{project}"

            @staticmethod
            def paginate(host: str, path: str):
                return {"items": [record(7, "task/wanted"),
                                  record(8, "task/wanted")],
                        "pages": 1, "complete": True,
                        "truncated": False, "error": None}

        with self.assertRaises(Stop) as raised:
            lenovo_publish_flow.find_open_mr(
                Doubled, HOST, PROJECT, "task/wanted")
        self.assertEqual(raised.exception.reason, "mr-ambiguous")


if __name__ == "__main__":
    unittest.main()
