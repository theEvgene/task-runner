"""Standalone publication of an existing Lenovo branch (issue #43).

Uses the real queue, request writer, publish-existing driver and Git; only
the remote is the local bare fixture. Proves: an existing MR is reused
without duplication and without changing its Ready status; a missing MR
without title/description yields a missing-data request and, after the
resubmit, one Draft with verified project/branch/URL; an ambiguous MR list
is refused and never reported as success.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from dell_fixture import BASE_BRANCH, PROJECT, TASK_BRANCH, Workspace

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "dell_lan"
REQUEST_CLI = SCRIPTS / "dell_request.py"
GITLAB_HOST = "gitlab.fixture.invalid"


def existing_request(ws: Workspace, token: str, authorize: str = "push,mr",
                     subject: str = "", description: str = "", name: str | None = None,
                     state: str = "draft", change_type: str = "",
                     jira_key: str = "") -> dict:
    from dell_fixture import run

    command_file = ws.root / f"existing-command-{token}.txt"
    command = [
        sys.executable, str(REQUEST_CLI), "publish-existing", "--shared", str(ws.shared),
        "--token", token, "--lenovo-skill-dir", str(ROOT),
        "--lenovo-driver-dir", str(SCRIPTS), "--python", sys.executable,
        "--lenovo-flow-dir", str(TESTS_DIR / "fixture_flow"),
        "--expected-base", ws.base, "--expected-commit", ws.task,
        "--branch", TASK_BRANCH, "--target-branch", BASE_BRANCH,
        "--host", GITLAB_HOST, "--project", PROJECT,
        "--state", state, "--authorize", authorize,
        "--lenovo-checkout", str(ws.lenovo), "--lenovo-worktree", str(ws.lenovo_worktree),
        "--request-name", name or f"existing-request-{token}.json",
        "--command-file", str(command_file), "--repo", str(ws.dell),
    ]
    if change_type:
        command += ["--change-type", change_type]
    if jira_key:
        command += ["--jira-key", jira_key]
    if subject:
        command += ["--subject", subject]
    if description:
        command += ["--description-file", description]
    proc = run(command, env=ws.env())
    if proc.returncode != 0:
        raise AssertionError(f"publish-existing request failed: {proc.stdout} {proc.stderr}")
    data = json.loads(proc.stdout)
    data["commandFile"] = command_file
    return data


def build_with_branch(token: str = "aa00000000000001") -> Workspace:
    """Base + task work + staged bundle + transfer, so the branch exists on Lenovo."""

    ws = Workspace().build()
    job = ws.request_base("bb00000000000001")
    ws.run_job(job)
    proc, _ = ws.import_base(job)
    if proc.returncode != 0:
        raise AssertionError(f"base import failed: {proc.stdout} {proc.stderr}")
    ws.cut_worktree()
    ws.stage_bundle()
    from dell_fixture import run
    import json as _json

    command_file = ws.root / f"transfer-{token}.txt"
    proc = run([
        sys.executable, str(REQUEST_CLI), "transfer", "--shared", str(ws.shared),
        "--token", token, "--lenovo-skill-dir", str(ROOT),
        "--lenovo-driver-dir", str(SCRIPTS), "--python", sys.executable,
        "--manifest", str(ws.manifest_file), "--bundle", str(ws.bundle_file),
        "--expected-base", ws.base, "--expected-commit", ws.task,
        "--branch", TASK_BRANCH, "--authorize", "import",
        "--lenovo-checkout", str(ws.lenovo), "--lenovo-worktree", str(ws.lenovo_worktree),
        "--request-name", f"transfer-request-{token}.json",
        "--command-file", str(command_file), "--repo", str(ws.dell),
    ], env=ws.env())
    if proc.returncode != 0:
        raise AssertionError(f"transfer request failed: {proc.stdout} {proc.stderr}")
    data = _json.loads(proc.stdout)
    job = ws.submit(command_file)
    _, ran = ws.run_job(job)
    if ran.returncode != 0:
        raise AssertionError(f"transfer failed: {ran.stdout} {ran.stderr}")
    return ws


class PublishExistingTests(unittest.TestCase):
    def test_driver_refuses_mr_work_without_mr_authorization(self):
        ws = build_with_branch("aa00000000000005")
        self.addCleanup(ws.cleanup)
        request = existing_request(ws, "bb00000000000005")
        ws.rewrite_request(request, authorized=["push"])
        job = ws.submit(request["commandFile"])
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(driver["error"]["reason"], "consent-missing")
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {})

    def test_existing_mr_is_reused_without_duplication(self):
        ws = build_with_branch("aa00000000000011")
        self.addCleanup(ws.cleanup)
        description = ws.shared / "mr-description.md"
        description.write_text("Fixture change.\n", encoding="utf-8")
        first = existing_request(ws, "bb00000000000011", change_type="feat", jira_key="ABC-1", subject="fixture task",
                                 description=str(description))
        job = ws.submit(first["commandFile"])
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(driver["mr"]["created"])
        url = driver["mr"]["url"]
        # Second run without title/description must reuse the same MR.
        second = existing_request(ws, "cc00000000000011")
        job2 = ws.submit(second["commandFile"])
        _, proc2 = ws.run_job(job2)
        reuse = json.loads(proc2.stdout)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(reuse["outcome"], "ok")
        self.assertFalse(reuse["mr"]["created"])
        self.assertEqual(reuse["mr"]["url"], url)
        self.assertEqual(reuse["identity"]["head_sha"], ws.task)
        self.assertEqual(reuse["identity"]["project_path"], PROJECT)
        self.assertEqual(reuse["identity"]["source_branch"], TASK_BRANCH)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1)

    def test_existing_ready_status_is_not_changed(self):
        ws = build_with_branch("aa00000000000022")
        self.addCleanup(ws.cleanup)
        description = ws.shared / "mr-description.md"
        description.write_text("Fixture change.\n", encoding="utf-8")
        first = existing_request(ws, "bb00000000000022", change_type="feat", jira_key="ABC-1", subject="fixture task",
                                 description=str(description))
        job = ws.submit(first["commandFile"])
        _, proc = ws.run_job(job)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        iid = next(iter(state["mrs"]))
        state["mrs"][iid]["draft"] = False
        ws.state_path.write_text(json.dumps(state), encoding="utf-8")
        second = existing_request(ws, "cc00000000000022")
        job2 = ws.submit(second["commandFile"])
        _, proc2 = ws.run_job(job2)
        reuse = json.loads(proc2.stdout)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertFalse(reuse["mr"]["created"])
        self.assertFalse(reuse["mr"]["draft"], "the Ready status must stay unchanged")
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1)
        self.assertFalse(state["mrs"][iid]["draft"])

    def test_missing_mr_data_requests_fields_then_creates_draft(self):
        ws = build_with_branch("aa00000000000033")
        self.addCleanup(ws.cleanup)
        bare = existing_request(ws, "bb00000000000033")
        job = ws.submit(bare["commandFile"])
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(driver["error"]["reason"], "missing-data")
        self.assertIn("subject", driver["missing"])
        self.assertIn("descriptionFile", driver["missing"])
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {}, "no MR may be created from missing data")
        # Resubmit the same branch with the data: one Draft appears.
        description = ws.shared / "mr-description.md"
        description.write_text("Fixture change.\n", encoding="utf-8")
        full = existing_request(ws, "cc00000000000033", change_type="feat", jira_key="ABC-1", subject="fixture task",
                                description=str(description))
        job2 = ws.submit(full["commandFile"])
        _, proc2 = ws.run_job(job2)
        created = json.loads(proc2.stdout)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertTrue(created["mr"]["created"])
        self.assertTrue(created["mr"]["url"].endswith("/merge_requests/1"))
        self.assertEqual(created["identity"]["project_path"], PROJECT)
        self.assertEqual(created["identity"]["source_branch"], TASK_BRANCH)
        self.assertEqual(created["identity"]["target_branch"], BASE_BRANCH)
        self.assertTrue(created["mr"]["draft"])

    def test_new_ready_mr_reuses_exact_title_and_description_readback(self):
        ws = build_with_branch("aa00000000000035")
        self.addCleanup(ws.cleanup)
        description = ws.shared / "mr-description.md"
        description.write_text("Exact fixture change.\n", encoding="utf-8")
        request = existing_request(
            ws, "bb00000000000035", authorize="push,mr,undraft",
            change_type="feat", jira_key="ABC-1", subject="fixture task",
            description=str(description), state="ready",
        )
        job = ws.submit(request["commandFile"])
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(driver["mr"]["draft"])
        self.assertEqual(driver["undraft"]["descriptionCheck"], "exact")
        self.assertTrue(driver["undraft"]["titleChecked"])

    def test_ambiguous_mr_list_is_refused_not_published(self):
        ws = build_with_branch("aa00000000000044")
        self.addCleanup(ws.cleanup)
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        for iid in (1, 2):
            state["mrs"][str(iid)] = {
                "iid": iid, "source_branch": TASK_BRANCH, "target_branch": BASE_BRANCH,
                "title": "Draft: feat(ABC-1): x", "description": "d", "draft": True,
                "head_sha": ws.task, "target_sha": ws.base,
                "web_url": f"https://{GITLAB_HOST}/{PROJECT}/-/merge_requests/{iid}",
            }
        state["nextIid"] = 3
        ws.state_path.write_text(json.dumps(state), encoding="utf-8")
        description = ws.shared / "mr-description.md"
        description.write_text("Fixture change.\n", encoding="utf-8")
        request = existing_request(ws, "bb00000000000044", change_type="feat", jira_key="ABC-1", subject="fixture task",
                                   description=str(description))
        job = ws.submit(request["commandFile"])
        _, proc = ws.run_job(job)
        driver = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(driver["error"]["reason"], "mr-ambiguous")
        self.assertNotEqual(driver["outcome"], "ok")
        state = json.loads(ws.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 2, "no blind creation on ambiguity")


if __name__ == "__main__":
    unittest.main()
