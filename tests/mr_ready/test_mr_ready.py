"""Fast offline tests for the existing-MR Ready queue adapter."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "mr_ready"))

import readback  # noqa: E402
import request  # noqa: E402

URL = "https://gitlab.example/team/app/-/merge_requests/7"
HEAD = "1" * 40
TARGET = "2" * 40


def run_receipt(status: str = "ready") -> dict:
    receipt = {
        "schema": "mr-ready-run/1", "status": status, "mr_url": URL,
        "project_path": "team/app", "iid": 7,
        "source_branch": "feature/ABC-1", "target_branch": "master",
        "head_sha": HEAD, "target_sha": TARGET,
        "gate": {"outcome": "passed", "sha": HEAD, "pipeline_id": 9},
        "phase_gate": {"source": "preflight"},
        "publish": {"mr_url": URL, "draft": False,
                    "target_sha": TARGET},
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
        self.assertIn("branch_transfer", built["forbidden"])
        self.assertIn("create_merge_request", built["forbidden"])


class ReadbackTests(unittest.TestCase):
    def test_confirmed_ready_is_bound_to_head_and_target(self) -> None:
        result = readback.verify(payload(run_receipt(), 0), "a" * 32, URL)
        self.assertEqual(result["verdict"], "ready")
        self.assertEqual(result["run"]["head_sha"], HEAD)
        self.assertEqual(result["run"]["target_sha"], TARGET)

    def test_stale_pipeline_sha_never_confirms_ready(self) -> None:
        receipt = run_receipt()
        receipt["gate"]["sha"] = "3" * 40
        result = readback.verify(payload(receipt, 0), "a" * 32, URL)
        self.assertEqual(result["verdict"], "mismatch")
        self.assertEqual(result["reason"], "stale_gate_sha")

    def test_untrusted_worker_status_never_confirms_ready(self) -> None:
        result_payload = payload(run_receipt(), 0)
        result_payload["status"] = "running"
        result = readback.verify(result_payload, "a" * 32, URL)
        self.assertEqual(result["verdict"], "unknown")
        self.assertEqual(result["reason"], "runner_error")

    def test_prepared_new_head_stops_until_sha_is_published(self) -> None:
        result = readback.verify(
            payload(run_receipt("draft"), 1), "a" * 32, URL)
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["reason"], "prepared_new_head")
        self.assertIn("push", result["next_action"])

    def test_ambiguous_ready_write_is_unknown_not_success(self) -> None:
        receipt = run_receipt("draft")
        receipt["blockers"][0]["kind"] = "publish_landed_unconfirmed"
        receipt["publish"] = {"write": "not_confirmed",
                              "readback": "ready"}
        result = readback.verify(payload(receipt, 1), "a" * 32, URL)
        self.assertEqual(result["verdict"], "unknown")
        self.assertEqual(result["reason"], "write_unconfirmed")


if __name__ == "__main__":
    unittest.main()
