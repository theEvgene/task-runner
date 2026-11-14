"""Exact Draft description round-trip on publish and readback (#11 correction).

The driver reads the description bytes strictly as UTF-8 with no newline
translation and proves the stored MR text equals the full signed text on both
create and update. A suffix check is not used: ``unexpected newline sent``
must be a refused mismatch, never a success. Reconciliation repeats the same
single-MR full-text check before Dell can bind publication. Fixture only:
disposable repos, the real signed queue, doubles for the external flow; no
network.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from dell_fixture import TASK_BRANCH, Workspace, run


FIRST = "Описание Draft MR: ASCII ABC-1\r\nВторая строка\n\n"
SECOND = "Обновление: ASCII update-2\nЕщё одна строка\r\n"


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
        request = ws.publish_request(description=FIRST, token="fedcba9876543210")
        signed = json.loads(Path(request["requestPath"]).read_text(encoding="utf-8"))
        self.assertEqual(signed["descriptionBytes"], len(FIRST.encode("utf-8")))
        self.assertEqual(signed["descriptionSha256"], sha(FIRST))
        job = ws.submit_publish(request)
        payload, proc = ws.run_job(job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        driver = json.loads(proc.stdout)
        self.assertEqual(driver["outcome"], "ok")
        self.assertTrue(driver["mr"]["created"])
        self.assertTrue(driver["mr"]["descriptionVerified"])
        self.assertEqual(driver["mr"]["descriptionSha256"], sha(FIRST))
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"]["1"]["description"], FIRST)

    def test_valid_utf8_replacement_after_signing_is_refused_before_remote_write(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(description=FIRST, token="9090909090909090")
        (ws.shared / "mr-description.md").write_bytes(SECOND.encode("utf-8"))

        payload, proc = ws.run_job(ws.submit_publish(request))

        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(driver["stage"], "request")
        self.assertEqual(driver["error"]["reason"], "description-digest-mismatch")
        self.assertIsNone(driver["import"])
        self.assertIsNone(driver["push"])
        self.assertIsNone(driver["mr"])
        self.assertNotEqual(run([
            "git", "-C", str(ws.origin), "rev-parse", "--verify",
            f"refs/heads/{TASK_BRANCH}",
        ]).returncode, 0)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {})

    def test_update_replaces_with_complete_signed_text_without_second_mr(self):
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
        self.assertEqual(state["mrs"]["1"]["description"], SECOND)

    def test_missing_description_is_refused_before_remote_write(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(
            description=FIRST, token="eeeeeeeeeeeeeeee")
        (ws.shared / "mr-description.md").unlink()

        payload, proc = ws.run_job(ws.submit_publish(request))

        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(driver["stage"], "request")
        self.assertEqual(driver["error"]["reason"], "description-missing")
        self.assertIsNone(driver["push"])
        self.assertIsNone(driver["mr"])
        self.assertNotEqual(run([
            "git", "-C", str(ws.origin), "rev-parse", "--verify",
            f"refs/heads/{TASK_BRANCH}",
        ]).returncode, 0)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {})

    def test_invalid_utf8_description_is_refused_before_remote_write(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(
            description=FIRST, token="ffffffffffffffff")
        invalid = b"valid prefix\n\xff"
        (ws.shared / "mr-description.md").write_bytes(invalid)
        ws.rewrite_request(
            request,
            descriptionBytes=len(invalid),
            descriptionSha256=hashlib.sha256(invalid).hexdigest(),
        )

        payload, proc = ws.run_job(ws.submit_publish(request))

        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(driver["stage"], "request")
        self.assertEqual(driver["error"]["reason"], "description-invalid-utf8")
        self.assertIsNone(driver["push"])
        self.assertIsNone(driver["mr"])
        self.assertNotEqual(run([
            "git", "-C", str(ws.origin), "rev-parse", "--verify",
            f"refs/heads/{TASK_BRANCH}",
        ]).returncode, 0)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {})

    def test_description_readback_unknown_reconciles_without_duplicate(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(
            authorize="import,push,mr,reconcile", description=FIRST,
            token="abababababababab")
        ws.extra_env["DELL_FIXTURE_FAIL_DESCRIPTION_READBACK"] = "1"
        try:
            payload, proc = ws.run_job(ws.submit_publish(request))
        finally:
            ws.extra_env.pop("DELL_FIXTURE_FAIL_DESCRIPTION_READBACK", None)

        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(driver["outcome"], "unknown")
        self.assertEqual(driver["stage"], "mr")
        self.assertEqual(driver["error"]["reason"], "mr-readback-unavailable")
        before = ws.state_path.read_bytes()
        self.assertEqual(len(json.loads(before)["mrs"]), 1)

        reconcile_job = ws.reconcile(request)
        payload, proc = ws.run_job(reconcile_job)

        observed = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(observed["outcome"], "observed")
        self.assertFalse(observed["mr"]["created"])
        self.assertTrue(observed["mr"]["descriptionVerified"])
        self.assertEqual(observed["mr"]["descriptionExpectedSha256"], sha(FIRST))
        self.assertEqual(observed["mr"]["descriptionObservedSha256"], sha(FIRST))
        self.assertEqual(ws.state_path.read_bytes(), before)
        self.assertEqual(len(json.loads(before)["mrs"]), 1)
        readback_proc, readback = ws.readback(*ws.queue_args(reconcile_job))
        self.assertEqual(readback_proc.returncode, 0, (readback_proc.stderr, readback))
        self.assertTrue(readback["publication"]["bound"])
        self.assertTrue(readback["publication"]["descriptionVerified"])
        self.assertEqual(readback["publication"]["descriptionExpectedSha256"], sha(FIRST))
        self.assertEqual(readback["publication"]["descriptionObservedSha256"], sha(FIRST))

    def test_corrupted_remote_description_cannot_reconcile_or_bind(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(
            authorize="import,push,mr,reconcile", description=FIRST,
            token="adadadadadadadad")
        ws.extra_env["DELL_FIXTURE_FAIL_DESCRIPTION_READBACK"] = "1"
        try:
            payload, proc = ws.run_job(ws.submit_publish(request))
        finally:
            ws.extra_env.pop("DELL_FIXTURE_FAIL_DESCRIPTION_READBACK", None)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["outcome"], "unknown")

        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        state["mrs"]["1"]["description"] = "corrupted prefix\n" + FIRST
        ws.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        reconcile_job = ws.reconcile(request)
        payload, proc = ws.run_job(reconcile_job)

        observed = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(observed["outcome"], "refused")
        self.assertEqual(observed["stage"], "reconcile")
        self.assertEqual(observed["error"]["reason"], "description-mismatch")
        self.assertEqual(len(json.loads(ws.state_path.read_text(encoding="utf-8"))["mrs"]), 1)
        readback_proc, readback = ws.readback(*ws.queue_args(reconcile_job))
        self.assertEqual(readback_proc.returncode, 1, readback_proc.stderr)
        self.assertFalse(readback["publication"]["bound"])

    def test_reconcile_refuses_description_replaced_after_signing(self):
        ws = build(FIRST)
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(
            authorize="import,push,mr,reconcile", description=FIRST,
            token="acacacacacacacac")
        ws.extra_env["DELL_FIXTURE_FAIL_DESCRIPTION_READBACK"] = "1"
        try:
            payload, proc = ws.run_job(ws.submit_publish(request))
        finally:
            ws.extra_env.pop("DELL_FIXTURE_FAIL_DESCRIPTION_READBACK", None)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        before = ws.state_path.read_bytes()
        (ws.shared / "mr-description.md").write_bytes(SECOND.encode("utf-8"))

        payload, proc = ws.run_job(ws.reconcile(request))

        observed = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(observed["stage"], "request")
        self.assertEqual(observed["error"]["reason"], "description-digest-mismatch")
        self.assertEqual(ws.state_path.read_bytes(), before)

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
        self.assertNotEqual(stored, SECOND)


if __name__ == "__main__":
    unittest.main()
