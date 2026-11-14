"""Fail-closed pins for the repeat transfer and the unknown classification.

Fast and deterministic: no queue, no bundle stager, no network, no Lenovo
host. The race test calls ``import_once`` in-process with a planted
concurrent edit; the unknown tests call ``check_result`` with a fake sealed
bridge. Nothing here proves a live run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan"
sys.path.insert(0, str(SCRIPTS))

import lenovo_bundle
from dell_lan_common import Stop as DriverStop
from dell_readback_checks import Stop, check_result


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_ok_bool(*args: str, cwd: Path) -> bool:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=False).returncode == 0


class MiniRepo:
    """One checkout with base -> previous -> expected, branch + worktree at previous.

    With ``same_tree_q`` the history is base -> previous -> Q -> expected, where
    Q is an empty commit: it carries the same tree as the prior SHA and is an
    ancestor of the new commit. That is exactly the shape a bare
    ``git merge --ff-only`` silently accepts even though the request named the
    prior SHA, and it must stay refused.
    """

    BRANCH = "task/race"

    def __init__(self, same_tree_q: bool = False) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-race-"))
        self.checkout = self.root / "checkout"
        self.worktree = self.root / "lenovo-worktree"
        self.concurrent: str | None = None
        self.checkout.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=self.checkout)
        git("config", "user.email", "race-fixture@example.invalid", cwd=self.checkout)
        git("config", "user.name", "race-fixture", cwd=self.checkout)
        git("config", "core.autocrlf", "false", cwd=self.checkout)
        (self.checkout / "work.txt").write_text("base\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.checkout)
        git("commit", "-qm", "base", cwd=self.checkout)
        self.base = git("rev-parse", "--verify", "HEAD", cwd=self.checkout)
        (self.checkout / "work.txt").write_text("base\nprevious\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.checkout)
        git("commit", "-qm", "previous", cwd=self.checkout)
        self.previous = git("rev-parse", "--verify", "HEAD", cwd=self.checkout)
        if same_tree_q:
            git("commit", "-q", "--allow-empty", "-m", "concurrent same-tree commit",
                cwd=self.checkout)
            self.concurrent = git("rev-parse", "--verify", "HEAD", cwd=self.checkout)
        (self.checkout / "work.txt").write_text("base\nprevious\nexpected\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.checkout)
        git("commit", "-qm", "expected", cwd=self.checkout)
        self.expected = git("rev-parse", "--verify", "HEAD", cwd=self.checkout)
        git("branch", self.BRANCH, self.previous, cwd=self.checkout)
        git("worktree", "add", "-q", str(self.worktree), self.BRANCH, cwd=self.checkout)
        bundle = self.root / "transfer.bundle"
        git("bundle", "create", str(bundle), "HEAD", cwd=self.checkout)
        self.bundle = bundle

    def request(self) -> dict:
        return {
            "lenovoCheckout": str(self.checkout),
            "branch": self.BRANCH,
            "expectedCommit": self.expected,
            "expectedBaseSha": self.base,
            "expectedPreviousCommit": self.previous,
            "token": "racetest00000001",
            "lenovoWorktree": str(self.worktree),
            "authorized": ["import"],
        }

    def branch_head(self) -> str:
        return git("rev-parse", "--verify", f"refs/heads/{self.BRANCH}", cwd=self.checkout)

    def apply_move(self) -> None:
        """Reach the state a completed move leaves: ref and files both at expected."""

        git("update-ref", f"refs/heads/{self.BRANCH}", self.expected, self.previous,
            cwd=self.checkout)
        git("read-tree", "-m", "-u", self.previous, self.expected, cwd=self.worktree)


class RaceNoLossTests(unittest.TestCase):
    def test_the_repeat_move_is_a_ref_compare_and_swap_not_a_merge(self):
        import re
        source = (SCRIPTS / "lenovo_bundle.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"""['"]--hard['"]""", source), [],
                         "the Lenovo driver must not invoke a discarding reset")
        self.assertEqual(re.findall(r"""['"]reset['"]""", source), [],
                         "no check-then-reset path may exist on the Lenovo driver")
        self.assertEqual(re.findall(r"""['"]merge['"]""", source), [],
                         "git merge --ff-only accepts any ancestor of the new commit, "
                         "a same-tree Q included, so it is not a compare-and-swap")
        self.assertIn('"update-ref"', source,
                      "the repeat move must be a ref compare-and-swap on the prior SHA")
        self.assertIn('"read-tree"', source,
                      "the worktree file step must be a refusing two-tree update")

    def test_same_tree_concurrent_commit_is_never_accepted_as_the_prior(self):
        repo = MiniRepo(same_tree_q=True)
        # The adversarial shape: Q carries the prior SHA's tree and is an
        # ancestor of the new commit, so a bare fast-forward would take it.
        self.assertEqual(
            git("rev-parse", f"{repo.concurrent}^{{tree}}", cwd=repo.checkout),
            git("rev-parse", f"{repo.previous}^{{tree}}", cwd=repo.checkout))
        self.assertTrue(git_ok_bool("merge-base", "--is-ancestor", repo.concurrent,
                                    repo.expected, cwd=repo.checkout))
        planted = {"done": False}
        real_git_ok = lenovo_bundle.git_ok

        def racing_git_ok(checkout: Path, *args: str):
            ok, output = real_git_ok(checkout, *args)
            if (not planted["done"] and str(checkout) == str(repo.worktree)
                    and tuple(args) == ("status", "--porcelain") and ok
                    and not output.strip()):
                # A concurrent writer commits Q on the same branch after the
                # clean check. The tree is unchanged, so the check cannot tell
                # Q from the prior SHA.
                git("update-ref", f"refs/heads/{MiniRepo.BRANCH}", repo.concurrent,
                    repo.previous, cwd=repo.checkout)
                planted["done"] = True
            return ok, output

        lenovo_bundle.git_ok = racing_git_ok
        try:
            result: dict = {}
            with self.assertRaises(DriverStop) as caught:
                lenovo_bundle.import_once(repo.request(), repo.bundle, repo.expected, result)
        finally:
            lenovo_bundle.git_ok = real_git_ok
        self.assertTrue(planted["done"], "the race plant must have fired")
        stop = caught.exception
        self.assertEqual(stop.reason, "branch-owned")
        self.assertIn(stop.verdict, ("refused", "mismatch"),
                      "the driver outcome for this stop is refused (main maps "
                      "non-unknown stops to refused)")
        self.assertNotIn("import", result, "a refused swap records nothing")
        self.assertEqual(repo.branch_head(), repo.concurrent,
                         "the driver must move nothing when the branch is not at the "
                         "named prior SHA")
        self.assertNotIn("expected", (repo.worktree / "work.txt").read_text(encoding="utf-8"))
        self.assertEqual(git("status", "--porcelain", cwd=repo.worktree), "",
                         "the same tree means the clean check cannot see the "
                         "concurrent commit; only the compare-and-swap can")

    def test_concurrent_edit_after_the_clean_check_is_unknown_not_discarded(self):
        repo = MiniRepo()
        planted = {"done": False}
        real_git_ok = lenovo_bundle.git_ok

        def racing_git_ok(checkout: Path, *args: str):
            ok, output = real_git_ok(checkout, *args)
            if (not planted["done"] and str(checkout) == str(repo.worktree)
                    and tuple(args) == ("status", "--porcelain") and ok
                    and not output.strip()):
                with (repo.worktree / "work.txt").open("a", encoding="utf-8") as handle:
                    handle.write("concurrent edit\n")
                planted["done"] = True
            return ok, output

        lenovo_bundle.git_ok = racing_git_ok
        try:
            result: dict = {}
            with self.assertRaises(DriverStop) as caught:
                lenovo_bundle.import_once(repo.request(), repo.bundle, repo.expected, result)
        finally:
            lenovo_bundle.git_ok = real_git_ok
        self.assertTrue(planted["done"], "the race plant must have fired")
        stop = caught.exception
        self.assertEqual(stop.reason, "import-unknown")
        self.assertEqual(stop.verdict, "unknown")
        self.assertEqual(stop.code, 3)
        self.assertNotIn("import", result)
        victim = (repo.worktree / "work.txt").read_text(encoding="utf-8")
        self.assertIn("concurrent edit", victim,
                      "the two-tree update must refuse instead of overwriting the edit")
        self.assertEqual(repo.branch_head(), repo.expected,
                         "the atomic swap lands before the file step; the partial "
                         "state is reported, never published as a success")
        self.assertTrue(git("status", "--porcelain", cwd=repo.worktree).strip(),
                        "the preserved edit stays visible for reconciliation")

    def test_unrelated_edit_after_move_reports_unknown_with_edits_intact(self):
        repo = MiniRepo()
        planted = {"done": False}
        real_git_ok = lenovo_bundle.git_ok

        def racing_git_ok(checkout: Path, *args: str):
            ok, output = real_git_ok(checkout, *args)
            if (not planted["done"] and str(checkout) == str(repo.worktree)
                    and tuple(args) == ("status", "--porcelain") and ok
                    and not output.strip()):
                (repo.worktree / "notes.txt").write_text("side note\n", encoding="utf-8")
                planted["done"] = True
            return ok, output

        lenovo_bundle.git_ok = racing_git_ok
        try:
            result: dict = {}
            with self.assertRaises(DriverStop) as caught:
                lenovo_bundle.import_once(repo.request(), repo.bundle, repo.expected, result)
        finally:
            lenovo_bundle.git_ok = real_git_ok
        self.assertTrue(planted["done"], "the race plant must have fired")
        stop = caught.exception
        self.assertEqual(stop.reason, "import-unknown")
        self.assertEqual(stop.verdict, "unknown")
        self.assertEqual(stop.code, 3)
        self.assertEqual(
            (repo.worktree / "notes.txt").read_text(encoding="utf-8"), "side note\n",
            "the post-move edit must survive the unknown report")
        self.assertEqual(repo.branch_head(), repo.expected)

    def test_clean_worktree_at_expected_is_an_idempotent_rerun(self):
        repo = MiniRepo()
        repo.apply_move()
        self.assertEqual(git("status", "--porcelain", cwd=repo.worktree), "")
        result: dict = {}
        worktree = lenovo_bundle.import_once(repo.request(), repo.bundle, repo.expected, result)
        self.assertEqual(worktree, Path(repo.request()["lenovoWorktree"]))
        self.assertFalse(result["import"]["updated"])
        self.assertFalse(result["import"]["recovered"])
        self.assertTrue(result["import"]["filesSynced"])
        self.assertEqual(repo.branch_head(), repo.expected)

    def test_dirty_worktree_at_expected_is_unknown_not_a_clean_publication(self):
        repo = MiniRepo()
        repo.apply_move()
        (repo.worktree / "work.txt").write_text(
            "base\nprevious\nexpected\nunreconciled edit\n", encoding="utf-8")
        result: dict = {}
        with self.assertRaises(DriverStop) as caught:
            lenovo_bundle.import_once(repo.request(), repo.bundle, repo.expected, result)
        stop = caught.exception
        self.assertEqual(stop.reason, "import-unknown")
        self.assertEqual(stop.verdict, "unknown")
        self.assertEqual(stop.code, 3)
        self.assertNotIn("import", result, "a dirty state is never reported as published")
        self.assertIn("unreconciled edit",
                      (repo.worktree / "work.txt").read_text(encoding="utf-8"))
        self.assertEqual(repo.branch_head(), repo.expected)

    def test_interrupted_sync_at_expected_is_completed_from_intact_files(self):
        repo = MiniRepo()
        # The interrupt lands right after the atomic swap: the ref is at the
        # new commit and the files are still at the prior one.
        git("update-ref", f"refs/heads/{MiniRepo.BRANCH}", repo.expected, repo.previous,
            cwd=repo.checkout)
        self.assertEqual((repo.worktree / "work.txt").read_text(encoding="utf-8"),
                         "base\nprevious\n")
        result: dict = {}
        lenovo_bundle.import_once(repo.request(), repo.bundle, repo.expected, result)
        self.assertTrue(result["import"]["recovered"])
        self.assertFalse(result["import"]["updated"])
        self.assertEqual((repo.worktree / "work.txt").read_text(encoding="utf-8"),
                         "base\nprevious\nexpected\n")
        self.assertEqual(git("status", "--porcelain", cwd=repo.worktree), "")



class FakeBridge:
    class JobNotFoundError(Exception):
        pass

    class ProtocolError(Exception):
        pass

    class BridgeError(Exception):
        pass

    class FetchTimeout(Exception):
        pass

    fail_with = None

    def __init__(self, key: bytes, command: str, payload: dict | None,
                 receipt_script_path: str | None = None) -> None:
        self.key = key
        self.command = command
        self.payload = payload
        self.receipt_script_path = receipt_script_path

    def load_key(self, path: str, key_format: str, queue: str) -> bytes:
        return self.key

    def read(self, jobs_dir: str, job_id: str) -> bytes:
        return b"job-envelope"

    def open_envelope(self, raw: bytes, key: bytes, kind: str, job_id: str) -> dict:
        return {"command": self.command,
                "receipt_script_path": self.receipt_script_path}

    def fetch_result(self, queue: str, key: bytes, job_id: str,
                     timeout_seconds: float, poll_seconds: float) -> dict:
        if isinstance(self.fail_with, Exception):
            raise self.fail_with
        assert self.payload is not None
        return self.payload


def unknown_args(root: Path) -> SimpleNamespace:
    repo = root / "repo"
    shared = root / "shared"
    queue = root / "queue"
    for path in (repo, shared, queue):
        path.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        queue=str(queue), key_file=str(root / "key.bin"), key_format="raw",
        repo=str(repo), worktree=None, shared=str(shared), bundle_dir=None,
        bundle_manifest=None, job_id="b" * 32, fetch_timeout=3.0, poll=0.05,
        task_commit="a" * 40, provenance="fixture", project=None,
        source_branch=None, target_branch=None,
    )


DRIVER_SCRIPT = r"C:\skill\scripts\dell_lan\lenovo_publish.py"
IDENTITY_SCRIPT = r"C:\flow\gitlab-mr-flow\scripts\mr_identity.py"
DRIVER_COMMAND = (f'"python" "{DRIVER_SCRIPT}" '
                  '--request "x" --request-sha256 "y"')
IDENTITY_COMMAND = (f'"python" "{IDENTITY_SCRIPT}" capture '
                    '--url "https://gitlab.example/mr/1"')


def payload(status: str, exit_code: int, stdout: str = "",
            timed_out: bool = False, truncated: bool = False) -> dict:
    return {"status": status, "exit_code": exit_code, "stdout": stdout,
            "stderr": "", "timed_out": timed_out, "output_truncated": truncated}


def identity_receipt() -> dict:
    return {
        "schema_version": 1,
        "gitlab_host": "gitlab.example",
        "project_path": "group/project",
        "source_branch": "task/test",
        "target_branch": "main",
        "mr_url": "https://gitlab.example/mr/1",
        "iid": 1,
        "state": "opened",
        "head_sha": "a" * 40,
        "target_sha": "c" * 40,
        "captured_at": "2026-11-14T00:00:00+00:00",
    }


class ReadbackCommandSchemaBindingTests(unittest.TestCase):
    def run_check(self, command: str, document: dict,
                  receipt_script_path: str | None = None):
        root = Path(tempfile.mkdtemp(prefix="dell-schema-binding-"))
        (root / "key.bin").write_bytes(b"k" * 32)
        bridge = FakeBridge(
            b"k" * 32, command,
            payload("ok", 0, stdout=json.dumps(document)),
            receipt_script_path or (IDENTITY_SCRIPT if "mr_identity" in command
                                    else DRIVER_SCRIPT))
        return check_result(unknown_args(root), bridge)

    def test_publish_driver_cannot_bypass_gate_with_raw_identity_stdout(self):
        with self.assertRaises(Stop) as caught:
            self.run_check(DRIVER_COMMAND, identity_receipt())
        self.assertEqual(caught.exception.reason,
                         "publication-readback-malformed")
        self.assertIn("structured result envelope", caught.exception.detail)

    def test_direct_identity_command_still_accepts_raw_identity_stdout(self):
        result = self.run_check(IDENTITY_COMMAND, identity_receipt())
        self.assertTrue(result["publication"]["bound"])
        self.assertIsNone(result["publication"]["driver"])

    def test_publish_driver_accepts_gated_structured_stdout(self):
        identity = identity_receipt()
        description_sha = "d" * 64
        document = {
            "schema": "dell-lan-publish-result/1",
            "outcome": "ok",
            "stage": "readback",
            "identity": identity,
            "push": {"observedRemoteSha": "a" * 40},
            "mr": {
                "url": identity["mr_url"],
                "iid": identity["iid"],
                "descriptionVerified": True,
                "descriptionExpectedSha256": description_sha,
                "descriptionObservedSha256": description_sha,
            },
        }
        result = self.run_check(DRIVER_COMMAND, document)
        self.assertTrue(result["publication"]["bound"])
        self.assertEqual(result["publication"]["driverStage"], "readback")

    def test_attacker_driver_basename_is_not_authenticated_evidence(self):
        command = r'python C:\attacker\lenovo_publish.py --request x'
        with self.assertRaises(Stop) as caught:
            self.run_check(command, identity_receipt(), DRIVER_SCRIPT)
        self.assertEqual(caught.exception.reason, "queue-job-mismatch")

    def test_attacker_identity_basename_is_not_authenticated_evidence(self):
        command = r'python C:\attacker\mr_identity.py capture --url https://x'
        with self.assertRaises(Stop) as caught:
            self.run_check(command, identity_receipt(), IDENTITY_SCRIPT)
        self.assertEqual(caught.exception.reason, "queue-job-mismatch")


class UnknownProofTests(unittest.TestCase):
    def check(self, payload_: dict) -> Stop:
        root = Path(tempfile.mkdtemp(prefix="dell-unknown-"))
        (root / "key.bin").write_bytes(b"k" * 32)
        bridge = FakeBridge(b"k" * 32, DRIVER_COMMAND, payload_, DRIVER_SCRIPT)
        with self.assertRaises(Stop) as caught:
            check_result(unknown_args(root), bridge)
        return caught.exception

    def test_proven_refusal_stays_rejected(self):
        stop = self.check(payload("error", 1, stdout="{}", timed_out=False, truncated=False))
        self.assertEqual(stop.reason, "publication-job-failed")
        self.assertEqual(stop.verdict, "rejected")
        self.assertEqual(stop.code, 1)

    def test_exit_three_without_parseable_envelope_is_unknown(self):
        stop = self.check(payload("error", 3, stdout="truncated-garbage {{\"",
                                  timed_out=False, truncated=False))
        self.assertEqual(stop.reason, "outcome-unknown")
        self.assertEqual(stop.verdict, "unknown")
        self.assertEqual(stop.code, 3)

    def test_timed_out_result_is_unknown_despite_exit_code(self):
        stop = self.check(payload("error", 1, stdout="{}", timed_out=True, truncated=False))
        self.assertEqual(stop.reason, "outcome-unknown")
        self.assertEqual(stop.verdict, "unknown")
        self.assertEqual(stop.code, 3)

    def test_truncated_result_is_unknown_despite_exit_code(self):
        stop = self.check(payload("error", 1, stdout="{}", timed_out=False, truncated=True))
        self.assertEqual(stop.reason, "outcome-unknown")
        self.assertEqual(stop.verdict, "unknown")
        self.assertEqual(stop.code, 3)

    def test_timed_out_success_is_unknown_not_bound(self):
        stop = self.check(payload("ok", 0, stdout=json.dumps({"schema_version": 1}),
                                  timed_out=True, truncated=False))
        self.assertEqual(stop.verdict, "unknown")
        self.assertEqual(stop.code, 3)


if __name__ == "__main__":
    unittest.main()
