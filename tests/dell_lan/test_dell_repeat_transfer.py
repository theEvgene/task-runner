"""Repeat transfer: a subsequent descendant commit on the same task branch.

First import creates the branch; a repeat names the explicit prior SHA, the
owned worktree must be registered there and clean, and the new commit must
descend from it. Mismatch, dirty, and non-descendant cases fail closed
without moving the branch. Uses the real Lenovo publish driver and the local
bare remote only; no LAN, no Lenovo host, no corporate service.
"""

from __future__ import annotations

import json
import unittest

from dell_fixture import TASK_BRANCH, Workspace, git, run


def build_published(token: str = "aaaaaaaaaaaaaaaa") -> Workspace:
    ws = Workspace().build()
    job = ws.request_base("bbbbbbbbbbbbbbbb")
    ws.run_job(job)
    proc, _ = ws.import_base(job)
    if proc.returncode != 0:
        raise AssertionError(f"base import failed: {proc.stdout} {proc.stderr}")
    ws.cut_worktree()
    ws.stage_bundle()
    request = ws.publish_request(token=token)
    job = ws.submit_publish(request)
    _, proc = ws.run_job(job)
    if proc.returncode != 0:
        raise AssertionError(f"first publish failed: {proc.stdout} {proc.stderr}")
    ws.first_job = job
    ws.first_task = ws.task
    return ws


class RepeatTransferTests(unittest.TestCase):
    def test_second_descendant_commit_moves_branch_and_updates_mr(self):
        ws = build_published("a1a1a1a1a1a1a1a1")
        self.addCleanup(ws.cleanup)
        previous, new = ws.commit_second_task()
        self.assertEqual(previous, ws.first_task)
        ws.stage_bundle()
        request = ws.publish_request(token="b2b2b2b2b2b2b2b2", previous=previous)
        job = ws.submit_publish(request)
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(driver["outcome"], "ok")
        self.assertEqual(driver["import"]["previous"], previous)
        self.assertTrue(driver["import"]["updated"])
        self.assertTrue(driver["import"]["filesSynced"])
        self.assertFalse(driver["import"]["recovered"])
        self.assertEqual(driver["import"]["commit"], new)
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=ws.lenovo), new)
        self.assertEqual(git("rev-parse", "--verify", "HEAD", cwd=ws.lenovo_worktree), new)
        self.assertEqual(
            (ws.lenovo_worktree / "work.txt").read_text(encoding="utf-8"),
            "published base\ntask work\ntask work iteration 2\n",
            "the owned worktree files must follow the atomic ref swap")
        self.assertEqual(git("status", "--porcelain", cwd=ws.lenovo_worktree), "")
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=ws.origin), new)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1, "a repeat must update the MR, not open a second")
        self.assertFalse(driver["mr"]["created"])
        self.assertEqual(driver["identity"]["head_sha"], new)
        proc, data = ws.readback(*ws.queue_args(job))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["publication"]["headSha"], new)

    def test_repeat_without_previous_is_refused_and_branch_stays(self):
        ws = build_published("c3c3c3c3c3c3c3c3")
        self.addCleanup(ws.cleanup)
        previous, new = ws.commit_second_task()
        ws.stage_bundle()
        request = ws.publish_request(token="d4d4d4d4d4d4d4d4")
        job = ws.submit_publish(request)
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(driver["outcome"], "refused")
        self.assertEqual(driver["error"]["reason"], "branch-owned")
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=ws.lenovo), previous)

    def test_wrong_previous_sha_is_refused(self):
        ws = build_published("e5e5e5e5e5e5e5e5")
        self.addCleanup(ws.cleanup)
        previous, new = ws.commit_second_task()
        ws.stage_bundle()
        request = ws.publish_request(token="f6f6f6f6f6f6f6f6", previous="0" * 40)
        job = ws.submit_publish(request)
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(driver["error"]["reason"], "previous-mismatch")
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=ws.lenovo), previous)

    def test_dirty_lenovo_worktree_refuses_repeat(self):
        ws = build_published("1111222233334444")
        self.addCleanup(ws.cleanup)
        previous, new = ws.commit_second_task()
        ws.stage_bundle()
        (ws.lenovo_worktree / "work.txt").write_text("local dirt\n", encoding="utf-8")
        request = ws.publish_request(token="5555666677778888", previous=previous)
        job = ws.submit_publish(request)
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(driver["error"]["reason"], "worktree-dirty")
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=ws.lenovo), previous)

    def test_non_descendant_commit_is_refused(self):
        ws = build_published("9999aaaabbbbcccc")
        self.addCleanup(ws.cleanup)
        previous = ws.first_task
        git("checkout", "-q", "-b", "task/diverged", ws.base, cwd=ws.worktree)
        (ws.worktree / "work.txt").write_text("diverged\n", encoding="utf-8")
        git("add", "work.txt", cwd=ws.worktree)
        git("commit", "-qm", "diverged", cwd=ws.worktree)
        ws.task = git("rev-parse", "--verify", "HEAD", cwd=ws.worktree)
        ws.stage_bundle()
        request = ws.publish_request(token="ddddeeeeffff0000", previous=previous)
        job = ws.submit_publish(request)
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(driver["error"]["reason"], "non-descendant")
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=ws.lenovo), previous)

    def test_unknown_push_classified_unknown_not_rejected(self):
        ws = Workspace().build()
        self.addCleanup(ws.cleanup)
        job = ws.request_base("1234123412341234")
        ws.run_job(job)
        proc, _ = ws.import_base(job)
        self.assertEqual(proc.returncode, 0)
        ws.cut_worktree()
        ws.stage_bundle()
        hook = ws.lenovo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        request = ws.publish_request(token="abcdabcdabcdabcd")
        job = ws.submit_publish(request)
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(driver["outcome"], "unknown")
        self.assertEqual(driver["stage"], "push")
        self.assertEqual(driver["error"]["reason"], "push-unknown")
        proc, data = ws.readback(*ws.queue_args(job))
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(data["verdict"], "unknown")
        self.assertEqual(data["reason"], "outcome-unknown")
        self.assertNotIn("new job id", data["nextAction"])


if __name__ == "__main__":
    unittest.main()
