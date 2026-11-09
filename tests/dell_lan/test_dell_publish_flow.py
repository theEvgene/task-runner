"""Complete two-direction fixture: Lenovo base -> Dell work -> Lenovo publish.

The queue, the request writer, both Lenovo drivers, the Dell importer, plain
``git bundle`` staging with a ``git-bundle-manifest/1`` manifest, the fixture
publication flow doubles, and Git itself all run for real. Only the external
remote is simulated: it is a local bare repository plus a JSON state file.
Nothing here proves a real Lenovo run or a real remote publication, and the
readback verdict stays ``simulated``.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dell_fixture import (
    BASE_BRANCH, PROJECT, TASK_BRANCH, Workspace, git, run,
)

import sys

REQUEST_CLI = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan" / "dell_request.py"


def build_to_publish(token: str = "1111111111111111") -> Workspace:
    """Build both machines, import the base, cut the task worktree, stage the bundle."""

    ws = Workspace().build()
    job = ws.request_base("2222222222222222")
    ws.run_job(job)
    proc, data = ws.import_base(job)
    if proc.returncode != 0:
        raise AssertionError(f"base import failed: {proc.stdout} {proc.stderr}")
    ws.cut_worktree()
    ws.stage_bundle()
    return ws


class TwoDirectionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = build_to_publish()
        cls.request = cls.ws.publish_request(token="1111111111111111")
        cls.job = cls.ws.submit_publish(cls.request)
        cls.payload, cls.proc = cls.ws.run_job(cls.job)
        cls.driver = json.loads(cls.proc.stdout) if cls.proc.returncode == 0 else {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def test_base_copy_was_imported_once(self):
        self.assertEqual(git("rev-parse", "--verify", "HEAD", cwd=self.ws.dell), self.ws.base)
        self.assertNotEqual(self.ws.task, self.ws.base)

    def test_lenovo_published_the_task_commit_and_a_draft_mr(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertEqual(self.driver["schema"], "dell-lan-publish-result/1")
        self.assertEqual(self.driver["outcome"], "ok")
        self.assertEqual(self.driver["authorized"], ["import", "push", "mr"])
        self.assertTrue(self.driver["bundle"]["verified"])
        self.assertEqual(self.driver["import"]["commit"], self.ws.task)
        self.assertEqual(self.driver["push"]["observedRemoteSha"], self.ws.task)
        self.assertTrue(self.driver["mr"]["created"])
        self.assertTrue(self.driver["mr"]["draft"])
        identity = self.driver["identity"]
        self.assertEqual(identity["schema_version"], 1)
        self.assertEqual(identity["head_sha"], self.ws.task)
        self.assertEqual(identity["local"]["source_branch_head"], self.ws.task)
        self.assertTrue(identity["local"]["head_matches_receipt"])
        self.assertEqual(identity["project_path"], PROJECT)
        self.assertEqual(identity["source_branch"], TASK_BRANCH)
        self.assertEqual(identity["target_branch"], BASE_BRANCH)

    def test_the_simulated_remote_branch_carries_the_task_commit(self):
        head = git("rev-parse", "--verify", f"refs/heads/{TASK_BRANCH}", cwd=self.ws.origin)
        self.assertEqual(head, self.ws.task)

    def test_dell_readback_binds_sha_and_mr_but_stays_simulated(self):
        proc, data = self.ws.readback(*self.ws.queue_args(self.job))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "simulated")
        self.assertEqual(data["provenance"], "fixture")
        self.assertTrue(data["queue"]["jobVerified"])
        self.assertTrue(data["queue"]["resultVerified"])
        publication = data["publication"]
        self.assertTrue(publication["bound"])
        self.assertTrue(publication["simulated"])
        self.assertEqual(publication["headSha"], self.ws.task)
        self.assertEqual(publication["pushObservedSha"], self.ws.task)
        self.assertTrue(publication["mrCreated"])
        self.assertTrue(publication["mrUrl"].endswith("/merge_requests/1"))
        self.assertEqual(publication["driverStage"], "readback")
        self.assertEqual(publication["driver"],
                         "scripts/dell_lan/lenovo_publish.py")

    def test_consent_is_required_per_action(self):
        ws = build_to_publish("3333333333333333")
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(authorize="import", token="3333333333333333")
        job = ws.submit_publish(request)
        payload, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(driver["outcome"], "refused")
        self.assertEqual(driver["stage"], "push")
        self.assertIn("consent-missing", driver["error"]["reason"])
        self.assertIn("push", driver["error"]["detail"])
        proc = run(["git", "-C", str(ws.origin), "rev-parse", "--verify",
                    f"refs/heads/{TASK_BRANCH}"])
        self.assertNotEqual(proc.returncode, 0)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {})

    def test_unknown_mr_write_is_reconciled_before_any_retry(self):
        ws = build_to_publish("4444444444444444")
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(authorize="import,push,mr,reconcile",
                                     token="4444444444444444")
        ws.extra_env["DELL_FIXTURE_FAIL_AFTER_WRITE"] = "1"
        job = ws.submit_publish(request)
        payload, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(driver["outcome"], "unknown")
        self.assertEqual(driver["stage"], "mr")
        self.assertEqual(driver["error"]["reason"], "mr-write-unknown")
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1, "the simulated write landed before the lost response")

        proc, data = ws.readback(*ws.queue_args(job))
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(data["verdict"], "unknown")
        self.assertEqual(data["reason"], "outcome-unknown")
        self.assertIn("keep the slot", data["nextAction"])
        self.assertNotIn("new job id", data["nextAction"])

        ws.extra_env.pop("DELL_FIXTURE_FAIL_AFTER_WRITE")
        reconcile_job = ws.reconcile(request)
        payload, proc = ws.run_job(reconcile_job)
        observed = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(observed["outcome"], "observed")
        self.assertFalse(observed["mr"]["created"])
        self.assertEqual(observed["push"]["observedRemoteSha"], ws.task)
        self.assertEqual(observed["identity"]["head_sha"], ws.task)
        proc, data = ws.readback(*ws.queue_args(reconcile_job))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "simulated")
        self.assertFalse(data["publication"]["mrCreated"])

        retry = ws.publish_request(token="5555555555555555")
        retry_job = ws.submit_publish(retry)
        payload, proc = ws.run_job(retry_job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1, "reconciliation must not open a second MR")
        self.assertFalse(json.loads(proc.stdout)["mr"]["created"])

    def test_reconcile_probe_needs_its_own_consent(self):
        ws = build_to_publish("8888888888888888")
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(token="8888888888888888")
        command = Path(request["commandFile"])
        command.write_text(
            command.read_text(encoding="utf-8").strip() + " --reconcile\n", encoding="utf-8")
        job = ws.submit_publish(request)
        payload, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(driver["outcome"], "refused")
        self.assertEqual(driver["stage"], "request")
        self.assertEqual(driver["error"]["reason"], "consent-missing")
        self.assertIn("reconcile", driver["error"]["detail"])
        self.assertEqual(driver["authorized"], ["import", "push", "mr"])

    def test_a_state_other_than_draft_is_refused_by_the_driver(self):
        ws = build_to_publish("9999999999999999")
        self.addCleanup(ws.cleanup)
        request = ws.publish_request(token="9999999999999999")
        ws.rewrite_request(request, state="ready")
        job = ws.submit_publish(request)
        payload, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(driver["outcome"], "refused")
        self.assertEqual(driver["stage"], "request")
        self.assertEqual(driver["error"]["reason"], "request-malformed")
        self.assertIn("Draft", driver["error"]["detail"])
        self.assertIsNone(driver["push"])
        self.assertIsNone(driver["mr"])
        proc = run(["git", "-C", str(ws.origin), "rev-parse", "--verify",
                    f"refs/heads/{TASK_BRANCH}"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(ws.state_path.read_text(encoding="utf-8"))["mrs"], {})

    def test_tampered_bundle_blocks_the_publication_job(self):
        ws = build_to_publish("6666666666666666")
        self.addCleanup(ws.cleanup)
        bundle = ws.bundle_file
        original = bundle.read_bytes()
        try:
            bundle.write_bytes(original + b"tampered")
            request = ws.publish_request(token="6666666666666666")
            job = ws.submit_publish(request)
            payload, proc = ws.run_job(job)
        finally:
            bundle.write_bytes(original)
        driver = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(driver["outcome"], "refused")
        self.assertEqual(driver["error"]["reason"], "artifact-mismatch")
        self.assertIsNone(driver["push"])
        proc = run(["git", "-C", str(ws.origin), "rev-parse", "--verify",
                    f"refs/heads/{TASK_BRANCH}"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(ws.state_path.read_text(encoding="utf-8"))["mrs"], {})

    def test_ready_state_is_refused_with_its_missing_capability(self):
        ws = build_to_publish("7777777777777777")
        self.addCleanup(ws.cleanup)
        proc = run([
            sys.executable, str(REQUEST_CLI), "publish", "--shared", str(ws.shared),
            "--token", "7777777777777777", "--lenovo-skill-dir", str(Path(ws.root)),
            "--python", sys.executable,
            "--manifest", str(ws.shared / "transfer.manifest.json"),
            "--bundle", str(ws.shared / "transfer.bundle"),
            "--description-file", str(ws.shared / "mr-description.md"),
            "--expected-base", ws.base, "--expected-commit", ws.task,
            "--branch", TASK_BRANCH, "--target-branch", BASE_BRANCH,
            "--host", "gitlab.fixture.invalid", "--project", PROJECT,
            "--change-type", "feat", "--jira-key", "ABC-1", "--subject", "x",
            "--state", "ready", "--authorize", "import,push,mr",
            "--lenovo-checkout", str(ws.lenovo), "--lenovo-worktree", str(ws.lenovo_worktree),
            "--request-name", "ready.json", "--command-file", str(ws.root / "ready.txt"),
        ], env=ws.env())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mr_prepare.py", proc.stderr)


if __name__ == "__main__":
    unittest.main()
