#!/usr/bin/env python3
"""Dell side: write one mirror request for an open GitLab MR (issue #142).

Validates the MR URL, the repository-to-mirror mapping, and the request
file layout, then writes the request JSON on the shared LAN path and the
one command file the signed bridge seals. The request carries SHAs only
after Lenovo observes them; this step carries names only, never secrets.

Writes files only. It never submits a job, contacts GitLab or GitHub, and
never publishes anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dell_lan"))

from mirror_mr_common import (REQUEST_SCHEMA, TOKEN_RE, Stop, load_mapping,
                              parse_mr_url, resolve_mirror)
from dell_lan_common import DETAIL_MAX, command_line, inside, safe_name, sha256_bytes  # noqa: E402


def parse(argv=None):
    parser = argparse.ArgumentParser(
        prog="dell_mirror_request.py", description=__doc__)
    parser.add_argument("--mr-url", required=True,
                        help="https://<host>/<group/project>/-/merge_requests/<iid>")
    parser.add_argument("--mapping-file", required=True,
                        help="mirror-mapping/1 JSON: GitLab project -> private GitHub mirror")
    parser.add_argument("--token", required=True, help="16 lowercase hex request token")
    parser.add_argument("--shared", required=True,
                        help="owner-provided LAN share that hosts the queue")
    parser.add_argument("--lenovo-checkout", required=True,
                        help="absolute Lenovo checkout to observe and export from")
    parser.add_argument("--lenovo-driver-dir", default=None,
                        help="absolute dir holding the Lenovo drivers "
                             "(default: this script's directory)")
    parser.add_argument("--python", required=True,
                        help="absolute interpreter path on the Lenovo machine")
    parser.add_argument("--lenovo-flow-dir", default=None,
                        help="absolute GitLab reader seam dir on Lenovo "
                             "(default: the driver's glab fallback)")
    parser.add_argument("--out-dir", default=None,
                        help="where to write the request (default: the shared dir)")
    parser.add_argument("--request-name", required=True,
                        help="request file name on the shared dir")
    parser.add_argument("--command-file", required=True,
                        help="UTF-8 command file for the bridge")
    parser.add_argument("--repo", default=None,
                        help="Dell checkout that must not contain the shared dir")
    return parser.parse_args(argv)


def validated(args) -> tuple[dict, Path, Path]:
    if not TOKEN_RE.match(args.token or ""):
        raise Stop("usage", code=2, detail="--token must be 16 lowercase hex")
    meta = parse_mr_url(args.mr_url)
    mapping = load_mapping(Path(args.mapping_file))
    mirror = resolve_mirror(mapping, meta["host"], meta["project"])
    shared = Path(args.shared)
    if not shared.is_dir():
        raise Stop("usage", code=2, detail=f"shared directory does not exist: {shared}")
    if args.repo and inside(shared, Path(args.repo)):
        raise Stop("usage", code=2, detail="shared directory must stay outside the checkout")
    out_dir = Path(args.out_dir) if args.out_dir else shared
    if not inside(out_dir, shared):
        raise Stop("usage", code=2, detail="request file must live on the shared LAN path")
    for label, value in (("--lenovo-checkout", args.lenovo_checkout),
                         ("--python", args.python)):
        if not value or not Path(value).is_absolute():
            raise Stop("usage", code=2,
                       detail=f"{label} must be the absolute path on the Lenovo machine")
    if args.lenovo_flow_dir and not Path(args.lenovo_flow_dir).is_absolute():
        raise Stop("usage", code=2, detail="--lenovo-flow-dir must be absolute on Lenovo")
    driver_dir = Path(args.lenovo_driver_dir) if args.lenovo_driver_dir \
        else Path(__file__).resolve().parent
    if not Path(args.lenovo_driver_dir or ".").is_absolute() and args.lenovo_driver_dir:
        raise Stop("usage", code=2, detail="--lenovo-driver-dir must be absolute on Lenovo")
    if not safe_name(args.request_name):
        raise Stop("usage", code=2, detail="--request-name must be one contained file name")
    token = args.token
    request = {
        "schema": REQUEST_SCHEMA,
        "token": token,
        "mrUrl": meta["url"],
        "gitlabHost": meta["host"],
        "gitlabProject": meta["project"],
        "mrIid": meta["iid"],
        "mirrorRepo": mirror,
        "shared": str(shared),
        "lenovoCheckout": str(Path(args.lenovo_checkout)),
        "bundleName": f"mirror-mr-{token}.bundle",
        "manifestName": f"mirror-mr-{token}.manifest.json",
    }
    command = command_line([
        args.python, str(driver_dir / "lenovo_mr_export.py"),
        "--request", str(out_dir / args.request_name),
        "--request-sha256", sha256_bytes((json.dumps(
            request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")),
    ] + (["--flow-dir", args.lenovo_flow_dir] if args.lenovo_flow_dir else []))
    return request, out_dir / args.request_name, Path(args.command_file), command


def main(argv=None) -> int:
    try:
        args = parse(argv)
        request, request_path, command_file, command = validated(args)
    except Stop as stop:
        print(f"dell mirror request {stop.reason}: {stop.detail}", file=sys.stderr)
        return stop.code
    except OSError as error:
        print(f"dell mirror request io-error: {error}"[:DETAIL_MAX], file=sys.stderr)
        return 1
    text = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_bytes(text.encode("utf-8"))
        command_file.parent.mkdir(parents=True, exist_ok=True)
        command_file.write_bytes((command + "\n").encode("utf-8"))
    except OSError as error:
        print(f"dell mirror request io-error: {error}"[:DETAIL_MAX], file=sys.stderr)
        return 1
    print(json.dumps({
        "schema": "dell-mirror-request/1", "kind": REQUEST_SCHEMA,
        "requestPath": str(request_path), "commandFile": str(command_file),
        "command": command,
        "returnFile": str(Path(request["shared"]) / request["manifestName"]),
        "mrUrl": request["mrUrl"], "mirrorRepo": request["mirrorRepo"],
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
