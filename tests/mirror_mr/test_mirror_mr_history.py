"""Focused tests for the merged-MR historical route (issue #143).

Disposable fixtures only (see `mirror_fixture.py`): no network, no live
GitLab, GitHub, or Lenovo. Each test names its contract first and asserts
the exact receipt verdict, reason, representation, SHAs, and PR identity.
The historical comparison (paths plus patch bytes) is verified by digest,
never by titles or SHAs alone. Open-route (#142) behavior is preserved:
an open-route job never proves a historical import.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from mirror_fixture import (BASE_BRANCH, FEATURE_BRANCH, MR_IID, MR_URL,
                            Workspace, git, run)

SCRIPTS = TESTS_DIR.parents[1] / "scripts" / "mirror_mr"


def history_request(ws: Workspace, token: str = "0123456789abcdef") -> dict:
    command_file = ws.root / f"history-request-command-{token}.txt"
    proc = run([sys.executable, str(SCRIPTS / "dell_mirror_request.py"),
                "--mr-url", MR_URL, "--mapping-file", str(ws.mapping),
                "--mode", "merged",
                "--token", token, "--shared", str(ws.shared),
                "--lenovo-checkout", str(ws.lenovo),
                "--lenovo-driver-dir", str(SCRIPTS),
                "--lenovo-flow-dir", str(TESTS_DIR),
                "--python", sys.executable,
                "--request-name", f"mirror-request-{token}.json",
                "--command-file", str(command_file)], env=ws.env())
    if proc.returncode != 0:
        raise AssertionError(f"history request failed: {proc.stdout} {proc.stderr}")
    data = json.loads(proc.stdout)
    data["commandFile"] = command_file
    return data


def history_export_job(ws: Workspace, token: str = "0123456789abcdef") -> str:
    data = history_request(ws, token)
    return ws.submit(data["commandFile"],
                     return_file=Path(data["returnFile"]))


def history_import(ws: Workspace, job_id: str,
                   token: str = "0123456789abcdef",
                   *extra: str) -> tuple:
    receipt = ws.root / f"history-receipt-{token}.json"
    state = ws.root / f"history-state-{token}.json"
    proc = run([sys.executable, str(SCRIPTS / "dell_mr_history_import.py"),
                "--queue", str(ws.queue), "--key-file", str(ws.key),
                "--job-id", job_id, "--shared", str(ws.shared),
                "--mirror-dir", str(ws.mirror),
                "--mapping-file", str(ws.mapping),
                "--mr-url", MR_URL, "--state", str(state),
                "--receipt", str(receipt),
                "--fetch-timeout", "3", "--poll", "0.05",
                "--github-dir", str(TESTS_DIR), *extra], env=ws.env())
    data = json.loads(receipt.read_text(encoding="utf-8")) \
        if receipt.is_file() else None
    return proc, data


def mark_merged(ws: Workspace, squash: str | None = None,
                merge_commit: str | None = None) -> None:
    state = json.loads(ws.gitlab_state.read_text(encoding="utf-8"))
    record = state["mrs"][str(MR_IID)]
    record["status"] = "merged"
    record["source_sha"] = ws.feature
    record["target_sha"] = ws.base
    if squash is not None:
        record["squash_sha"] = squash
    if merge_commit is not None:
        record["merge_commit_sha"] = merge_commit
    ws.gitlab_state.write_text(json.dumps(state), encoding="utf-8")


def delete_remote_source(ws: Workspace) -> None:
    git("checkout", "-q", BASE_BRANCH, cwd=ws.lenovo)
    git("push", "-q", "origin", f":{FEATURE_BRANCH}", cwd=ws.lenovo)


def make_squash(ws: Workspace, token: str, different: bool) -> str:
    """A squash commit on top of the historical base; optionally different."""
    git("checkout", "-q", BASE_BRANCH, cwd=ws.lenovo)
    body = "base\nsquash-different\n" if different else "base\nfeature\n"
    (ws.lenovo / "work.txt").write_text(body, encoding="utf-8")
    git("add", "work.txt", cwd=ws.lenovo)
    git("commit", "-qm", "squash", cwd=ws.lenovo)
    squash = git("rev-parse", "--verify", "HEAD", cwd=ws.lenovo)
    git("push", "-q", "origin", f"{squash}:refs/heads/squash-holder-{token}",
        cwd=ws.lenovo)
    return squash


class MergedHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace().build()

    def tearDown(self) -> None:
        self.ws.teardown()

    def export(self, token: str = "0123456789abcdef") -> str:
        job_id = history_export_job(self.ws, token)
        _, proc = self.ws.run_job(job_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return job_id

    def test_merged_deleted_source_mirrors_exact_and_rerun_reconciles(self) -> None:
        """A merged MR with a deleted source branch yields one review PR on
        uniquely named refs; a rerun adopts it."""

        delete_remote_source(self.ws)
        remote = git("ls-remote", "--heads", str(self.ws.gitlab_origin))
        self.assertNotIn(FEATURE_BRANCH, remote)
        mark_merged(self.ws)
        job_id = self.export()
        proc, receipt = history_import(self.ws, job_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(receipt["verdict"], "mirrored")
        self.assertEqual(receipt["representation"], "original-head")
        self.assertEqual(receipt["historicalBase"], self.ws.base)
        self.assertEqual(receipt["historicalHead"], self.ws.feature)
        self.assertEqual(receipt["comparison"], "exact")
        base_ref = f"mirror-mr/{MR_IID}/base-{self.ws.base[:7]}"
        head_ref = f"mirror-mr/{MR_IID}/head-{self.ws.feature[:7]}"
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        self.assertEqual(len(state["prs"]), 1)
        record = next(iter(state["prs"].values()))
        self.assertEqual(record["headRefName"], head_ref)
        self.assertEqual(record["baseRefName"], base_ref)
        self.assertEqual(record["headRefOid"], self.ws.feature)
        self.assertEqual(record["baseRefOid"], self.ws.base)
        self.assertIn("merged", record["title"].lower())
        self.assertIn(MR_URL, record["body"])
        self.assertIn("original-head", record["body"])
        self.assertIn("review", record["title"].lower() + record["body"].lower())
        remote_heads = git("ls-remote", "--heads", str(self.ws.github_origin))
        self.assertIn(base_ref, remote_heads)
        self.assertIn(head_ref, remote_heads)
        self.assertNotIn(f"refs/heads/{BASE_BRANCH}\t", remote_heads)

        proc2, receipt2 = history_import(self.ws, job_id)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(receipt2["verdict"], "reconciled")
        self.assertEqual(receipt2["prUrl"], receipt["prUrl"])
        state2 = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        self.assertEqual(len(state2["prs"]), 1)

    def test_squash_with_differing_diff_still_uses_original_head(self) -> None:
        """A squash whose patch differs from the original never replaces it:
        the proven original head is published and verified by digest."""

        squash = make_squash(self.ws, "a1b2c3d4e5f60718", different=True)
        delete_remote_source(self.ws)
        mark_merged(self.ws, squash=squash)
        job_id = self.export(token="a1b2c3d4e5f60718")
        proc, receipt = history_import(self.ws, job_id,
                                       token="a1b2c3d4e5f60718")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(receipt["verdict"], "mirrored")
        self.assertEqual(receipt["representation"], "original-head")
        self.assertEqual(receipt["historicalHead"], self.ws.feature)
        self.assertEqual(receipt["comparison"], "exact")
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        record = next(iter(state["prs"].values()))
        self.assertEqual(record["headRefOid"], self.ws.feature)
        self.assertIn("original-head", record["body"])
        head_ref = f"mirror-mr/{MR_IID}/head-{self.ws.feature[:7]}"
        self.assertEqual(record["headRefName"], head_ref)

    def test_missing_historical_objects_refuse_without_write(self) -> None:
        """A head SHA with no retrievable object refuses as unavailable."""

        mark_merged(self.ws)
        state = json.loads(self.ws.gitlab_state.read_text(encoding="utf-8"))
        state["mrs"][str(MR_IID)]["source_sha"] = "1" * 40
        self.ws.gitlab_state.write_text(json.dumps(state), encoding="utf-8")
        job_id = history_export_job(self.ws)
        _, proc = self.ws.run_job(job_id)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("history-unavailable", proc.stdout)

    def test_conflicting_history_ref_refuses_without_duplicate(self) -> None:
        """A history ref already pointing elsewhere stops the run."""

        mark_merged(self.ws)
        job_id = self.export()
        base_ref = f"mirror-mr/{MR_IID}/base-{self.ws.base[:7]}"
        head_ref = f"mirror-mr/{MR_IID}/head-{self.ws.feature[:7]}"
        bundle = next(self.ws.shared.glob("mirror-mr-*.bundle"))
        git("fetch", "-q", str(bundle),
            f"{self.ws.base}:refs/heads/{base_ref}",
            f"{self.ws.feature}:refs/heads/{head_ref}", cwd=self.ws.mirror)
        other = git("commit-tree", git("mktree", cwd=self.ws.mirror, input_text=""),
                    "-m", "other", cwd=self.ws.mirror)
        git("update-ref", f"refs/heads/{head_ref}", other, cwd=self.ws.mirror)
        git("push", "-q", "origin", base_ref, head_ref, cwd=self.ws.mirror)
        proc, receipt = history_import(self.ws, job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "history-ref-conflict")
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        self.assertEqual(len(state["prs"]), 0)

    def test_public_destination_refuses_before_publication(self) -> None:
        """A non-private mirror stops before any history ref or PR write."""

        mark_merged(self.ws)
        job_id = self.export()
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        state["visibility"] = "public"
        self.ws.github_state.write_text(json.dumps(state), encoding="utf-8")
        proc, receipt = history_import(self.ws, job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "destination-not-private")
        self.assertIsNone(receipt["prUrl"])
        remote = git("ls-remote", "--heads", str(self.ws.github_origin))
        self.assertEqual(remote, "")

    def test_open_route_job_never_proves_historical_import(self) -> None:
        """A sealed open-route export command is refused by the history path."""

        job_id = self.ws.export_job()
        self.ws.run_job(job_id)
        proc, receipt = history_import(self.ws, job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "queue-job-mismatch")
        self.assertIsNone(receipt["prUrl"])

    def test_unknown_job_resumes_same_id(self) -> None:
        """No result yet is unknown (exit 3); the same id completes later."""

        mark_merged(self.ws)
        job_id = history_export_job(self.ws)
        proc, _ = history_import(self.ws, job_id)
        self.assertEqual(proc.returncode, 3)
        _, run_proc = self.ws.run_job(job_id)
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
        proc3, receipt3 = history_import(self.ws, job_id)
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        self.assertEqual(receipt3["verdict"], "mirrored")

    def test_interrupted_pr_write_reconciles(self) -> None:
        """A lost PR write is adopted on resume, never duplicated."""

        mark_merged(self.ws)
        job_id = self.export()
        self.ws.extra_env["MIRROR_FIXTURE_FAIL_AFTER_WRITE"] = "1"
        proc, receipt = history_import(self.ws, job_id)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(receipt["reason"], "pr-write-unknown")
        del self.ws.extra_env["MIRROR_FIXTURE_FAIL_AFTER_WRITE"]
        proc2, receipt2 = history_import(self.ws, job_id)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(receipt2["verdict"], "reconciled")
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        self.assertEqual(len(state["prs"]), 1)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
