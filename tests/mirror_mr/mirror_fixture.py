"""Disposable two-remote fixture for the open-MR mirror route (issue #142).

Everything is local: one bare repository standing in for the GitLab
origin (observed on the "Lenovo" checkout), one bare repository standing
in for the private GitHub mirror (published from the "Dell" mirror
checkout), the real signed queue, and fixture doubles at the documented
seams (`gitlab_mr_reader`, `github_ops`). The worker stand-in executes the
submitted Lenovo command exactly as the queue would, so the export runs
through the real CLI. No network, no live service, no invented user task.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[1]
SCRIPTS = ROOT / "scripts" / "mirror_mr"
BRIDGE_CLI = ROOT / "scripts" / "lan_bridge" / "lan_bridge.py"

sys.path.insert(0, str(BRIDGE_CLI.parent))
sys.path.insert(0, str(ROOT / "scripts" / "dell_lan"))
import lan_bridge  # noqa: E402

GITLAB_HOST = "gitlab.fixture.invalid"
PROJECT = "group/project"
MIRROR_REPO = "owner/private-mirror"
BASE_BRANCH = "main"
FEATURE_BRANCH = "feature/open-142"
MR_IID = 7
MR_URL = f"https://{GITLAB_HOST}/{PROJECT}/-/merge_requests/{MR_IID}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(command: list, cwd: Path | None = None,
        env: dict | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command], cwd=str(cwd) if cwd else None,
        env=env, input=input_text, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False)


def git(*args: str, cwd: Path | None = None,
        env: dict | None = None, input_text: str | None = None) -> str:
    proc = run(["git", *args], cwd=cwd, env=env, input_text=input_text)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class Workspace:
    """GitLab origin, Lenovo checkout, signed queue, GitHub mirror."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mirror-mr-"))
        self.gitlab_origin = self.root / "gitlab-origin.git"
        self.lenovo = self.root / "lenovo-checkout"
        self.shared = self.root / "lan-share"
        self.queue = self.shared / "queue"
        self.key = self.root / "key.bin"
        self.gitlab_state = self.root / "gitlab-state.json"
        self.github_state = self.root / "github-state.json"
        self.github_origin = self.root / "github-origin.git"
        self.mirror = self.root / "mirror-checkout"
        self.mapping = self.root / "mapping.json"
        self.key_bytes = os.urandom(32)
        self.base = ""
        self.feature = ""
        self.extra_env: dict[str, str] = {}

    def build(self) -> "Workspace":
        for path in (self.shared, self.lenovo, self.mirror):
            path.mkdir(parents=True, exist_ok=True)
        self.key.write_bytes(self.key_bytes)
        git("init", "-q", "--bare", "-b", BASE_BRANCH, str(self.gitlab_origin))
        git("init", "-q", "-b", BASE_BRANCH, cwd=self.lenovo)
        for name, value in (("user.email", "fixture@example.invalid"),
                            ("user.name", "fixture"),
                            ("core.autocrlf", "false")):
            git("config", name, value, cwd=self.lenovo)
        git("remote", "add", "origin", str(self.gitlab_origin), cwd=self.lenovo)
        (self.lenovo / "work.txt").write_text("base\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.lenovo)
        git("commit", "-qm", "base", cwd=self.lenovo)
        git("push", "-q", "origin", BASE_BRANCH, cwd=self.lenovo)
        git("checkout", "-qb", FEATURE_BRANCH, cwd=self.lenovo)
        (self.lenovo / "work.txt").write_text("base\nfeature\n", encoding="utf-8")
        git("add", "work.txt", cwd=self.lenovo)
        git("commit", "-qm", "feature", cwd=self.lenovo)
        git("push", "-q", "origin", FEATURE_BRANCH, cwd=self.lenovo)
        self.base = git("rev-parse", "--verify", BASE_BRANCH, cwd=self.lenovo)
        self.feature = git("rev-parse", "--verify", FEATURE_BRANCH, cwd=self.lenovo)
        self.gitlab_state.write_text(json.dumps({
            "host": GITLAB_HOST, "project": PROJECT,
            "mrs": {str(MR_IID): {
                "status": "opened", "title": "Add the feature",
                "description": "Review this change.",
                "source_branch": FEATURE_BRANCH, "target_branch": BASE_BRANCH,
                "source_sha": self.feature, "target_sha": self.base}},
        }, indent=2), encoding="utf-8")
        git("init", "-q", "--bare", "-b", BASE_BRANCH, str(self.github_origin))
        git("init", "-q", "-b", BASE_BRANCH, cwd=self.mirror)
        for name, value in (("user.email", "fixture@example.invalid"),
                            ("user.name", "fixture"),
                            ("core.autocrlf", "false")):
            git("config", name, value, cwd=self.mirror)
        # No branch is checked out: ref updates and bundle fetches must
        # never fight a checked-out branch in this operator-owned clone.
        git("symbolic-ref", "HEAD", "refs/heads/_mirror_scratch",
            cwd=self.mirror)
        git("remote", "add", "origin", str(self.github_origin), cwd=self.mirror)
        self.github_state.write_text(json.dumps({
            "repo": MIRROR_REPO, "visibility": "private",
            "nextNumber": 1, "prs": {},
        }, indent=2), encoding="utf-8")
        self.mapping.write_text(json.dumps({
            "version": "mirror-mapping/1",
            "mirrors": {f"{GITLAB_HOST}/{PROJECT}": MIRROR_REPO},
        }, indent=2), encoding="utf-8")
        return self

    def env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["MIRROR_FIXTURE_GITLAB"] = str(self.gitlab_state)
        env["MIRROR_FIXTURE_GITHUB"] = str(self.github_state)
        env["MIRROR_FIXTURE_REMOTE"] = str(self.github_origin)
        env.update(self.extra_env)
        return env

    def run_job(self, job_id: str) -> tuple[dict, subprocess.CompletedProcess]:
        """Execute the sealed job exactly as the queue worker would."""

        raw = lan_bridge.read(str(self.queue / "jobs"), job_id)
        payload = lan_bridge.open_envelope(raw, self.key_bytes, "job", job_id)
        proc = subprocess.run(
            payload["command"], shell=True, cwd=payload["working_directory"],
            env=self.env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=payload["timeout_seconds"], check=False)
        files = {}
        for declared in payload.get("return_files") or []:
            path = Path(declared)
            if path.is_file():
                files[str(path)] = base64.b64encode(path.read_bytes()).decode("ascii")
        limit = int(payload["max_output_bytes"])
        result = lan_bridge.build_result_payload(
            job_id, "ok" if proc.returncode == 0 else "error",
            proc.returncode, stdout=proc.stdout[:limit],
            stderr=proc.stderr[:limit], started_at=now_iso(),
            finished_at=now_iso(),
            output_truncated=len(proc.stdout) > limit, files=files)
        lan_bridge.publish(str(self.queue / "results"), job_id,
                           lan_bridge.seal(result, self.key_bytes))
        return payload, proc

    def submit(self, command_file: Path, return_file: Path | None = None,
               cwd: Path | None = None) -> str:
        command = [sys.executable, str(BRIDGE_CLI), "submit", "--queue",
                   str(self.queue), "--key-file", str(self.key),
                   "--command-file", str(command_file),
                   "--cwd", str(cwd or self.lenovo), "--timeout", "300",
                   "--max-output", "65536"]
        if return_file is not None:
            command += ["--return-file", str(return_file)]
        proc = run(command)
        if proc.returncode != 0:
            raise AssertionError(f"bridge submit failed: {proc.stderr}")
        return proc.stdout.strip().split("JOBID=", 1)[1].strip()

    def request(self, token: str = "0123456789abcdef",
                mr_url: str = MR_URL) -> dict:
        command_file = self.root / f"request-command-{token}.txt"
        proc = run([sys.executable, str(SCRIPTS / "dell_mirror_request.py"),
                    "--mr-url", mr_url, "--mapping-file", str(self.mapping),
                    "--token", token, "--shared", str(self.shared),
                    "--lenovo-checkout", str(self.lenovo),
                    "--lenovo-driver-dir", str(SCRIPTS),
                    "--lenovo-flow-dir", str(TESTS_DIR),
                    "--python", sys.executable,
                    "--request-name", f"mirror-request-{token}.json",
                    "--command-file", str(command_file)], env=self.env())
        if proc.returncode != 0:
            raise AssertionError(f"mirror request failed: {proc.stdout} {proc.stderr}")
        data = json.loads(proc.stdout)
        data["commandFile"] = command_file
        return data

    def export_job(self, token: str = "0123456789abcdef",
                   mr_url: str = MR_URL) -> str:
        data = self.request(token, mr_url)
        return self.submit(data["commandFile"],
                           return_file=Path(data["returnFile"]))

    def import_mr(self, job_id: str, token: str = "0123456789abcdef",
                  *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
        receipt = self.root / f"receipt-{token}.json"
        state = self.root / f"state-{token}.json"
        proc = run([sys.executable, str(SCRIPTS / "dell_mirror_import.py"),
                    "--queue", str(self.queue), "--key-file", str(self.key),
                    "--job-id", job_id, "--shared", str(self.shared),
                    "--mirror-dir", str(self.mirror),
                    "--mapping-file", str(self.mapping),
                    "--mr-url", MR_URL, "--state", str(state),
                    "--receipt", str(receipt),
                    "--fetch-timeout", "3", "--poll", "0.05",
                    "--github-dir", str(TESTS_DIR), *extra], env=self.env())
        data = json.loads(receipt.read_text(encoding="utf-8")) \
            if receipt.is_file() else None
        return proc, data

    def teardown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
