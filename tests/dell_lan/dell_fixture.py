"""Disposable two-direction fixture for the task-runner Dell LAN dev loop.

Everything is local: two Git repositories standing in for the Dell copy and the
Lenovo checkout, one bare repository standing in for the external Git remote,
the real signed queue (HMAC sealing, immutable publish), and fixture doubles
for the external publication flow (``tests/dell_lan/fixture_flow``) at its
documented seams. The worker stand-in executes the submitted command exactly
as the queue would, so both directions run through the real CLIs. Task bundles
are staged with plain ``git bundle`` plus a ``git-bundle-manifest/1`` manifest
written directly; no separate stager is required.

The external remote is explicitly simulated: no network, no real Git hosting,
no Lenovo host, and no invented user task.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "dell_lan"
READBACK = SCRIPTS / "dell_readback.py"
BASE_IMPORT = SCRIPTS / "dell_base_import.py"
REQUEST_CLI = SCRIPTS / "dell_request.py"
STAGE_BUNDLE = SCRIPTS / "stage_bundle.py"
BRIDGE_CLI = ROOT / "scripts" / "lan_bridge" / "lan_bridge.py"
FIXTURE_FLOW = TESTS_DIR / "fixture_flow"

sys.path.insert(0, str(BRIDGE_CLI.parent))
sys.path.insert(0, str(SCRIPTS))
import lan_bridge  # noqa: E402  (reviewed library used as-is)

GITLAB_HOST = "gitlab.fixture.invalid"
PROJECT = "group/project"
ORIGIN_URL = f"https://{GITLAB_HOST}/{PROJECT}.git"
BASE_BRANCH = "main"
TASK_BRANCH = "task/iteration-1"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(command: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command], cwd=str(cwd) if cwd else None, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    proc = run(["git", *args], cwd=cwd, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Workspace:
    """Two machines, one shared LAN path, one signed queue, one simulated remote."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-lan-"))
        self.origin = self.root / "remote-origin.git"
        self.lenovo = self.root / "lenovo-checkout"
        self.shared = self.root / "lan-share"
        self.queue = self.shared / "queue"
        self.key = self.root / "key.bin"
        self.state_path = self.root / "remote-state.json"
        self.dell = self.root / "dell-copy"
        self.worktree = self.root / "dell-task-worktree"
        self.verify = self.root / "dell-verify"
        self.lenovo_worktree = self.root / "lenovo-task-worktree"
        self.key_bytes = os.urandom(32)
        self.base = ""
        self.task = ""
        self.bundle_file: Path | None = None
        self.manifest_file: Path | None = None
        self.extra_env: dict[str, str] = {}

    # -- setup ---------------------------------------------------------
    def build(self) -> "Workspace":
        for path in (self.shared, self.verify):
            path.mkdir(parents=True, exist_ok=True)
        self.key.write_bytes(self.key_bytes)
        git("init", "-q", "--bare", "-b", BASE_BRANCH, str(self.origin))
        self.lenovo.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", BASE_BRANCH, cwd=self.lenovo)
        for name, value in (("user.email", "lenovo-fixture@example.invalid"),
                            ("user.name", "lenovo-fixture"), ("core.autocrlf", "false")):
            git("config", name, value, cwd=self.lenovo)
        (self.lenovo / "work.txt").write_text("published base\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.lenovo)
        git("commit", "-qm", "published base", cwd=self.lenovo)
        self.base = git("rev-parse", "--verify", "HEAD", cwd=self.lenovo)
        git("remote", "add", "origin", str(self.origin), cwd=self.lenovo)
        git("remote", "add", "gitlab", f"git@gitlab.fixture.invalid:{PROJECT}.git", cwd=self.lenovo)
        git("push", "-q", "-u", "origin", BASE_BRANCH, cwd=self.lenovo)
        git("fetch", "-q", "origin", cwd=self.lenovo)
        self.state_path.write_text(json.dumps({
            "host": GITLAB_HOST, "project": PROJECT, "nextIid": 1, "mrs": {},
        }, indent=2), encoding="utf-8")
        git("init", "-q", "-b", BASE_BRANCH, cwd=self.verify)
        return self

    def env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["DELL_LAN_FLOW_DIR"] = str(FIXTURE_FLOW)
        env["DELL_FIXTURE_STATE"] = str(self.state_path)
        env["DELL_FIXTURE_ORIGIN"] = str(self.origin)
        env.update(self.extra_env)
        return env

    # -- Lenovo worker stand-in ---------------------------------------
    def run_job(self, job_id: str) -> tuple[dict, subprocess.CompletedProcess]:
        """Execute the sealed job exactly as the queue worker would, then seal the result."""

        raw = lan_bridge.read(str(self.queue / "jobs"), job_id)
        payload = lan_bridge.open_envelope(raw, self.key_bytes, "job", job_id)
        proc = subprocess.run(
            payload["command"], shell=True, cwd=payload["working_directory"],
            env=self.env(), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=payload["timeout_seconds"], check=False,
        )
        files = {}
        for declared in payload.get("return_files") or []:
            path = Path(declared)
            if path.is_file():
                files[str(path)] = base64.b64encode(path.read_bytes()).decode("ascii")
        limit = int(payload["max_output_bytes"])
        result = lan_bridge.build_result_payload(
            job_id, "ok" if proc.returncode == 0 else "error", proc.returncode,
            stdout=proc.stdout[:limit], stderr=proc.stderr[:limit],
            started_at=now_iso(), finished_at=now_iso(),
            output_truncated=len(proc.stdout) > limit, files=files,
        )
        lan_bridge.publish(str(self.queue / "results"), job_id,
                           lan_bridge.seal(result, self.key_bytes))
        return payload, proc

    def submit(self, command_file: Path, return_file: Path | None = None,
               cwd: Path | None = None) -> str:
        command = [
            sys.executable, str(BRIDGE_CLI), "submit", "--queue", str(self.queue),
            "--key-file", str(self.key), "--command-file", str(command_file),
            "--cwd", str(cwd or self.lenovo), "--timeout", "300", "--max-output", "65536",
        ]
        if return_file is not None:
            command += ["--return-file", str(return_file)]
        proc = run(command)
        if proc.returncode != 0:
            raise AssertionError(f"bridge submit failed: {proc.stderr}")
        return proc.stdout.strip().split("JOBID=", 1)[1].strip()

    def fetch(self, job_id: str) -> subprocess.CompletedProcess:
        return run([
            sys.executable, str(BRIDGE_CLI), "fetch", "--queue", str(self.queue),
            "--key-file", str(self.key), "--id", job_id, "--timeout", "3",
            "--poll", "0.05", "--receipt-out", str(self.root / "fetch.json"),
        ])

    def new_job_id(self) -> str:
        return os.urandom(16).hex()

    # -- direction one: Lenovo -> Dell ---------------------------------
    def base_request(self, token: str = "0123456789abcdef") -> dict:
        command_file = self.root / "base-command.txt"
        proc = run([
            sys.executable, str(REQUEST_CLI), "base", "--shared", str(self.shared),
            "--token", token, "--lenovo-skill-dir", str(ROOT),
            "--lenovo-driver-dir", str(SCRIPTS),
            "--python", sys.executable, "--lenovo-checkout", str(self.lenovo),
            "--ref-full", f"refs/heads/{BASE_BRANCH}",
            "--request-name", f"base-request-{token}.json",
            "--command-file", str(command_file), "--repo", str(self.lenovo),
        ], env=self.env())
        if proc.returncode != 0:
            raise AssertionError(f"base request failed: {proc.stdout} {proc.stderr}")
        data = json.loads(proc.stdout)
        data["commandFile"] = command_file
        return data

    def request_base(self, token: str = "0123456789abcdef") -> str:
        data = self.base_request(token)
        return self.submit(data["commandFile"], return_file=Path(data["returnFile"]))

    def import_base(self, job_id: str, receipt: Path | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        return self.import_base_to(self.dell, job_id, receipt=receipt)

    def import_base_to(self, repo_out: Path, job_id: str, *extra: str,
                       receipt: Path | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        receipt = receipt or (self.root / f"base-import-{os.urandom(3).hex()}.json")
        proc = run([
            sys.executable, str(BASE_IMPORT), "--queue", str(self.queue),
            "--key-file", str(self.key), "--job-id", job_id, "--shared", str(self.shared),
            "--repo", str(self.dell),
            "--repo-out", str(repo_out), "--branch", BASE_BRANCH,
            "--expected-project", PROJECT, "--receipt", str(receipt), *extra,
        ], env=self.env())
        data = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else None
        return proc, data

    # -- local work ----------------------------------------------------
    def cut_worktree(self) -> str:
        git("worktree", "add", "-q", "-b", TASK_BRANCH, str(self.worktree), BASE_BRANCH, cwd=self.dell)
        (self.worktree / "work.txt").write_text("published base\ntask work\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.worktree)
        git("commit", "-qm", "task work", cwd=self.worktree)
        self.task = git("rev-parse", "--verify", "HEAD", cwd=self.worktree)
        return self.task

    def commit_second_task(self, text: str = "task work iteration 2\n") -> tuple[str, str]:
        """Append a descendant commit in the Dell task worktree; return (previous, new)."""

        previous = self.task
        (self.worktree / "work.txt").write_text("published base\ntask work\n" + text, encoding="utf-8")
        git("add", "work.txt", cwd=self.worktree)
        git("commit", "-qm", "task work iteration 2", cwd=self.worktree)
        self.task = git("rev-parse", "--verify", "HEAD", cwd=self.worktree)
        return previous, self.task

    def stage_bundle(self) -> dict:
        """Stage the task worktree through the real staging script.

        Every attempt stages fresh unique paths (never re-staging over an
        existing bundle): the staging script enforces immutable creation, so
        the fixture names one new pair per attempt and tracks it for the
        publish request and readback that follow. These tests exercise the
        real staging interface the driver verifies.
        """

        tag = os.urandom(4).hex()
        bundle_name = f"transfer-{tag}.bundle"
        manifest_name = f"transfer-{tag}.manifest.json"
        receipt = self.root / f"bundle-receipt-{tag}.json"
        proc = run([
            sys.executable, str(STAGE_BUNDLE), "--worktree", str(self.worktree),
            "--shared", str(self.shared), "--bundle-name", bundle_name,
            "--manifest-name", manifest_name,
            "--expected-tip", self.task, "--receipt", str(receipt),
        ])
        if proc.returncode != 0:
            raise AssertionError(f"bundle stage failed: {proc.stdout} {proc.stderr}")
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if data.get("outcome") != "staged":
            raise AssertionError(f"bundle stage outcome: {data}")
        self.bundle_file = self.shared / bundle_name
        self.manifest_file = self.shared / manifest_name
        return data

    def build_ready(self, token: str = "0123456789abcdef") -> "Workspace":
        """Import the base, cut the task worktree, and stage the task bundle."""

        job = self.request_base(token)
        self.run_job(job)
        proc, data = self.import_base(job)
        if proc.returncode != 0:
            raise AssertionError(f"base import failed: {proc.stdout} {proc.stderr}")
        self.cut_worktree()
        self.stage_bundle()
        self.base_job = job
        return self

    # -- direction two: Dell -> Lenovo -> remote --------------------------
    def publish_request(self, authorize: str = "import,push,mr",
                        token: str = "fedcba9876543210",
                        subject: str = "fixture task", state: str = "draft",
                        request_name: str | None = None,
                        previous: str | None = None,
                        description: str | None = None) -> dict:
        text = (description if description is not None
                else "Fixture change for the LAN local-dev flow.\n")
        description_path = self.shared / "mr-description.md"
        description_path.write_bytes(text.encode("utf-8"))
        command_file = self.root / f"publish-command-{token}.txt"
        name = request_name or f"publish-request-{token}.json"
        command = [
            sys.executable, str(REQUEST_CLI), "publish", "--shared", str(self.shared),
            "--token", token, "--lenovo-skill-dir", str(ROOT),
            "--lenovo-driver-dir", str(SCRIPTS), "--python", sys.executable,
            "--lenovo-flow-dir", str(FIXTURE_FLOW),
            "--manifest", str(self.manifest_file),
            "--bundle", str(self.bundle_file),
            "--description-file", str(description_path),
            "--expected-base", self.base, "--expected-commit", self.task,
            "--branch", TASK_BRANCH, "--target-branch", BASE_BRANCH,
            "--host", GITLAB_HOST, "--project", PROJECT, "--change-type", "feat",
            "--jira-key", "ABC-1", "--subject", subject, "--state", state,
            "--authorize", authorize, "--lenovo-checkout", str(self.lenovo),
            "--lenovo-worktree", str(self.lenovo_worktree),
            "--request-name", name, "--command-file", str(command_file),
            "--repo", str(self.dell),
        ]
        if previous:
            command += ["--expected-previous", previous]
        proc = run(command, env=self.env())
        if proc.returncode != 0:
            raise AssertionError(f"publish request failed: {proc.stdout} {proc.stderr}")
        data = json.loads(proc.stdout)
        data["commandFile"] = command_file
        return data

    def submit_publish(self, request: dict) -> str:
        return self.submit(request["commandFile"])

    def rewrite_request(self, request: dict, **fields) -> Path:
        """Rewrite a request file outside the writer and re-point its command digest.

        This bypasses ``dell_request.py`` on purpose: only a driver-level guard
        can be exercised this way.
        """

        path = Path(request["requestPath"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(fields)
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
        path.write_bytes(text.encode("utf-8"))
        digest = sha256_bytes(text.encode("utf-8"))
        command_file = Path(request["commandFile"])
        command_file.write_text(
            command_file.read_text(encoding="utf-8").replace(
                request["requestSha256"], digest),
            encoding="utf-8",
        )
        request["requestSha256"] = digest
        return command_file

    def reconcile(self, request: dict) -> str:
        command_file = Path(request["commandFile"])
        text = command_file.read_text(encoding="utf-8").strip()
        command_file.write_text(text + ' --reconcile\n', encoding="utf-8")
        return self.submit(command_file)

    def readback(self, *extra: str, receipt: Path | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        receipt = receipt or (self.root / f"readback-{os.urandom(4).hex()}.json")
        command = [
            sys.executable, str(READBACK),
            "--repo", str(self.dell), "--expected-base", self.base,
            "--task-commit", self.task, "--worktree", str(self.worktree),
            "--bundle-manifest", str(self.manifest_file),
            "--bundle-dir", str(self.shared), "--shared", str(self.shared),
            "--receipt", str(receipt), *extra,
        ]
        proc = run(command, env=self.env())
        data = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else None
        return proc, data

    def readback_command(self, *extra: str, receipt: Path | None = None) -> list[str]:
        """A readback command line the caller can build without the fixture defaults.

        Anchoring always comes from the fixture; the queue, shared-root, and
        bundle arguments are the caller's, so a case can prove what happens when
        one of those protected roots is missing.
        """

        receipt = receipt or (self.root / f"readback-{os.urandom(4).hex()}.json")
        return [
            sys.executable, str(READBACK),
            "--repo", str(self.dell), "--expected-base", self.base,
            "--task-commit", self.task, "--worktree", str(self.worktree),
            "--receipt", str(receipt), *extra,
        ]

    def queue_args(self, job_id: str) -> list[str]:
        return ["--queue", str(self.queue), "--key-file", str(self.key), "--job-id", job_id]

    # -- helpers for the read-only capture path ------------------------
    def identity(self, head: str | None = None, simulated: bool = True,
                 local_head: str | None = None, iid: int = 7) -> dict:
        head = head or self.task
        local_head = local_head or head
        receipt = {
            "schema_version": 1, "captured_at": now_iso(), "gitlab_host": GITLAB_HOST,
            "project_path": PROJECT, "project_path_encoded": PROJECT.replace("/", "%2F"),
            "iid": iid, "mr_url": f"https://{GITLAB_HOST}/{PROJECT}/-/merge_requests/{iid}",
            "title": "Draft: feat(ABC-1): fixture task", "state": "opened",
            "source_branch": TASK_BRANCH, "target_branch": BASE_BRANCH,
            "head_sha": head, "target_sha": SHA_C, "changes_count": "1",
            "changed_files": ["work.txt"],
            "links": {"mr": f"https://{GITLAB_HOST}/{PROJECT}/-/merge_requests/{iid}"},
            "local": {"repo": str(self.lenovo), "matching_remotes": ["origin"],
                      "source_branch_head": local_head,
                      "head_matches_receipt": local_head == head},
        }
        if simulated:
            receipt["simulated"] = True
        return receipt

    def seal_result(self, job_id: str, stdout: str, status: str = "ok",
                    exit_code: int = 0, timed_out: bool = False,
                    truncated: bool = False, stderr: str = "") -> Path:
        payload = lan_bridge.build_result_payload(
            job_id, status, exit_code, stdout=stdout, stderr=stderr,
            started_at=now_iso(), finished_at=now_iso(),
            timed_out=timed_out, output_truncated=truncated,
        )
        return lan_bridge.publish(str(self.queue / "results"), job_id,
                                  lan_bridge.seal(payload, self.key_bytes))

    def cleanup(self) -> None:
        for current, directories, files in os.walk(self.root):
            for name in files:
                try:
                    os.chmod(os.path.join(current, name), stat.S_IWRITE)
                except OSError:
                    pass
            for name in directories:
                try:
                    os.chmod(os.path.join(current, name), stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)


def command_file(workspace: Workspace, text: str | None = None) -> Path:
    """One command file for the read-only capture path."""

    identity = FIXTURE_FLOW / "mr_identity.py"
    default = (
        f'"{sys.executable}" "{identity}" capture '
        f"--url https://{GITLAB_HOST}/{PROJECT}/-/merge_requests/7 "
        f'--repo "{workspace.lenovo}"'
    )
    path = workspace.root / f"command-{os.urandom(4).hex()}.txt"
    path.write_text((text or default) + "\n", encoding="utf-8")
    return path
