"""Focused tests for the open-MR mirror route (issue #142).

Disposable fixtures only (see `mirror_fixture.py`): no network, no live
GitLab, GitHub, or Lenovo. Each test names its contract first and asserts
the exact receipt verdict, reason, SHAs, and PR identity.
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


class OpenMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace().build()

    def tearDown(self) -> None:
        self.ws.teardown()

    # -- helpers ------------------------------------------------------
    def export(self, token: str = "0123456789abcdef") -> str:
        job_id = self.ws.export_job(token)
        _, proc = self.ws.run_job(job_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return job_id

    def import_mr(self, job_id: str, token: str = "0123456789abcdef"):
        return self.ws.import_mr(job_id, token)

    # -- happy path ----------------------------------------------------
    def test_open_mr_mirrors_exact_change_and_rerun_reconciles(self) -> None:
        """An open MR yields one private-mirror PR with the observed SHAs;
        a second run adopts it instead of duplicating."""

        job_id = self.export()
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(receipt["verdict"], "mirrored")
        self.assertEqual(receipt["sourceSha"], self.ws.feature)
        self.assertEqual(receipt["targetSha"], self.ws.base)
        self.assertEqual(receipt["comparison"], "exact")
        pr_url = receipt["prUrl"]
        self.assertTrue(pr_url.startswith("https://"), pr_url)

        state = json.loads((self.ws.root / "github-state.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(len(state["prs"]), 1)
        record = next(iter(state["prs"].values()))
        self.assertEqual(record["headRefOid"], self.ws.feature)
        self.assertEqual(record["baseRefOid"], self.ws.base)
        self.assertIn(MR_URL, record["body"])

        proc2, receipt2 = self.import_mr(job_id)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(receipt2["verdict"], "reconciled")
        self.assertEqual(receipt2["prUrl"], pr_url)
        state2 = json.loads((self.ws.root / "github-state.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(len(state2["prs"]), 1)

    # -- request validation ---------------------------------------------
    def test_credential_bearing_url_refused_before_any_job(self) -> None:
        """A URL with userinfo refuses at request time; no job exists."""

        proc = run([sys.executable, str(SCRIPTS / "dell_mirror_request.py"),
                    "--mr-url", f"https://user:pass@host/group/project/-/"
                                f"merge_requests/{MR_IID}",
                    "--mapping-file", str(self.ws.mapping),
                    "--token", "0123456789abcdef",
                    "--shared", str(self.ws.shared),
                    "--lenovo-checkout", str(self.ws.lenovo),
                    "--python", sys.executable,
                    "--request-name", "r.json",
                    "--command-file", str(self.ws.root / "cmd.txt")],
                   env=self.ws.env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("credentials", proc.stderr)

    def test_unmapped_project_refused_before_any_job(self) -> None:
        """An MR outside the mapping refuses with an actionable reason."""

        proc = run([sys.executable, str(SCRIPTS / "dell_mirror_request.py"),
                    "--mr-url", f"https://other.invalid/a/b/-/merge_requests/3",
                    "--mapping-file", str(self.ws.mapping),
                    "--token", "0123456789abcdef",
                    "--shared", str(self.ws.shared),
                    "--lenovo-checkout", str(self.ws.lenovo),
                    "--python", sys.executable,
                    "--request-name", "r.json",
                    "--command-file", str(self.ws.root / "cmd.txt")],
                   env=self.ws.env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no mirror configured", proc.stderr)

    def test_malformed_url_refused_before_any_job(self) -> None:
        proc = run([sys.executable, str(SCRIPTS / "dell_mirror_request.py"),
                    "--mr-url", "https://host/only-two-segments",
                    "--mapping-file", str(self.ws.mapping),
                    "--token", "0123456789abcdef",
                    "--shared", str(self.ws.shared),
                    "--lenovo-checkout", str(self.ws.lenovo),
                    "--python", sys.executable,
                    "--request-name", "r.json",
                    "--command-file", str(self.ws.root / "cmd.txt")],
                   env=self.ws.env())
        self.assertNotEqual(proc.returncode, 0)

    # -- Lenovo observation -----------------------------------------------
    def test_closed_mr_refuses_without_export(self) -> None:
        """A non-open MR stops on Lenovo; no bundle is staged."""

        state = json.loads(self.ws.gitlab_state.read_text(encoding="utf-8"))
        state["mrs"][str(MR_IID)]["status"] = "merged"
        self.ws.gitlab_state.write_text(json.dumps(state), encoding="utf-8")
        job_id = self.ws.export_job()
        _, proc = self.ws.run_job(job_id)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("mr-not-open", proc.stdout)

    def test_moved_mr_refuses_without_snapshot(self) -> None:
        """Metadata that disagrees with the remote refuses as moved."""

        state = json.loads(self.ws.gitlab_state.read_text(encoding="utf-8"))
        state["mrs"][str(MR_IID)]["source_sha"] = "d" * 40
        self.ws.gitlab_state.write_text(json.dumps(state), encoding="utf-8")
        job_id = self.ws.export_job()
        _, proc = self.ws.run_job(job_id)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("mr-moved", proc.stdout)

    def test_missing_branch_refuses(self) -> None:
        git("push", "-q", "origin", f":{FEATURE_BRANCH}", cwd=self.ws.lenovo)
        job_id = self.ws.export_job()
        _, proc = self.ws.run_job(job_id)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("branch-missing", proc.stdout)

    # -- transfer integrity --------------------------------------------------
    def test_swapped_bundle_refuses_before_any_write(self) -> None:
        """Bytes that differ from the sealed digest are never imported."""

        job_id = self.export()
        bundle = next(self.ws.shared.glob("mirror-mr-*.bundle"))
        bundle.write_bytes(b"swapped bytes, not a bundle")
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "artifact-mismatch")
        self.assertIsNone(receipt["prUrl"])
        remote = git("ls-remote", "--heads", str(self.ws.github_origin))
        self.assertEqual(remote, "")

    # -- destination and ref safety -------------------------------------------
    def test_public_destination_refuses_before_publication(self) -> None:
        """A mirror that is not private stops before any ref or PR write."""

        job_id = self.export()
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        state["visibility"] = "public"
        self.ws.github_state.write_text(json.dumps(state), encoding="utf-8")
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "destination-not-private")
        remote = git("ls-remote", "--heads", str(self.ws.github_origin))
        self.assertEqual(remote, "")

    def test_non_fast_forward_target_refuses(self) -> None:
        """A target ref the observed SHA does not descend from is never moved."""

        job_id = self.export()
        other = git("commit-tree", git("mktree", cwd=self.ws.mirror,
                                       input_text=""),
                    "-m", "divergent", cwd=self.ws.mirror)
        git("update-ref", f"refs/heads/{BASE_BRANCH}", other,
            cwd=self.ws.mirror)
        git("push", "-q", "origin", BASE_BRANCH, cwd=self.ws.mirror)
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "target-not-fast-forward")
        head = git("rev-parse", "--verify", f"refs/heads/{BASE_BRANCH}",
                   cwd=self.ws.mirror)
        self.assertEqual(head, other)

    def test_conflicting_source_ref_refuses(self) -> None:
        """An existing source ref at another SHA is never overwritten."""

        job_id = self.export()
        bundle = next(self.ws.shared.glob("mirror-mr-*.bundle"))
        git("fetch", "-q", str(bundle),
            f"{self.ws.base}:refs/heads/{BASE_BRANCH}", cwd=self.ws.mirror)
        other = git("commit-tree", git("mktree", cwd=self.ws.mirror,
                                       input_text=""),
                    "-m", "divergent", cwd=self.ws.mirror)
        git("update-ref", f"refs/heads/{FEATURE_BRANCH}", other,
            cwd=self.ws.mirror)
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "source-ref-conflict")

    def test_conflicting_pr_stops_without_duplicate(self) -> None:
        """A same-name PR pointing at other SHAs stops the run."""

        job_id = self.export()
        bundle = next(self.ws.shared.glob("mirror-mr-*.bundle"))
        git("fetch", "-q", str(bundle),
            f"{self.ws.base}:refs/heads/{BASE_BRANCH}",
            f"{self.ws.feature}:refs/heads/{FEATURE_BRANCH}",
            cwd=self.ws.mirror)
        git("push", "-q", "origin", BASE_BRANCH, FEATURE_BRANCH,
            cwd=self.ws.mirror)
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        state["prs"]["1"] = {
            "url": f"https://github.fixture.invalid/x/pull/1", "number": 1,
            "state": "open", "title": "other", "body": "",
            "headRefName": FEATURE_BRANCH, "baseRefName": BASE_BRANCH,
            "headRefOid": "e" * 40, "baseRefOid": self.ws.base}
        state["nextNumber"] = 2
        self.ws.github_state.write_text(json.dumps(state), encoding="utf-8")
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "pr-conflict")
        state2 = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        self.assertEqual(len(state2["prs"]), 1)

    def test_pr_base_oid_mismatch_refuses(self) -> None:
        """A PR whose base OID is not the observed target SHA is not exact."""

        job_id = self.export()
        bundle = next(self.ws.shared.glob("mirror-mr-*.bundle"))
        git("fetch", "-q", str(bundle),
            f"{self.ws.base}:refs/heads/{BASE_BRANCH}",
            f"{self.ws.feature}:refs/heads/{FEATURE_BRANCH}",
            cwd=self.ws.mirror)
        git("push", "-q", "origin", BASE_BRANCH, FEATURE_BRANCH,
            cwd=self.ws.mirror)
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        state["prs"]["1"] = {
            "url": "https://github.fixture.invalid/x/pull/1", "number": 1,
            "state": "open", "title": "mirror", "body": "mirror",
            "headRefName": FEATURE_BRANCH, "baseRefName": BASE_BRANCH,
            "headRefOid": self.ws.feature, "baseRefOid": "f" * 40}
        state["nextNumber"] = 2
        self.ws.github_state.write_text(json.dumps(state), encoding="utf-8")
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "pr-conflict")
        self.assertIn("base", receipt["detail"])
        state2 = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        self.assertEqual(len(state2["prs"]), 1)

    def test_spoofed_job_command_refuses(self) -> None:
        """A substring marker in another command never proves this import."""

        command_file = self.ws.root / "spoof-command.txt"
        command_file.write_text("echo lenovo_mr_export.py\n", encoding="utf-8")
        job_id = self.ws.submit(command_file)
        self.ws.run_job(job_id)
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "queue-job-mismatch")
        self.assertIsNone(receipt["prUrl"])
        remote = git("ls-remote", "--heads", str(self.ws.github_origin))
        self.assertEqual(remote, "")

    def test_token_mismatched_artifacts_refuse(self) -> None:
        """Sealed names that ignore the job's request token prove nothing."""

        import sys as _sys
        bridge_root = TESTS_DIR.parents[1] / "scripts" / "lan_bridge"
        if str(bridge_root) not in _sys.path:
            _sys.path.insert(0, str(bridge_root))
        import lan_bridge as _bridge
        from datetime import datetime, timezone as _tz

        token = "0123456789abcdef"
        data = self.ws.request(token)
        job_id = self.ws.submit(data["commandFile"],
                                return_file=Path(data["returnFile"]))
        forged = {
            "schema": "mirror-mr-export/1", "token": token,
            "status": "opened", "title": "t", "description": "",
            "sourceBranch": FEATURE_BRANCH, "targetBranch": BASE_BRANCH,
            "sourceSha": self.ws.feature, "targetSha": self.ws.base,
            "bundleName": "wrong.bundle",
            "manifestName": f"mirror-mr-{token}.manifest.json",
            "sha256": "a" * 64, "bytes": 10,
            "manifestDigest": "b" * 64,
        }
        payload = _bridge.build_result_payload(
            job_id, "ok", 0, stdout=json.dumps(forged),
            stderr="", started_at=datetime.now(_tz.utc).isoformat(),
            finished_at=datetime.now(_tz.utc).isoformat(),
            output_truncated=False, files={})
        _bridge.publish(str(self.ws.queue / "results"), job_id,
                        _bridge.seal(payload, self.ws.key_bytes))
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "request-malformed")
        self.assertIsNone(receipt["prUrl"])

    def test_checked_out_branch_refuses_before_move(self) -> None:
        """A clean checkout of the target branch is never moved underneath."""

        job_id = self.export()
        other = git("commit-tree", git("mktree", cwd=self.ws.mirror,
                                       input_text=""),
                    "-m", "divergent", cwd=self.ws.mirror)
        git("update-ref", f"refs/heads/{BASE_BRANCH}", other,
            cwd=self.ws.mirror)
        git("checkout", "-q", BASE_BRANCH, cwd=self.ws.mirror)
        try:
            proc, receipt = self.import_mr(job_id)
        finally:
            git("checkout", "-q", "--detach", "HEAD", cwd=self.ws.mirror)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(receipt["reason"], "worktree-checked-out")
        head = git("rev-parse", "--verify", f"refs/heads/{BASE_BRANCH}",
                   cwd=self.ws.mirror)
        self.assertEqual(head, other)

    # -- interruption and unknown outcomes --------------------------------------
    def test_unknown_job_resumes_same_id(self) -> None:
        """No result yet is unknown (exit 3); the same id completes later."""

        job_id = self.ws.export_job()
        proc, _ = self.ws.import_mr(job_id)
        self.assertEqual(proc.returncode, 3)
        _, proc2 = self.ws.run_job(job_id)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        proc3, receipt3 = self.import_mr(job_id)
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        self.assertEqual(receipt3["verdict"], "mirrored")

    def test_interrupted_pr_write_reconciles(self) -> None:
        """A lost PR write is adopted on resume, never duplicated."""

        job_id = self.export()
        self.ws.extra_env["MIRROR_FIXTURE_FAIL_AFTER_WRITE"] = "1"
        proc, receipt = self.import_mr(job_id)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(receipt["reason"], "pr-write-unknown")
        del self.ws.extra_env["MIRROR_FIXTURE_FAIL_AFTER_WRITE"]
        proc2, receipt2 = self.import_mr(job_id)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(receipt2["verdict"], "reconciled")
        state = json.loads(self.ws.github_state.read_text(encoding="utf-8"))
        self.assertEqual(len(state["prs"]), 1)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
