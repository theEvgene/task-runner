"""Exact Draft description round-trip on publish and readback (#11 correction).

The driver reads the description bytes strictly as UTF-8 with no newline
translation and proves the stored MR text equals the full expected text: a
create stores exactly what was sent, a draft update appends it after a
newline to the previously observed text (the base writer contract). A suffix
check is not used: ``unexpected newline sent`` on a create, or a replaced
prefix that preserves the suffix on an update, must be a refused mismatch,
never a success. When the previous text cannot be known the run fails closed
as unknown instead of inferring from a suffix. Fixture only: disposable
repos, the real signed queue, doubles for the external flow; no network.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from dell_fixture import TASK_BRANCH, Workspace, run


FIRST = "Fixture description one: ASCII plus kirillitsa ABC-1\n"
SECOND = "Fixture description two: update line\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(description: str, token: str = "0123456789012345") -> Workspace:
    ws = Workspace().build().build_ready(token)
    (ws.shared / "mr-description.md").write_bytes(description.encode("utf-8"))
    return ws


class DraftDescriptionExactTests(unittest.TestCase):
    def test_create_stores_exact_text(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        job = ws.submit_publish(ws.publish_request(
            description=FIRST, token="fedcba9876543210"))
        payload, proc = ws.run_job(job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        driver = json.loads(proc.stdout)
        self.assertEqual(driver["outcome"], "ok")
        self.assertTrue(driver["mr"]["created"])
        self.assertTrue(driver["mr"]["descriptionVerified"])
        self.assertEqual(driver["mr"]["descriptionSha256"], sha(FIRST))
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"]["1"]["description"], FIRST)

    def test_update_appends_after_newline_without_second_mr(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        job = ws.submit_publish(ws.publish_request(
            description=FIRST, token="fedcba9876543210"))
        self.assertEqual(ws.run_job(job)[1].returncode, 0)
        previous, _ = ws.commit_second_task("published base\ntask work\niteration 2\n")
        ws.stage_bundle()
        retry = ws.publish_request(description=SECOND, token="aaaaaaaaaaaaaaaa",
                                   previous=previous)
        payload, proc = ws.run_job(ws.submit_publish(retry))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        driver = json.loads(proc.stdout)
        self.assertEqual(driver["outcome"], "ok")
        self.assertFalse(driver["mr"]["created"])
        self.assertTrue(driver["mr"]["descriptionVerified"])
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1)
        self.assertEqual(state["mrs"]["1"]["description"], FIRST + "\n" + SECOND)

    def test_create_with_prepended_corruption_is_mismatch(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(description=FIRST, token="bbbbbbbbbbbbbbbb")
        ws.extra_env["DELL_FIXTURE_PREPEND_CORRUPTION"] = "1"
        try:
            job = ws.submit_publish(request)
            payload, proc = ws.run_job(job)
        finally:
            ws.extra_env.pop("DELL_FIXTURE_PREPEND_CORRUPTION", None)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(driver["outcome"], "refused")
        self.assertEqual(driver["stage"], "mr")
        self.assertEqual(driver["error"]["reason"], "description-mismatch")
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        stored = state["mrs"]["1"]["description"]
        self.assertTrue(stored.endswith("\n" + FIRST))
        self.assertNotEqual(stored, FIRST)

    def test_update_with_prepended_corruption_is_mismatch(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        job = ws.submit_publish(ws.publish_request(
            description=FIRST, token="cccccccccccccccc"))
        self.assertEqual(ws.run_job(job)[1].returncode, 0)
        previous, _ = ws.commit_second_task("published base\ntask work\niteration 2\n")
        ws.stage_bundle()
        retry = ws.publish_request(description=SECOND, token="dddddddddddddddd",
                                   previous=previous)
        ws.extra_env["DELL_FIXTURE_PREPEND_CORRUPTION"] = "1"
        try:
            payload, proc = ws.run_job(ws.submit_publish(retry))
        finally:
            ws.extra_env.pop("DELL_FIXTURE_PREPEND_CORRUPTION", None)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(driver["outcome"], "refused")
        self.assertEqual(driver["stage"], "mr")
        self.assertEqual(driver["error"]["reason"], "description-mismatch")
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        stored = state["mrs"]["1"]["description"]
        self.assertTrue(stored.endswith("\n" + SECOND))
        self.assertNotEqual(stored, FIRST + "\n" + SECOND)


if __name__ == "__main__":
    unittest.main()
