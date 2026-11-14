"""Standalone transfer: one branch import, never a push or an MR (issues #42).

Uses the real queue, request writer, transfer driver and Git; only the
remote is the local bare fixture. Proves the returned path/branch/SHA,
the absence of any publication side effect, and the repeat via the
explicit prior SHA reusing the existing compare-and-swap guard.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from dell_fixture import TASK_BRANCH, Workspace, git

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "dell_lan"
REQUEST_CLI = SCRIPTS / "dell_request.py"


def transfer_request(ws: Workspace, token: str, authorize: str = "import",
                     previous: str | None = None, name: str | None = None) -> dict:
    command_file = ws.root / f"transfer-command-{token}.txt"
    command = [
        sys.executable, str(REQUEST_CLI), "transfer", "--shared", str(ws.shared),
        "--token", token, "--lenovo-skill-dir", str(ROOT),
        "--lenovo-driver-dir", str(SCRIPTS), "--python", sys.executable,
        "--manifest", str(ws.manifest_file), "--bundle", str(ws.bundle_file),
        "--expected-base", ws.base, "--expected-commit", ws.task,
        "--branch", TASK_BRANCH, "--authorize", authorize,
        "--lenovo-checkout", str(ws.lenovo), "--lenovo-worktree", str(ws.lenovo_worktree),
        "--request-name", name or f"transfer-request-{token}.json",
        "--command-file", str(command_file), "--repo", str(ws.dell),
    ]
    if previous:
        command += ["--expected-previous", previous]
    from dell_fixture import run

    proc = run(command, env=ws.env())
    if proc.returncode != 0:
        raise AssertionError(f"transfer request failed: {proc.stdout} {proc.stderr}")
    data = json.loads(proc.stdout)
    data["commandFile"] = command_file
    return data


def build_staged() -> Workspace:
    ws = Workspace().build()
    job = ws.request_base("2222222222222222")
    ws.run_job(job)
    proc, _ = ws.import_base(job)
    if proc.returncode != 0:
        raise AssertionError(f"base import failed: {proc.stdout} {proc.stderr}")
    ws.cut_worktree()
    ws.stage_bundle()
    return ws


class TransferOnlyTests(unittest.TestCase):
    def test_transfer_request_contains_no_publication_metadata(self):
        ws = build_staged()
        self.addCleanup(ws.cleanup)
        request = transfer_request(ws, "1010101010101010")
        payload = json.loads(Path(request["requestPath"]).read_text(encoding="utf-8"))
        for field in ("host", "project", "targetBranch", "changeType", "jiraKey",
                      "subject", "descriptionFile", "state"):
            self.assertNotIn(field, payload)

    def test_transfer_returns_path_branch_and_verified_sha(self):
        ws = build_staged()
        self.addCleanup(ws.cleanup)
        request = transfer_request(ws, "1111111111111111")
        job = ws.submit(request["commandFile"])
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(driver["schema"], "dell-lan-transfer-result/1")
        self.assertEqual(driver["outcome"], "ok")
        self.assertEqual(driver["stage"], "import")
        self.assertEqual(driver["import"]["branch"], TASK_BRANCH)
        self.assertEqual(driver["import"]["commit"], ws.task)
        self.assertEqual(driver["import"]["worktree"], str(ws.lenovo_worktree))
        self.assertEqual(
            git("rev-parse", "--verify", "HEAD", cwd=ws.lenovo_worktree), ws.task)

    def test_signed_queue_readback_binds_distinct_dell_and_lenovo_worktrees(self):
        ws = build_staged()
        self.addCleanup(ws.cleanup)
        self.assertNotEqual(ws.worktree.resolve(), ws.lenovo_worktree.resolve())
        request = transfer_request(ws, "abababababababab")
        job = ws.submit(request["commandFile"])
        _, driver = ws.run_job(job)
        self.assertEqual(driver.returncode, 0, driver.stderr)

        proc, receipt = ws.readback(
            *ws.queue_args(job),
            "--source-branch", TASK_BRANCH,
            "--lenovo-worktree", str(ws.lenovo_worktree),
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(receipt["verdict"], "transferred")
        self.assertTrue(receipt["worktree"]["registered"])
        self.assertEqual(receipt["worktree"]["head"], ws.task)
        self.assertEqual(receipt["transfer"]["worktree"], str(ws.lenovo_worktree))
        self.assertTrue(receipt["transfer"]["bound"])

    def test_transfer_performs_no_push_and_no_mr(self):
        ws = build_staged()
        self.addCleanup(ws.cleanup)
        request = transfer_request(ws, "3333333333333333")
        job = ws.submit(request["commandFile"])
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(driver["push"])
        self.assertIsNone(driver["mr"])
        from dell_fixture import run as _run

        probe = _run(["git", "--git-dir", str(ws.origin), "rev-parse", "--verify",
                      f"refs/heads/{TASK_BRANCH}"])
        self.assertNotEqual(probe.returncode, 0, "transfer must not push the branch")
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {}, "transfer must not create an MR")

    def test_repeat_transfer_with_previous_sha_moves_branch(self):
        ws = build_staged()
        self.addCleanup(ws.cleanup)
        first = transfer_request(ws, "4444444444444444")
        job = ws.submit(first["commandFile"])
        _, proc = ws.run_job(job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        previous, _ = ws.commit_second_task()
        ws.stage_bundle()
        second = transfer_request(ws, "5555555555555555", previous=previous)
        job2 = ws.submit(second["commandFile"])
        _, proc2 = ws.run_job(job2)
        driver = json.loads(proc2.stdout)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(driver["outcome"], "ok")
        self.assertEqual(driver["import"]["previous"], previous)
        self.assertTrue(driver["import"]["updated"])
        self.assertEqual(
            git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=ws.lenovo), ws.task)


if __name__ == "__main__":
    unittest.main()
