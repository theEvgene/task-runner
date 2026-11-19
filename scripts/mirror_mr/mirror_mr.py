#!/usr/bin/env python3
"""Single entry: mirror one GitLab MR into a private GitHub PR (issue #144).

Thin orchestrator over the reviewed #142/#143 route scripts. It validates
the MR URL and mirror mapping, submits one signed job per attempt, polls
the SAME job id through the existing import drivers, and returns the
verified PR link. It adds no transport, MAC, bundle, or PR logic: every
guarantee stays in `dell_mirror_request.py`, `lan_bridge.py`,
`dell_mirror_import.py`, and `dell_mr_history_import.py`.

`--mode auto` (default) determines live status through the signed export:
the open attempt runs first; a sealed `mr-not-open` refusal retries once
as `merged` with a NEW token and job id. Explicit `--mode open|merged`
pins one route. An unknown outcome (exit 3) resumes the SAME id: rerun
the identical command with the identical `--state`.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mirror_mr_common import (DETAIL_MAX, Stop, load_mapping, parse_mr_url,
                              resolve_mirror)

CLI_SCHEMA = "mirror-mr-cli/1"
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
DETAIL = DETAIL_MAX


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="mirror_mr.py", description=__doc__)
    parser.add_argument("--mr-url", required=True)
    parser.add_argument("--mapping-file", required=True)
    parser.add_argument("--shared", required=True)
    parser.add_argument("--mirror-dir", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--key-format", default="raw", choices=("raw", "base64"))
    parser.add_argument("--lenovo-checkout", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--lenovo-driver-dir", default=None)
    parser.add_argument("--lenovo-flow-dir", default=None)
    parser.add_argument("--state", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--mode", default="auto", choices=("auto", "open", "merged"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--fetch-timeout", type=float, default=60.0)
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--submit-timeout", default="600")
    parser.add_argument("--max-output", default="65536")
    parser.add_argument("--provenance", default="fixture", choices=("fixture", "live"))
    parser.add_argument("--bridge-dir", default=None)
    parser.add_argument("--github-dir", default=None)
    return parser.parse_args(argv)


def fail(reason: str, detail: str, code: int = 1):
    raise Stop(reason, detail=detail, code=code)


def run(cmd: list, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, check=False)


def load_cli_state(path: Path, mr_url: str) -> dict:
    if not path.is_file():
        return {"schema": CLI_SCHEMA, "mrUrl": mr_url, "attempts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail("state-malformed", str(error)[:DETAIL])
    if not isinstance(data, dict) or data.get("schema") != CLI_SCHEMA:
        fail("state-malformed", f"expected {CLI_SCHEMA}")
    if data.get("mrUrl") != mr_url:
        fail("state-mismatch", "state names another MR")
    data.setdefault("attempts", {})
    return data


def save_cli_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False,
                               separators=(",", ":")) + "\n", encoding="utf-8")


def attempt_once(args, mode: str, token: str, env) -> tuple[int, dict | None]:
    here = Path(__file__).resolve().parent
    state_path = Path(args.state)
    import_state = state_path.parent / f"{state_path.stem}.import-{mode}.json"
    token = token.lower()
    request_name = f"mirror-request-{token}.json"
    command_file = state_path.parent / f"mirror-mr-command-{token}.txt"
    out_dir = str(Path(args.shared))
    req_cmd = [sys.executable, str(here / "dell_mirror_request.py"),
               "--mr-url", args.mr_url, "--mapping-file", args.mapping_file,
               "--token", token, "--shared", args.shared,
               "--lenovo-checkout", args.lenovo_checkout,
               "--python", args.python, "--mode", mode,
               "--request-name", request_name, "--command-file", str(command_file)]
    if args.lenovo_driver_dir:
        req_cmd += ["--lenovo-driver-dir", args.lenovo_driver_dir]
    else:
        req_cmd += ["--lenovo-driver-dir", str(here)]
    if args.lenovo_flow_dir:
        req_cmd += ["--lenovo-flow-dir", args.lenovo_flow_dir]
    if out_dir:
        req_cmd += ["--out-dir", out_dir]
    proc = run(req_cmd, env=env)
    if proc.returncode != 0:
        print(f"mirror-mr request refused: {proc.stderr.strip()}"[:DETAIL],
              file=sys.stderr)
        return proc.returncode, None
    try:
        facts = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        print(f"mirror-mr request unreadable: {error}"[:DETAIL], file=sys.stderr)
        return 1, None
    bridge = Path(args.bridge_dir) if args.bridge_dir else here.parent / "lan_bridge"
    # The command file carries the canonical PowerShell wrapper the
    # worker executes; --expected-cwd pins the digest-bound Lenovo
    # checkout as the only provable target. No --dialect: the legacy
    # gate-off path seals the exact command bytes either way, and the
    # wrapper verifies without requiring a PowerShell parser at submit.
    submit = [sys.executable, str(Path(bridge) / "lan_bridge.py"), "submit",
              "--queue", args.queue, "--key-file", args.key_file,
              "--key-format", args.key_format,
              "--command-file", str(command_file),
              "--cwd", args.lenovo_checkout,
              "--expected-cwd", str(facts.get("expectedCwd") or args.lenovo_checkout),
              "--timeout", args.submit_timeout,
              "--max-output", args.max_output,
              "--return-file", str(facts["returnFile"])]
    proc2 = run(submit, env=env)
    if proc2.returncode != 0:
        print(f"mirror-mr submit refused: {proc2.stderr.strip()}"[:DETAIL],
              file=sys.stderr)
        return proc2.returncode, None
    job_id = ""
    for chunk in proc2.stdout.replace("=", " ").split():
        if re.fullmatch(r"[0-9a-f]{32}", chunk or ""):
            job_id = chunk
    if "JOBID=" in proc2.stdout:
        job_id = proc2.stdout.split("JOBID=", 1)[1].strip().split()[0][:32]
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        print(f"mirror-mr submit returned no JOBID: {proc2.stdout.strip()}"[:DETAIL],
              file=sys.stderr)
        return 1, None
    importer = "dell_mirror_import.py" if mode == "open" else "dell_mr_history_import.py"
    imp_cmd = [sys.executable, str(here / importer),
               "--queue", args.queue, "--key-file", args.key_file,
               "--key-format", args.key_format, "--job-id", job_id,
               "--shared", args.shared, "--mirror-dir", args.mirror_dir,
               "--mapping-file", args.mapping_file, "--mr-url", args.mr_url,
               "--state", str(import_state), "--receipt", args.receipt,
               "--fetch-timeout", str(args.fetch_timeout), "--poll", str(args.poll),
               "--provenance", args.provenance]
    if args.bridge_dir:
        imp_cmd += ["--bridge-dir", args.bridge_dir]
    if args.github_dir:
        imp_cmd += ["--github-dir", args.github_dir]
    proc3 = run(imp_cmd, env=env)
    receipt = None
    try:
        receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        receipt = None
    sys.stdout.write(proc3.stdout)
    sys.stderr.write(proc3.stderr)
    return proc3.returncode, {"jobId": job_id, "receipt": receipt}


def main(argv=None) -> int:
    import os
    args = parse(argv)
    env = dict(os.environ)
    try:
        meta = parse_mr_url(args.mr_url)
        mapping = load_mapping(Path(args.mapping_file))
        mirror_repo = resolve_mirror(mapping, meta["host"], meta["project"])
        mirror = Path(args.mirror_dir)
        state_path = Path(args.state)
        receipt_path = Path(args.receipt)
        for label, value in (("--lenovo-checkout", args.lenovo_checkout),
                             ("--python", args.python)):
            if not value or not Path(value).is_absolute():
                fail("usage", f"{label} must be absolute on its host", code=2)
        if not mirror.is_absolute() or not state_path.is_absolute() \
                or not receipt_path.is_absolute():
            fail("usage", "--mirror-dir/--state/--receipt must be absolute", code=2)
        for path in (state_path, receipt_path):
            if path == mirror or mirror in path.parents:
                fail("usage", "--state/--receipt must stay outside the mirror", code=2)
        if args.token and not TOKEN_RE.match(args.token.lower()):
            fail("usage", "--token must be 16 lowercase hex", code=2)
        cli = load_cli_state(state_path, args.mr_url)
        modes = [args.mode] if args.mode in ("open", "merged") else ["open", "merged"]
        for index, mode in enumerate(modes):
            slot = cli["attempts"].get(mode) or {}
            token = str(slot.get("token") or args.token or secrets.token_hex(8)).lower()
            if not TOKEN_RE.match(token):
                fail("usage", "--token must be 16 lowercase hex", code=2)
            job_known = bool(slot.get("jobId"))
            if not job_known:
                # Fresh attempt: build, submit, then import (same id on resume).
                code, outcome = attempt_once(args, mode, token, env)
                if outcome and outcome.get("jobId"):
                    cli["attempts"][mode] = {"token": token,
                                             "jobId": outcome["jobId"]}
                    save_cli_state(state_path, cli)
                else:
                    # Request/submit refused before any job: nothing to resume.
                    return code
                receipt = (outcome or {}).get("receipt") or {}
            else:
                # Resume the SAME id: re-run only the import driver.
                here = Path(__file__).resolve().parent
                importer = "dell_mirror_import.py" if mode == "open" \
                    else "dell_mr_history_import.py"
                import_state = state_path.parent / f"{state_path.stem}.import-{mode}.json"
                imp_cmd = [sys.executable, str(here / importer),
                           "--queue", args.queue, "--key-file", args.key_file,
                           "--key-format", args.key_format,
                           "--job-id", slot["jobId"],
                           "--shared", args.shared, "--mirror-dir", args.mirror_dir,
                           "--mapping-file", args.mapping_file,
                           "--mr-url", args.mr_url,
                           "--state", str(import_state), "--receipt", args.receipt,
                           "--fetch-timeout", str(args.fetch_timeout),
                           "--poll", str(args.poll),
                           "--provenance", args.provenance]
                if args.bridge_dir:
                    imp_cmd += ["--bridge-dir", args.bridge_dir]
                if args.github_dir:
                    imp_cmd += ["--github-dir", args.github_dir]
                proc = run(imp_cmd, env=env)
                sys.stdout.write(proc.stdout)
                sys.stderr.write(proc.stderr)
                code = proc.returncode
                try:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    receipt = {}
            reason = str((receipt or {}).get("reason") or "")
            verdict = str((receipt or {}).get("verdict") or "")
            status = str((receipt or {}).get("status") or
                         ("opened" if mode == "open" else "merged"))
            if code == 0 and verdict in ("mirrored", "reconciled"):
                print(f"mirror-mr {mode} {verdict} : "
                      f"{(receipt or {}).get('prUrl')} (MR {status} "
                      f"{mirror_repo})", file=sys.stderr)
                return 0
            detail = str((receipt or {}).get("detail") or "")
            if code == 3:
                # Unknown: the slot stays; rerun the identical command.
                print(f"mirror-mr {mode} outcome-unknown : rerun the same "
                      f"command with the same --state", file=sys.stderr)
                return 3
            if args.mode == "auto" and index == 0 and mode == "open" \
                    and (reason == "mr-not-open"
                         or (reason == "export-job-failed"
                             and "mr-not-open" in detail)):
                # Live status is merged: one NEW job on the history route.
                print("mirror-mr auto: open route refused for a merged MR; "
                      "retrying once as merged with a new job id",
                      file=sys.stderr)
                continue
            return code
        return 1
    except Stop as stop:
        print(f"mirror-mr {stop.reason}: {stop.detail}"[:DETAIL], file=sys.stderr)
        return stop.code


if __name__ == "__main__":
    raise SystemExit(main())
