"""Read-only capture chain: real bundle, real signed queue, simulated remote.

The chain is real where this repository owns it: plain ``git bundle`` stages
the bytes through the filesystem, ``lan_bridge.py submit`` seals the exact
Lenovo command inside an HMAC payload, the worker stand-in seals a result,
``lan_bridge.py fetch`` verifies the result MAC and id binding, and the Dell
readback binds the carried receipt to the task commit. Only the remote receipt
is simulated: it is a fixture capture-shaped receipt marked ``simulated`` and
it is never live publication evidence.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dell_fixture import BRIDGE_CLI, SHA_D, Workspace, command_file

import lan_bridge


class SignedChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = Workspace().build().build_ready()
        cls.command = command_file(cls.ws)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def submit(self) -> str:
        return self.ws.submit(self.command)

    def publish(self, job_id: str, **identity) -> None:
        self.ws.seal_result(job_id, json.dumps(self.ws.identity(**identity)))

    def test_simulated_chain_is_labelled_and_bound(self):
        job_id = self.submit()
        self.publish(job_id)
        fetched = self.ws.fetch(job_id)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(json.loads(fetched.stdout)["id"], job_id)

        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "simulated")
        self.assertEqual(data["reason"], "simulated")
        self.assertEqual(data["jobId"], job_id)
        self.assertTrue(data["queue"]["jobVerified"])
        self.assertEqual(len(data["queue"]["jobCommandSha256"]), 64)
        self.assertTrue(data["queue"]["resultVerified"])
        self.assertEqual(data["queue"]["status"], "ok")
        self.assertEqual(data["queue"]["exitCode"], 0)
        publication = data["publication"]
        self.assertTrue(publication["bound"])
        self.assertTrue(publication["simulated"])
        self.assertTrue(publication["declaredSimulated"])
        self.assertTrue(publication["localChecked"])
        self.assertIsNone(publication["driver"])
        self.assertEqual(publication["headSha"], self.ws.task)
        self.assertEqual(publication["projectPath"], "group/project")
        self.assertEqual(publication["iid"], 7)
        self.assertEqual(publication["state"], "opened")
        self.assertEqual(publication["sourceBranch"], "task/iteration-1")
        self.assertTrue(publication["mrUrl"].endswith("/merge_requests/7"))
        self.assertIn("fixture chain recorded", data["nextAction"])

    def test_simulated_marker_wins_over_live_provenance(self):
        job_id = self.submit()
        self.publish(job_id)
        proc, data = self.ws.readback(
            *self.ws.queue_args(job_id), "--provenance", "live")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "simulated")

    def test_direct_capture_cannot_claim_live_bound_without_description_proof(self):
        job_id = self.submit()
        self.publish(job_id, simulated=False)

        proc, data = self.ws.readback(
            *self.ws.queue_args(job_id), "--provenance", "live")

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(data["reason"], "publication-readback-malformed")
        self.assertIn("signed-description proof", data["detail"])
        self.assertFalse(data["publication"]["bound"])

    def test_tampered_result_is_unverifiable(self):
        job_id = self.submit()
        self.publish(job_id)
        sealed = Path(self.ws.queue) / "results" / f"{job_id}.json"
        text = sealed.read_text(encoding="utf-8")
        index = text.index('"mac":"') + len('"mac":"')
        flipped = "0" if text[index] != "0" else "1"
        sealed.write_text(text[:index] + flipped + text[index + 1:], encoding="utf-8")
        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-result-unverifiable")
        self.assertFalse(data["publication"]["bound"])

    def test_result_bound_to_another_id_is_refused(self):
        other = self.submit()
        self.publish(other)
        sealed = Path(self.ws.queue) / "results" / f"{other}.json"
        victim = self.submit()
        (Path(self.ws.queue) / "results" / f"{victim}.json").write_bytes(sealed.read_bytes())
        proc, data = self.ws.readback(*self.ws.queue_args(victim))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-result-unverifiable")

    def test_result_without_its_job_is_refused(self):
        job_id = self.ws.new_job_id()
        self.ws.seal_result(job_id, json.dumps(self.ws.identity()))
        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-job-missing")
        self.assertFalse(data["queue"]["jobVerified"])

    def test_job_with_a_foreign_command_is_refused(self):
        foreign = command_file(self.ws, text="Get-Content -Raw something-else.txt")
        job_id = self.ws.submit(foreign)
        self.publish(job_id)
        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "queue-job-mismatch")

    def test_mr_head_not_task_commit_is_refused(self):
        job_id = self.submit()
        self.publish(job_id, head=SHA_D)
        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "publication-head-mismatch")
        self.assertFalse(data["publication"]["bound"])

    def test_lenovo_checkout_at_another_commit_is_refused(self):
        job_id = self.submit()
        self.publish(job_id, local_head=SHA_D)
        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["reason"], "publication-local-head-mismatch")

    def test_failed_publication_job_is_reported_factually(self):
        job_id = self.submit()
        self.ws.seal_result(job_id, json.dumps(self.ws.identity()), status="error", exit_code=1)
        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["verdict"], "rejected")
        self.assertEqual(data["reason"], "publication-job-failed")
        self.assertEqual(data["queue"]["exitCode"], 1)
        self.assertFalse(data["publication"]["bound"])
        self.assertIn("new job id", data["nextAction"])

    def test_unknown_outcome_reconciles_before_retry(self):
        job_id = self.submit()
        proc, data = self.ws.readback(
            *self.ws.queue_args(job_id), "--fetch-timeout", "0.2", "--poll", "0.05")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(data["verdict"], "unknown")
        self.assertEqual(data["reason"], "outcome-unknown")
        self.assertIn("do not start a second writer", data["nextAction"])

        self.publish(job_id)
        with self.assertRaises(lan_bridge.JobExistsError):
            self.ws.seal_result(job_id, "{}")

        proc, data = self.ws.readback(*self.ws.queue_args(job_id))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["verdict"], "simulated")
        self.assertEqual(data["jobId"], job_id)

    def test_bridge_cli_is_the_documented_entrypoint(self):
        self.assertEqual(BRIDGE_CLI.name, "lan_bridge.py")
        self.assertTrue(BRIDGE_CLI.is_file())


if __name__ == "__main__":
    unittest.main()
