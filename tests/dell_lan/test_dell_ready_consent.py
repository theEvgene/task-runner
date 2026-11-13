"""Consented Ready route: undraft consent, title/description proof, no pipeline wait.

Fast unit tests only: the external flow (``gitlab_api``, ``publish_mr``,
``mr_identity``) and the ``remove_draft`` step are stubbed in a temp flow
dir, so no Git repository, queue, or network is touched. Each case proves
one issue-#37 criterion at the cheapest level that can show it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "dell_lan"
sys.path.insert(0, str(SCRIPTS))

import lenovo_publish  # noqa: E402
import lenovo_remote  # noqa: E402
from dell_lan_common import Stop  # noqa: E402

REQUEST_CLI = SCRIPTS / "dell_request.py"
HOST = "gitlab.fixture.invalid"
PROJECT = "group/project"
BRANCH = "task/iteration-1"
BASE_BRANCH = "main"
TASK = "b" * 40
DESCRIPTION = "Фикстура: снятие Draft по согласию.\nASCII line\n"

GITLAB_API_STUB = '''\
import json, os
from pathlib import Path
from urllib.parse import parse_qsl

class GitlabUnavailable(Exception):
    pass

def _path():
    return Path(os.environ["STUB_MR_STATE"])

def _state():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GitlabUnavailable(str(error))

def project_api_base(host, project):
    from urllib.parse import quote
    return "projects/" + quote(project, safe="")

def merge_request_url(host, project, iid):
    return "https://%s/%s/-/merge_requests/%s" % (host, project, iid)

def api_get(host, path):
    state = _state()
    if host != state.get("host"):
        raise GitlabUnavailable("unknown host")
    head, _, query = path.partition("?")
    base = project_api_base(host, state.get("project")) + "/merge_requests"
    if not head.startswith(base):
        raise GitlabUnavailable("unsupported path")
    suffix = head[len(base):]
    if suffix == "" or suffix.startswith("?"):
        rows = [{"iid": r["iid"], "source_branch": r["source_branch"],
                 "target_branch": r["target_branch"], "web_url": r["web_url"],
                 "draft": bool(r.get("draft", True)), "state": "opened"}
                for r in state.get("mrs", {}).values() if isinstance(r, dict)]
        params = dict(parse_qsl(query))
        if "page" in params or "per_page" in params:
            per_page = int(params.get("per_page", 100))
            page = int(params.get("page", 1))
            rows = rows[(page - 1) * per_page:page * per_page]
        return rows
    if suffix.startswith("/") and suffix[1:].isdigit():
        record = state.get("mrs", {}).get(suffix[1:])
        if not isinstance(record, dict):
            raise GitlabUnavailable("unknown MR")
        return {"iid": int(record["iid"]), "source_branch": record["source_branch"],
                "target_branch": record["target_branch"], "web_url": record["web_url"],
                "draft": bool(record.get("draft", True)), "title": record.get("title", ""),
                "description": record.get("description", ""), "state": "opened"}
    raise GitlabUnavailable("unsupported path")
'''

PUBLISH_MR_STUB = '''\
import json, os
from pathlib import Path

class PublicationError(Exception):
    pass

def publish(*, host, project, change_type, key, subject, description,
            desired_state, no_test_results, source_branch, target_branch,
            mr_url, replace_description):
    path = Path(os.environ["STUB_MR_STATE"])
    state = json.loads(path.read_text(encoding="utf-8"))
    if host != state.get("host") or project != state.get("project"):
        raise PublicationError("unknown target")
    head = os.environ["STUB_HEAD"]
    title = "Draft: %s(%s): %s" % (change_type, key, subject)
    if mr_url:
        record = next((c for c in state.get("mrs", {}).values()
                       if isinstance(c, dict) and c.get("web_url") == mr_url), None)
        if record is None:
            raise PublicationError("unknown MR")
        record["title"] = title
        if replace_description:
            record["description"] = description
        else:
            record["description"] = (record.get("description") or "") + "\\n" + description
        record["head_sha"] = head
    else:
        iid = int(state.get("nextIid", 1))
        state["nextIid"] = iid + 1
        record = {"iid": iid, "source_branch": source_branch,
                  "target_branch": target_branch, "title": title,
                  "description": description, "draft": desired_state == "draft",
                  "head_sha": head, "target_sha": "c" * 40,
                  "web_url": "https://%s/%s/-/merge_requests/%s" % (host, project, iid)}
        state.setdefault("mrs", {})[str(iid)] = record
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    return {"mr_url": record["web_url"], "iid": record["iid"],
            "draft": bool(record.get("draft", True))}
'''

MR_IDENTITY_STUB = '''\
import os

class IdentityError(Exception):
    pass

class ContextMismatch(Exception):
    pass

def capture_identity(mr_url, _args, worktree, _extra):
    return {"schema_version": 1, "head_sha": os.environ["STUB_HEAD"].lower(),
            "mr_url": mr_url, "local": None}
'''

REMOVE_DRAFT_STUB = '''\
import argparse, json, os, sys
from pathlib import Path

parser = argparse.ArgumentParser(prog="remove_draft.py")
parser.add_argument("--mr-url", "--url", dest="mr_url", required=True)
parser.add_argument("--host", required=False, default=None)
parser.add_argument("--project", required=False, default=None)
args = parser.parse_args()
mode = os.environ.get("STUB_UNDRAFT_MODE", "ok")
path = Path(os.environ["STUB_MR_STATE"])
state = json.loads(path.read_text(encoding="utf-8"))
record = next((c for c in state.get("mrs", {}).values()
               if isinstance(c, dict) and c.get("web_url") == args.mr_url), None)
if record is None:
    print("stub undraft refused: unknown MR", file=sys.stderr)
    sys.exit(1)
if mode == "fail":
    print("stub undraft failed", file=sys.stderr)
    sys.exit(1)
if mode == "rewrite-title":
    record["title"] = "CORRUPTED"
    record["draft"] = False
elif mode == "rewrite-description":
    record["description"] = "CORRUPTED"
    record["draft"] = False
elif mode == "rewrite-prefix":
    record["description"] = "Earlier attacker text.\\n" + str(record.get("description") or "")
    record["draft"] = False
else:
    title = str(record.get("title") or "")
    if title.startswith("Draft: "):
        record["title"] = title[len("Draft: "):]
    record["draft"] = False
path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
print(json.dumps({"mr_url": args.mr_url, "iid": record["iid"], "draft": False}))
'''


class ReadyConsentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dell-ready-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = self.root / "lan-share"
        self.shared.mkdir()
        self.flow = self.root / "flow"
        self.flow.mkdir()
        self.worktree = self.root / "lenovo-task-worktree"
        self.worktree.mkdir()
        self.state = self.root / "mr-state.json"
        self.description = self.shared / "mr-description.md"
        self.description.write_text(DESCRIPTION, encoding="utf-8")
        self._saved_env = {name: os.environ.get(name) for name in
                           ("STUB_MR_STATE", "STUB_HEAD", "STUB_UNDRAFT_MODE")}
        os.environ["PYTHONUTF8"] = "1"
        os.environ["STUB_MR_STATE"] = str(self.state)
        os.environ["STUB_HEAD"] = TASK
        os.environ.pop("STUB_UNDRAFT_MODE", None)
        self.addCleanup(self._restore_env)
        self._saved_modules = {name: sys.modules.get(name) for name in
                               ("gitlab_api", "mr_identity", "publish_mr")}
        self._saved_path = list(sys.path)
        self.addCleanup(self._restore_modules)

    def _restore_env(self) -> None:
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _restore_modules(self) -> None:
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.path[:] = self._saved_path

    def _write_flow(self, with_undraft: bool = True) -> None:
        (self.flow / "gitlab_api.py").write_text(GITLAB_API_STUB, encoding="utf-8")
        (self.flow / "publish_mr.py").write_text(PUBLISH_MR_STUB, encoding="utf-8")
        (self.flow / "mr_identity.py").write_text(MR_IDENTITY_STUB, encoding="utf-8")
        if with_undraft:
            (self.flow / "remove_draft.py").write_text(REMOVE_DRAFT_STUB, encoding="utf-8")

    def _seed(self, mrs: dict | None = None) -> None:
        self.state.write_text(json.dumps(
            {"host": HOST, "project": PROJECT, "nextIid": 1, "mrs": mrs or {}},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _request(self, state: str, authorized: list[str]) -> dict:
        return {"schema": "dell-lan-publish-request/1", "token": "a" * 16,
                "shared": str(self.shared), "manifest": str(self.shared / "t.bundle"),
                "bundle": str(self.shared / "t.bundle"), "expectedBaseSha": "a" * 40,
                "expectedCommit": TASK, "expectedPreviousCommit": None,
                "branch": BRANCH, "targetBranch": BASE_BRANCH, "host": HOST,
                "project": PROJECT, "changeType": "feat", "jiraKey": "ABC-1",
                "subject": "fixture task", "descriptionFile": str(self.description),
                "state": state, "lenovoCheckout": str(self.root / "lenovo-checkout"),
                "lenovoWorktree": str(self.worktree), "reconcileOnly": False,
                "authorized": authorized}

    def _writer(self, state: str, authorize: str) -> subprocess.CompletedProcess:
        (self.shared / "transfer.manifest.json").write_text("{}", encoding="utf-8")
        (self.shared / "transfer.bundle").write_bytes(b"bundle")
        command = self.root / "command.txt"
        return subprocess.run(
            [sys.executable, str(REQUEST_CLI), "publish", "--shared", str(self.shared),
             "--token", "0123456789abcdef", "--lenovo-skill-dir", str(self.root),
             "--python", sys.executable,
             "--manifest", str(self.shared / "transfer.manifest.json"),
             "--bundle", str(self.shared / "transfer.bundle"),
             "--description-file", str(self.description),
             "--expected-base", "a" * 40, "--expected-commit", TASK,
             "--branch", BRANCH, "--target-branch", BASE_BRANCH,
             "--host", HOST, "--project", PROJECT, "--change-type", "feat",
             "--jira-key", "ABC-1", "--subject", "fixture task",
             "--state", state, "--authorize", authorize,
             "--lenovo-checkout", str(self.root / "lenovo-checkout"),
             "--lenovo-worktree", str(self.worktree),
             "--request-name", "publish-request.json", "--command-file", str(command)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False)

    def test_ready_without_undraft_is_refused_by_the_writer(self):
        proc = self._writer("ready", "import,push,mr")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("undraft", proc.stderr)
        self.assertFalse((self.root / "command.txt").exists())

    def test_ready_with_undraft_is_accepted_by_the_writer(self):
        proc = self._writer("ready", "import,push,mr,undraft")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        request = json.loads(Path(data["requestPath"]).read_text(encoding="utf-8"))
        self.assertIn("undraft", request["authorized"])

    def test_driver_refuses_ready_without_undraft_before_any_write(self):
        request = self._request("ready", ["import", "push", "mr"])
        path = self.root / "publish-request.json"
        text = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
        path.write_bytes(text.encode("utf-8"))
        import hashlib
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.assertRaises(Stop) as caught:
            lenovo_publish.validated(path, digest, False)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(caught.exception.reason, "consent-missing")
        self.assertIn("undraft", caught.exception.detail)

    def test_draft_never_runs_the_undraft_step(self):
        self._write_flow(with_undraft=False)
        self._seed()
        result: dict = {}
        receipt = lenovo_remote.publish(
            self._request("draft", ["import", "push", "mr"]),
            self.worktree, self.flow, result)
        self.assertEqual(receipt["head_sha"], TASK)
        self.assertTrue(result["mr"]["draft"])
        self.assertIsNone(result["undraft"])
        self.assertEqual(result["pipeline"], {"waited": False, "polled": False})

    def test_ready_marks_the_same_mr_ready_and_proves_title_and_description(self):
        self._write_flow(with_undraft=True)
        self._seed()
        result: dict = {}
        receipt = lenovo_remote.publish(
            self._request("ready", ["import", "push", "mr", "undraft"]),
            self.worktree, self.flow, result)
        self.assertEqual(receipt["head_sha"], TASK)
        self.assertFalse(result["mr"]["draft"])
        self.assertTrue(result["mr"]["created"])
        self.assertEqual(result["undraft"]["descriptionCheck"], "exact")
        self.assertEqual(result["undraft"]["title"], "feat(ABC-1): fixture task")
        self.assertEqual(result["pipeline"], {"waited": False, "polled": False})
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1)
        record = state["mrs"]["1"]
        self.assertFalse(record["draft"])
        self.assertEqual(record["title"], "feat(ABC-1): fixture task")
        self.assertEqual(record["description"], DESCRIPTION)

    def test_ready_update_reuses_the_existing_mr_without_a_second_one(self):
        self._write_flow(with_undraft=True)
        self._seed({"1": {
            "iid": 1, "source_branch": BRANCH, "target_branch": BASE_BRANCH,
            "title": "Draft: feat(ABC-1): fixture task",
            "description": "Earlier draft text.\n", "draft": True,
            "head_sha": TASK, "target_sha": "c" * 40,
            "web_url": f"https://{HOST}/{PROJECT}/-/merge_requests/1"}})
        result: dict = {}
        receipt = lenovo_remote.publish(
            self._request("ready", ["import", "push", "mr", "undraft"]),
            self.worktree, self.flow, result)
        self.assertEqual(receipt["head_sha"], TASK)
        self.assertFalse(result["mr"]["draft"])
        self.assertFalse(result["mr"]["created"])
        self.assertEqual(result["undraft"]["descriptionCheck"], "exact")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(len(state["mrs"]), 1, "no second MR for one branch")
        self.assertEqual(state["mrs"]["1"]["description"], DESCRIPTION)
        self.assertEqual(state["mrs"]["1"]["title"], "feat(ABC-1): fixture task")
        self.assertFalse(state["mrs"]["1"]["draft"])

    def test_draft_update_preserves_earlier_text(self):
        self._write_flow(with_undraft=True)
        self._seed({"1": {
            "iid": 1, "source_branch": BRANCH, "target_branch": BASE_BRANCH,
            "title": "Draft: feat(ABC-1): fixture task",
            "description": "Earlier draft text.\n", "draft": True,
            "head_sha": TASK, "target_sha": "c" * 40,
            "web_url": f"https://{HOST}/{PROJECT}/-/merge_requests/1"}})
        result: dict = {}
        lenovo_remote.publish(
            self._request("draft", ["import", "push", "mr"]),
            self.worktree, self.flow, result)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(state["mrs"]["1"]["description"].startswith("Earlier draft text.\n"))
        self.assertNotEqual(state["mrs"]["1"]["description"], DESCRIPTION)
        self.assertTrue(result["mr"]["draft"])

    def test_publish_gates_undraft_before_any_flow_read(self):
        self._write_flow(with_undraft=True)
        self._seed()
        with self.assertRaises(Stop) as caught:
            lenovo_remote.publish(
                self._request("ready", ["import", "push", "mr"]),
                self.worktree, self.flow, {})
        self.assertEqual(caught.exception.reason, "consent-missing")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["mrs"], {})

    def test_undraft_title_mismatch_is_unknown_never_success(self):
        self._write_flow(with_undraft=True)
        self._seed()
        with unittest.mock.patch.dict(os.environ, {"STUB_UNDRAFT_MODE": "rewrite-title"}):
            with self.assertRaises(Stop) as caught:
                lenovo_remote.publish(
                    self._request("ready", ["import", "push", "mr", "undraft"]),
                    self.worktree, self.flow, {})
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(caught.exception.reason, "readback-mismatch")

    def test_undraft_failure_is_unknown_never_success(self):
        self._write_flow(with_undraft=True)
        self._seed()
        with unittest.mock.patch.dict(os.environ, {"STUB_UNDRAFT_MODE": "fail"}):
            with self.assertRaises(Stop) as caught:
                lenovo_remote.publish(
                    self._request("ready", ["import", "push", "mr", "undraft"]),
                    self.worktree, self.flow, {})
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(caught.exception.reason, "undraft-unknown")

    def test_undraft_description_prefix_is_unknown_never_success(self):
        self._write_flow(with_undraft=True)
        self._seed()
        with unittest.mock.patch.dict(os.environ, {"STUB_UNDRAFT_MODE": "rewrite-prefix"}):
            with self.assertRaises(Stop) as caught:
                lenovo_remote.publish(
                    self._request("ready", ["import", "push", "mr", "undraft"]),
                    self.worktree, self.flow, {})
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(caught.exception.reason, "readback-mismatch")

    def test_ready_runs_no_pipeline_observer(self):
        """Consented Ready publishes with zero pipeline-observer invocations.

        The publish path has no observer seam by design: the only ways it
        could invoke one are a module import (``load_flow``) or a subprocess
        (``run``). Both seams are counted here at runtime, so a future
        change that imports or executes the observer fails this test.
        """
        self._write_flow(with_undraft=True)
        self._seed()
        calls: list = []
        import importlib as _imports
        import subprocess as _subprocess
        import dell_lan_common as _common
        import lenovo_publish_flow as _flow

        def _note(kind: str, text: str) -> None:
            lowered = text.lower()
            if ("pipeline_observer" in lowered or "pipeline-observer" in lowered
                    or "probe.py" in lowered or lowered == "probe"):
                calls.append((kind, text))

        real_import = _imports.import_module

        def _import(name, *args, **kwargs):
            _note("import", str(name))
            return real_import(name, *args, **kwargs)

        real_run = _common.run

        def _run(command, *args, **kwargs):
            _note("run", " ".join(str(part) for part in (command or [])))
            return real_run(command, *args, **kwargs)

        real_subprocess = _subprocess.run

        def _subprocess_run(command, *args, **kwargs):
            parts = command if isinstance(command, (list, tuple)) else [command]
            _note("run", " ".join(str(part) for part in parts))
            return real_subprocess(command, *args, **kwargs)

        with unittest.mock.patch.object(_imports, "import_module", _import), \
                unittest.mock.patch.object(_common, "run", _run), \
                unittest.mock.patch.object(lenovo_remote, "run", _run), \
                unittest.mock.patch.object(_flow, "run", _run), \
                unittest.mock.patch.object(_subprocess, "run", _subprocess_run):
            result: dict = {}
            lenovo_remote.publish(
                self._request("ready", ["import", "push", "mr", "undraft"]),
                self.worktree, self.flow, result)
        self.assertEqual(result["pipeline"], {"waited": False, "polled": False})
        self.assertEqual(calls, [], f"pipeline observer invoked: {calls!r}")


if __name__ == "__main__":
    unittest.main()
