"""External-flow stubs shared by the Ready consent tests."""

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
        rows = []
        for r in state.get("mrs", {}).values():
            if not isinstance(r, dict):
                continue
            row = {"iid": r["iid"], "source_branch": r["source_branch"],
                   "target_branch": r["target_branch"], "web_url": r["web_url"],
                   "draft": bool(r.get("draft", True)), "state": "opened"}
            if "description" in r:
                row["description"] = r["description"]
            rows.append(row)
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
