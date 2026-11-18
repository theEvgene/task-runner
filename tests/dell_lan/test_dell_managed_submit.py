"""Dell managed submit: signed job and its reservation publish together (#89).

Uses the real request writer, the real signed queue and the real queue-side
referee; no Lenovo host, no worker, no SMB write and no Git repository, so the
fixture stays fast. Proves the four Dell-side promises: the footprint comes
from the digest-bound request, a conflicting producer is refused before
publication, a failed publication leaves no provisional reservation behind,
and a footprint without a proof-bound release stays held.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from dell_fixture import ROOT, SCRIPTS, lan_bridge, run

REQUEST_CLI = SCRIPTS / "dell_request.py"
MANAGED_CLI = SCRIPTS / "dell_managed_submit.py"
TASK_BRANCH = "task/iteration-1"
BASE_SHA = "a" * 40
TASK_SHA = "b" * 40

sys.path.insert(0, str(SCRIPTS))
import dell_managed_submit  # noqa: E402  (the module under test)


class Fixture:
    """One LAN share, one signed queue, one key, no repository or host."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-managed-"))
        self.shared = self.root / "lan-share"
        self.shared.mkdir()
        self.queue = self.shared / "queue"
        self.key = self.root / "key.bin"
        self.key_bytes = os.urandom(32)
        self.key.write_bytes(self.key_bytes)
        self.dell = self.root / "dell-copy"
        self.dell.mkdir()
        self.lenovo = self.root / "lenovo-checkout"
        self.lenovo.mkdir()
        self.worktree = self.root / "lenovo-task-worktree"

    def env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        return env

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def transfer_request(fx: Fixture, token: str, worktree: Path | None = None,
                     checkout: Path | None = None) -> dict:
    """Write one standalone transfer request through the real writer."""

    manifest = fx.shared / f"manifest-{token}.json"
    bundle = fx.shared / f"bundle-{token}.bundle"
    manifest.write_text("{}\n", encoding="utf-8")
    bundle.write_bytes(b"")
    command_file = fx.root / f"command-{token}.txt"
    proc = run([
        sys.executable, str(REQUEST_CLI), "transfer",
        "--shared", str(fx.shared), "--dialect", "cmd", "--token", token,
        "--lenovo-skill-dir", str(ROOT), "--lenovo-driver-dir", str(SCRIPTS),
        "--python", sys.executable,
        "--manifest", str(manifest), "--bundle", str(bundle),
        "--expected-base", BASE_SHA, "--expected-commit", TASK_SHA,
        "--branch", TASK_BRANCH, "--authorize", "import",
        "--lenovo-checkout", str(checkout or fx.lenovo),
        "--lenovo-worktree", str(worktree or fx.worktree),
        "--request-name", f"transfer-request-{token}.json",
        "--command-file", str(command_file), "--repo", str(fx.dell),
    ], env=fx.env())
    if proc.returncode != 0:
        raise AssertionError(f"transfer request failed: {proc.stdout} {proc.stderr}")
    data = json.loads(proc.stdout)
    data["commandFile"] = command_file
    return data


def submit(fx: Fixture, request: dict, cwd: Path | None = None,
           receipt: Path | None = None) -> tuple[int, str, dict | None]:
    receipt = receipt or (fx.root / f"managed-{os.urandom(4).hex()}.json")
    proc = run([
        sys.executable, str(MANAGED_CLI), "--queue", str(fx.queue),
        "--key-file", str(fx.key),
        "--request", str(request["requestPath"]),
        "--request-sha256", request["requestSha256"],
        "--command-file", str(request["commandFile"]),
        "--cwd", str(cwd or fx.lenovo), "--dialect", "cmd",
        "--receipt-script-path", request["receiptScriptPath"],
        "--shared", str(fx.shared), "--repo", str(fx.dell),
        "--receipt", str(receipt),
    ], env=fx.env())
    data = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else None
    return proc.returncode, proc.stdout + proc.stderr, data


def job_ids(fx: Fixture) -> list[str]:
    directory = fx.queue / "jobs"
    return sorted(path.stem for path in directory.glob("*.json")) \
        if directory.is_dir() else []


def reservation_ids(fx: Fixture) -> list[str]:
    directory = fx.queue / "reservations"
    return sorted(path.stem for path in directory.glob("*.json")) \
        if directory.is_dir() else []


def reservation_of(fx: Fixture, job_id: str) -> dict:
    raw = lan_bridge.read(str(fx.queue / "reservations"), job_id)
    return lan_bridge.open_envelope(raw, fx.key_bytes, "reservation", job_id)


class ManagedSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_transfer_reserves_the_request_worktree_and_proves_it(self):
        fx = self.fx
        request = transfer_request(fx, "aaaaaaaaaaaaaaa1")
        code, output, receipt = submit(fx, request)

        self.assertEqual(code, 0, output)
        self.assertIn(f"JOBID={receipt['jobId']}", output)
        self.assertEqual(receipt["schema"], "dell-lan-managed-submit/1")
        self.assertEqual(receipt["outcome"], "reserved")
        self.assertEqual(receipt["claims"], [f"file:{fx.worktree}"])
        self.assertEqual(receipt["revisions"],
                         {"expectedBaseSha": BASE_SHA,
                          "expectedCommit": TASK_SHA,
                          "expectedPreviousCommit": None})
        self.assertEqual(receipt["requestSha256"], request["requestSha256"])
        self.assertEqual(receipt["requestPath"], request["requestPath"])
        self.assertEqual(receipt["worktree"], str(fx.worktree))
        self.assertEqual(receipt["reservation"]["jobEnvelopeSha256"],
                         reservation_of(fx, receipt["jobId"])["job_envelope_sha256"])
        self.assertEqual(len(receipt["resources"]), 2)
        self.assertTrue(any(item.startswith("file:")
                            for item in receipt["resources"]))
        self.assertTrue(any(item.startswith("working-directory:")
                            for item in receipt["resources"]))
        self.assertEqual(reservation_ids(fx), [receipt["jobId"]])
        self.assertEqual(job_ids(fx), [receipt["jobId"]])

        raw = lan_bridge.read(str(fx.queue / "jobs"), receipt["jobId"])
        payload = lan_bridge.open_envelope(raw, fx.key_bytes, "job",
                                           receipt["jobId"])
        self.assertEqual(
            payload["command"],
            Path(request["commandFile"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["working_directory"], str(fx.lenovo))
        self.assertEqual(payload["expected_working_directory"], str(fx.lenovo))

    def test_conflicting_producer_is_refused_before_publication(self):
        fx = self.fx
        first = transfer_request(fx, "bbbbbbbbbbbbbbb1")
        code, output, receipt = submit(fx, first)
        self.assertEqual(code, 0, output)
        jobs_before = job_ids(fx)
        reservations_before = reservation_ids(fx)

        second = transfer_request(fx, "bbbbbbbbbbbbbbb2")
        code, output, refused = submit(fx, second)
        self.assertEqual(code, 2, output)
        self.assertIn("reservation refused", output)
        self.assertIsNone(refused)
        self.assertEqual(job_ids(fx), jobs_before)
        self.assertEqual(reservation_ids(fx), reservations_before)
        self.assertEqual(reservations_before, [receipt["jobId"]])

        # An independent worktree and working directory stays admitted.
        other = fx.root / "other-task-worktree"
        other.mkdir(parents=True, exist_ok=True)
        third = transfer_request(fx, "bbbbbbbbbbbbbbb3", worktree=other)
        code, output, admitted = submit(fx, third, cwd=other)
        self.assertEqual(code, 0, output)
        self.assertEqual(admitted["claims"], [f"file:{other}"])
        self.assertEqual(len(job_ids(fx)), 2)

    def test_signed_result_without_release_keeps_the_footprint_held(self):
        fx = self.fx
        request = transfer_request(fx, "ccccccccccccccc1")
        code, output, receipt = submit(fx, request)
        self.assertEqual(code, 0, output)

        job_id = receipt["jobId"]
        lan_bridge.publish(
            str(fx.queue / "results"), job_id,
            lan_bridge.seal(
                lan_bridge.build_result_payload(job_id, "ok", 0), fx.key_bytes))
        self.assertFalse((fx.queue / "releases" / f"{job_id}.json").exists())

        repeat = transfer_request(fx, "ccccccccccccccc2")
        code, output, _ = submit(fx, repeat)
        self.assertEqual(code, 2, output)
        self.assertIn("reservation refused", output)
        self.assertEqual(job_ids(fx), [job_id])

    def test_only_managed_schemas_with_a_provable_footprint_are_admitted(self):
        fx = self.fx
        managed = ("dell-lan-transfer-request/1", "dell-lan-publish-request/1",
                   "dell-lan-publish-existing-request/1")
        for index, schema in enumerate(managed):
            path = fx.shared / f"request-{index}.json"
            raw = json.dumps({
                "schema": schema, "lenovoWorktree": str(fx.worktree),
                "expectedBaseSha": BASE_SHA, "expectedCommit": TASK_SHA,
                "expectedPreviousCommit": None}).encode("utf-8")
            path.write_bytes(raw)
            request, digest = dell_managed_submit.load_request(
                path, hashlib.sha256(raw).hexdigest())
            self.assertEqual(dell_managed_submit.claims_for(request),
                             [f"file:{fx.worktree}"])
            self.assertEqual(dell_managed_submit.revisions(request)["expectedCommit"],
                             TASK_SHA)

        for index, schema in enumerate(("dell-lan-base-request/1",
                                        "dell-lan-first-request/1")):
            path = fx.shared / f"unmanaged-{index}.json"
            raw = json.dumps({"schema": schema,
                              "lenovoWorktree": str(fx.worktree)}).encode("utf-8")
            path.write_bytes(raw)
            with self.assertRaises(dell_managed_submit.Stop) as raised:
                dell_managed_submit.load_request(
                    path, hashlib.sha256(raw).hexdigest())
            self.assertEqual(raised.exception.reason, "request-not-managed")

        relative = {"schema": managed[0], "lenovoWorktree": "task-worktree"}
        with self.assertRaises(dell_managed_submit.Stop) as raised:
            dell_managed_submit.claims_for(relative)
        self.assertEqual(raised.exception.reason, "claim-unprovable")
        with self.assertRaises(dell_managed_submit.Stop) as raised:
            dell_managed_submit.revisions({"expectedCommit": "b" * 12})
        self.assertEqual(raised.exception.reason, "revision-unprovable")

    def test_tampered_request_or_borrowed_command_writes_nothing(self):
        fx = self.fx
        request = transfer_request(fx, "ddddddddddddddd1")
        path = Path(request["requestPath"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expectedCommit"] = "c" * 40
        path.write_text(json.dumps(payload), encoding="utf-8")

        code, output, _ = submit(fx, request)
        self.assertEqual(code, 1, output)
        self.assertIn("request-mismatch", output)
        self.assertEqual(job_ids(fx), [])
        self.assertEqual(reservation_ids(fx), [])

        # A claim can never be paired with another request's command file.
        own = transfer_request(fx, "ddddddddddddddd2")
        borrowed = transfer_request(fx, "ddddddddddddddd3")
        own["commandFile"] = borrowed["commandFile"]
        code, output, _ = submit(fx, own)
        self.assertEqual(code, 1, output)
        self.assertIn("command-unbound", output)
        self.assertEqual(job_ids(fx), [])
        self.assertEqual(reservation_ids(fx), [])

    def test_failed_publication_removes_only_its_own_reservation(self):
        fx = self.fx
        request = transfer_request(fx, "eeeeeeeeeeeeeee1")
        original = lan_bridge.publish

        def failing_publish(directory, job_id, data):
            if Path(str(directory)).name == "jobs":
                raise lan_bridge.BridgeError("simulated publication failure")
            return original(directory, job_id, data)

        lan_bridge.publish = failing_publish
        self.addCleanup(setattr, lan_bridge, "publish", original)
        with self.assertRaises(dell_managed_submit.Stop) as raised:
            dell_managed_submit.submit_managed(
                lan_bridge, fx.queue, fx.key_bytes,
                request_path=Path(request["requestPath"]),
                request_sha256=request["requestSha256"],
                command=Path(request["commandFile"]).read_text(encoding="utf-8"),
                cwd=str(fx.lenovo), dialect="cmd",
                receipt_script_path=request["receiptScriptPath"])
        self.assertEqual(raised.exception.reason, "reservation-refused")
        self.assertIn("simulated publication failure", raised.exception.detail)
        self.assertEqual(job_ids(fx), [])
        self.assertEqual(reservation_ids(fx), [])


if __name__ == "__main__":
    unittest.main()
