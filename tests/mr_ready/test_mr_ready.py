"""Fast offline tests for the existing-MR Ready queue adapter."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "mr_ready"))
sys.path.insert(0, str(ROOT / "scripts" / "lan_bridge"))

import readback  # noqa: E402
import request  # noqa: E402
import lan_bridge  # noqa: E402

URL = "https://gitlab.example/team/app/-/merge_requests/7"
HEAD = "1" * 40
TARGET = "2" * 40
RUNNER = r"C:\personal-agent-toolkit\skills\gitlab-mr-flow\scripts\mr_ready_run.py"


def run_receipt(status: str = "ready") -> dict:
    receipt = {
        "schema": "mr-ready-run/1", "status": status, "mr_url": URL,
        "project_path": "team/app", "iid": 7,
        "source_branch": "feature/ABC-1", "target_branch": "master",
        "head_sha": HEAD, "target_sha": TARGET, "phase": "first-review",
        "title": "feat(ABC-1): Short subject",
        "gate": {"outcome": "passed", "sha": HEAD, "pipeline_id": 9},
        "phase_gate": {
            "source": "preflight", "phase": "first-review",
            "receipt": {"phase_gate": {
                "phase": "first-review", "decision": "pass",
                "blocking_reasons": [],
            }},
        },
        "publish": {
            "mr_url": URL, "iid": 7, "draft": False,
            "title": "feat(ABC-1): Short subject",
            "target_branch": "master", "target_sha": TARGET,
        },
        "blockers": [], "next_action": "review the Ready MR",
    }
    if status == "draft":
        receipt.update(
            gate=None, publish=None,
            blockers=[{"kind": "prepared_new_head",
                       "detail": "preparation changed the local head",
                       "next_step": "push the prepared SHA, then run again"}])
    return receipt


def payload(receipt: dict, exit_code: int) -> dict:
    return {"kind": "result", "id": "a" * 32,
            "status": "ok" if exit_code == 0 else "error",
            "exit_code": exit_code, "stdout": json.dumps(receipt),
            "stderr": "", "timed_out": False,
            "output_truncated": False}


def job(command: str | None = None, receipt_script_path: str = RUNNER) -> dict:
    return {
        "kind": "job", "id": "a" * 32,
        "command": command or (
            "& 'C:\\Python\\python.exe' "
            "'C:\\personal-agent-toolkit\\skills\\gitlab-mr-flow\\scripts\\mr_ready_run.py' "
            f"'--url' '{URL}'"),
        "receipt_script_path": receipt_script_path,
    }


class RequestRoutingTests(unittest.TestCase):
    def test_routes_only_to_existing_toolkit_ready_runner(self) -> None:
        built, command = request.build_request(
            URL, "C:\\Python\\python.exe",
            "C:\\personal-agent-toolkit\\skills\\gitlab-mr-flow\\scripts",
            "C:\\repo", "feat", "ABC-1", "Коротко", ["src/**"])
        self.assertIn("mr_ready_run.py", command)
        self.assertIn("--repo", command)
        self.assertNotIn("dell_lan", command)
        self.assertNotIn("publish_mr.py", command)
        self.assertNotIn("--state", command)
        self.assertEqual(built["action"], "prepare-existing-mr-ready")
        self.assertEqual(built["receipt_script_path"], RUNNER)
        self.assertIn("branch_transfer", built["forbidden"])
        self.assertIn("create_merge_request", built["forbidden"])


class ReadbackTests(unittest.TestCase):
    def test_confirmed_ready_is_bound_to_head_and_target(self) -> None:
        result = readback.verify(payload(run_receipt(), 0), "a" * 32, URL, job())
        self.assertEqual(result["verdict"], "ready")
        self.assertEqual(result["run"]["head_sha"], HEAD)
        self.assertEqual(result["run"]["target_sha"], TARGET)

    def test_stale_pipeline_sha_never_confirms_ready(self) -> None:
        receipt = run_receipt()
        receipt["gate"]["sha"] = "3" * 40
        result = readback.verify(payload(receipt, 0), "a" * 32, URL, job())
        self.assertEqual(result["verdict"], "mismatch")
        self.assertEqual(result["reason"], "stale_gate_sha")

    def test_untrusted_worker_status_never_confirms_ready(self) -> None:
        result_payload = payload(run_receipt(), 0)
        result_payload["status"] = "running"
        result = readback.verify(result_payload, "a" * 32, URL, job())
        self.assertEqual(result["verdict"], "unknown")
        self.assertEqual(result["reason"], "runner_error")

    def test_prepared_new_head_stops_until_sha_is_published(self) -> None:
        result = readback.verify(
            payload(run_receipt("draft"), 1), "a" * 32, URL, job())
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["reason"], "prepared_new_head")
        self.assertIn("push", result["next_action"])

    def test_ambiguous_ready_write_is_unknown_not_success(self) -> None:
        receipt = run_receipt("draft")
        receipt["blockers"][0]["kind"] = "publish_landed_unconfirmed"
        receipt["publish"] = {"write": "not_confirmed",
                              "readback": "ready"}
        result = readback.verify(payload(receipt, 1), "a" * 32, URL, job())
        self.assertEqual(result["verdict"], "unknown")
        self.assertEqual(result["reason"], "write_unconfirmed")

    def test_ready_rejects_each_contradictory_publish_identity(self) -> None:
        for field, value in (("iid", 999), ("target_branch", "release"),
                             ("target_sha", "3" * 40), ("title", "other")):
            with self.subTest(field=field):
                receipt = run_receipt()
                receipt["publish"][field] = value
                result = readback.verify(
                    payload(receipt, 0), "a" * 32, URL, job())
                self.assertEqual(result["verdict"], "mismatch")
                self.assertEqual(result["reason"], "ready_unproven")

    def test_empty_or_nonpassing_phase_gate_never_confirms_ready(self) -> None:
        for phase_gate in ({}, {"source": "preflight", "phase": "first-review"},
                           {"source": "preflight", "phase": "first-review",
                            "receipt": {"phase_gate": {
                                "phase": "first-review", "decision": "blocked",
                                "blocking_reasons": ["conflict"],
                            }}}):
            with self.subTest(phase_gate=phase_gate):
                receipt = run_receipt()
                receipt["phase_gate"] = phase_gate
                result = readback.verify(
                    payload(receipt, 0), "a" * 32, URL, job())
                self.assertEqual(result["reason"], "ready_unproven")

    def test_satisfied_remote_phase_plan_is_substantive_evidence(self) -> None:
        receipt = run_receipt()
        receipt["phase_gate"] = {
            "source": "diagnosis-remote", "phase": "first-review",
            "plan": {
                "phase": "first-review",
                "items": [{"id": "required_checks", "status": "satisfied"}],
            },
        }
        result = readback.verify(
            payload(receipt, 0), "a" * 32, URL, job())
        self.assertEqual(result["verdict"], "ready")

    def test_missing_receipt_script_path_never_confirms_ready(self) -> None:
        result = readback.verify(
            payload(run_receipt(), 0), "a" * 32, URL,
            job(receipt_script_path=""))
        self.assertEqual(result["verdict"], "mismatch")
        self.assertEqual(result["reason"], "runner_provenance_mismatch")

    def test_signed_bridge_job_from_another_script_cannot_confirm_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mr-ready-signed-") as tmp:
            root = Path(tmp)
            queue = root / "queue"
            key_path = root / "key.bin"
            key = b"k" * 32
            key_path.write_bytes(key)
            job_id = "a" * 32
            forged_job = job(
                command="& 'C:\\Python\\python.exe' 'C:\\other\\receipt.py'",
                receipt_script_path=RUNNER)
            lan_bridge.publish(
                queue / "jobs", job_id, lan_bridge.seal(forged_job, key))
            lan_bridge.publish(
                queue / "results", job_id,
                lan_bridge.seal(payload(run_receipt(), 0), key))
            output = io.StringIO()
            with redirect_stdout(output):
                code = readback.main([
                    "--queue", str(queue), "--key-file", str(key_path),
                    "--job-id", job_id, "--expected-mr-url", URL,
                    "--bridge-dir", str(ROOT / "scripts" / "lan_bridge"),
                    "--timeout", "0.1", "--poll", "0.01",
                ])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(result["verdict"], "mismatch")
            self.assertEqual(result["reason"], "runner_provenance_mismatch")


if __name__ == "__main__":
    unittest.main()
