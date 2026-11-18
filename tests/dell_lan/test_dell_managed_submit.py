"""Dell managed submit: signed job and its reservation publish together (#89).

Uses the real request writer, the real signed queue and the real queue-side
referee; no Lenovo host, no worker, no SMB write and no Git repository, so the
fixture stays fast. Proves the Dell-side promises: the footprint is the
digest-bound request's worktree *and* checkout, the command is the one
allowlisted driver invocation for that exact request, a conflicting producer
is refused before publication, a failed publication leaves no provisional
reservation behind, a footprint without a proof-bound release stays held, and
a proof that cannot be read still prints its job id and writes an unknown
receipt that forbids a resubmit.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
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
        self.assertEqual(receipt["claims"],
                         [f"file:{fx.worktree}", f"file:{fx.lenovo}"])
        self.assertEqual(receipt["revisions"],
                         {"expectedBaseSha": BASE_SHA,
                          "expectedCommit": TASK_SHA,
                          "expectedPreviousCommit": None})
        self.assertEqual(receipt["requestSha256"], request["requestSha256"])
        self.assertEqual(receipt["requestPath"], request["requestPath"])
        self.assertEqual(receipt["worktree"], str(fx.worktree))
        self.assertEqual(receipt["checkout"], str(fx.lenovo))
        self.assertEqual(receipt["reservation"]["jobEnvelopeSha256"],
                         reservation_of(fx, receipt["jobId"])["job_envelope_sha256"])
        self.assertEqual(len(receipt["resources"]), 3)
        self.assertTrue(any(item.startswith("file:")
                            and "lenovo-checkout" in item
                            for item in receipt["resources"]))
        self.assertTrue(any(item.startswith("working-directory:")
                            and "lenovo-checkout" in item
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

        # The ref store is one footprint: another free task worktree inside
        # the same checkout still writes that checkout's refs, so it refuses.
        other = fx.root / "other-task-worktree"
        other.mkdir(parents=True, exist_ok=True)
        third = transfer_request(fx, "bbbbbbbbbbbbbbb3", worktree=other)
        code, output, _ = submit(fx, third)
        self.assertEqual(code, 2, output)
        self.assertIn("reservation refused", output)
        self.assertEqual(job_ids(fx), jobs_before)

        # An independent worktree *and* an independent checkout stays admitted.
        alt = fx.root / "lenovo-checkout-2"
        alt.mkdir(parents=True, exist_ok=True)
        fourth = transfer_request(fx, "bbbbbbbbbbbbbbb4", worktree=other,
                                  checkout=alt)
        code, output, admitted = submit(fx, fourth, cwd=alt)
        self.assertEqual(code, 0, output)
        self.assertEqual(admitted["claims"],
                         [f"file:{other}", f"file:{alt}"])
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
                "lenovoCheckout": str(fx.lenovo),
                "expectedBaseSha": BASE_SHA, "expectedCommit": TASK_SHA,
                "expectedPreviousCommit": None}).encode("utf-8")
            path.write_bytes(raw)
            request, digest = dell_managed_submit.load_request(
                path, hashlib.sha256(raw).hexdigest())
            self.assertEqual(dell_managed_submit.claims_for(request),
                             [f"file:{fx.worktree}", f"file:{fx.lenovo}"])
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

        relative = {"schema": managed[0], "lenovoWorktree": "task-worktree",
                    "lenovoCheckout": str(fx.lenovo)}
        with self.assertRaises(dell_managed_submit.Stop) as raised:
            dell_managed_submit.claims_for(relative)
        self.assertEqual(raised.exception.reason, "claim-unprovable")
        # The checkout whose ref store the route writes is part of the claim
        # set: a request that names no provable checkout is refused.
        with self.assertRaises(dell_managed_submit.Stop) as raised:
            dell_managed_submit.claims_for({
                "schema": managed[0], "lenovoWorktree": str(fx.worktree)})
        self.assertEqual(raised.exception.reason, "claim-unprovable")
        with self.assertRaises(dell_managed_submit.Stop) as raised:
            dell_managed_submit.claims_for({
                "schema": managed[0], "lenovoWorktree": str(fx.worktree),
                "lenovoCheckout": str(fx.worktree)})
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

    def test_forged_or_driver_less_command_is_refused_before_publication(self):
        fx = self.fx
        own = transfer_request(fx, "fffffffffffffff1")
        borrowed = transfer_request(fx, "fffffffffffffff2")
        # The review's repro: another request's real driver line plus an inert
        # statement that merely carries this request's path and digest.
        forged = fx.root / "forged-command.txt"
        forged.write_text(
            Path(borrowed["commandFile"]).read_text(encoding="utf-8").strip()
            + f' & rem "{own["requestPath"]}" {own["requestSha256"]}\n',
            encoding="utf-8")
        own["commandFile"] = forged
        code, output, receipt = submit(fx, own)
        self.assertEqual(code, 1, output)
        self.assertIn("command-unbound", output)
        self.assertIsNone(receipt)
        self.assertEqual(job_ids(fx), [])
        self.assertEqual(reservation_ids(fx), [])

        # The same forgery in the PowerShell statement-list form is refused by
        # the binding itself, with no shell runtime and no queue write.
        statement = (
            "powershell.exe -NoProfile -NonInteractive -Command \"& 'C:\\py.exe' "
            "'C:\\drv\\lenovo_transfer.py' '--request' "
            f"'{borrowed['requestPath']}' '--request-sha256' "
            f"'{borrowed['requestSha256']}'; Write-Host "
            f"'{own['requestPath']} {own['requestSha256']}'\"")
        with self.assertRaises(dell_managed_submit.Stop) as raised:
            dell_managed_submit.bind_command(
                statement, Path(own["requestPath"]), own["requestSha256"],
                "dell-lan-transfer-request/1", "powershell")
        self.assertEqual(raised.exception.reason, "command-unbound")

        # A command that runs no driver at all is refused too.
        noop = fx.root / "noop-command.txt"
        noop.write_text(f'echo "{own["requestPath"]}" {own["requestSha256"]}\n',
                        encoding="utf-8")
        own["commandFile"] = noop
        code, output, _ = submit(fx, own)
        self.assertEqual(code, 1, output)
        self.assertIn("command-unbound", output)
        self.assertEqual(job_ids(fx), [])

    def test_cwd_outside_the_request_checkout_is_refused(self):
        fx = self.fx
        request = transfer_request(fx, "abcd0000000000f1")
        stray = fx.root / "stray-cwd"
        stray.mkdir()
        code, output, receipt = submit(fx, request, cwd=stray)
        self.assertEqual(code, 2, output)
        self.assertIn("cwd-unbound", output)
        self.assertIsNone(receipt)
        self.assertEqual(job_ids(fx), [])
        self.assertEqual(reservation_ids(fx), [])

        # The admitted job runs in the request's checkout, not the operator's.
        code, output, receipt = submit(fx, request)
        self.assertEqual(code, 0, output)
        self.assertEqual(receipt["workingDirectory"], str(fx.lenovo))
        self.assertEqual(receipt["checkout"], str(fx.lenovo))

    def test_unreadable_proof_keeps_the_jobid_and_holds_the_slot(self):
        fx = self.fx
        request = transfer_request(fx, "abcd0000000000f2")
        receipt_path = fx.root / "unknown-receipt.json"
        original = dell_managed_submit._reservations
        dell_managed_submit._reservations = lambda: (None, None)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = dell_managed_submit.main([
                    "--queue", str(fx.queue), "--key-file", str(fx.key),
                    "--request", str(request["requestPath"]),
                    "--request-sha256", request["requestSha256"],
                    "--command-file", str(request["commandFile"]),
                    "--cwd", str(fx.lenovo), "--dialect", "cmd",
                    "--receipt-script-path", request["receiptScriptPath"],
                    "--shared", str(fx.shared), "--repo", str(fx.dell),
                    "--receipt", str(receipt_path)])
        finally:
            dell_managed_submit._reservations = original

        published = job_ids(fx)
        self.assertEqual(len(published), 1)
        self.assertEqual(code, 3, out.getvalue() + err.getvalue())
        self.assertIn(f"JOBID={published[0]}", out.getvalue())
        self.assertIn("never resubmit", err.getvalue())
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], "dell-lan-managed-submit/1")
        self.assertEqual(receipt["outcome"], "unknown")
        self.assertEqual(receipt["jobId"], published[0])
        self.assertIsNone(receipt["reservation"])
        self.assertEqual(receipt["resubmit"], "forbidden")
        self.assertEqual(reservation_ids(fx), published)

        # The slot is held: a blind resubmit for the footprint is refused.
        repeat = transfer_request(fx, "abcd0000000000f3")
        code, output, _ = submit(fx, repeat)
        self.assertEqual(code, 2, output)
        self.assertIn("reservation refused", output)
        self.assertEqual(job_ids(fx), published)

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
