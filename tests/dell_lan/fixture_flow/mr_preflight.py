#!/usr/bin/env python3
"""Fixture double for the publication freshness gate (tests only).

Genuinely checks freshness with Git only: the local ``--base`` ref in
``--repo`` must equal the same branch head observed on ``--remote`` via
``git ls-remote``. Prints the JSON receipt the Lenovo publish driver consumes
(``freshness``, ``target_sha``, ``stop_reasons``); exit ``0`` on fresh, ``3``
on stale or unreadable so the driver reports ``preflight-blocked`` instead of
publishing. No network beyond the configured remote (a local path in tests).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def run_git(*args: str, cwd: str | None = None) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False)
    output = (proc.stdout if proc.returncode == 0
              else (proc.stderr or proc.stdout)).strip()
    return proc.returncode == 0, output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mr_preflight.py")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--format", required=True)
    args = parser.parse_args(argv)
    ok, local = run_git("rev-parse", "--verify", args.base, cwd=args.repo)
    if not ok or len(local) != 40:
        print(json.dumps({"freshness": "unknown", "target_sha": None,
                          "stop_reasons": ["base-unreadable"]},
                         ensure_ascii=False, separators=(",", ":")))
        return 3
    ok, url = run_git("config", "--get", f"remote.{args.remote}.url",
                      cwd=args.repo)
    if not ok or not url:
        print(json.dumps({"freshness": "unknown", "target_sha": local,
                          "stop_reasons": ["remote-unreadable"]},
                         ensure_ascii=False, separators=(",", ":")))
        return 3
    ok, listed = run_git("ls-remote", url, f"refs/heads/{args.base}")
    remote = listed.split()[0].lower() if ok and listed.split() else None
    if remote and remote == local.lower():
        print(json.dumps({"freshness": "fresh", "target_sha": remote,
                          "stop_reasons": []},
                         ensure_ascii=False, separators=(",", ":")))
        return 0
    print(json.dumps({"freshness": "stale", "target_sha": remote,
                      "stop_reasons": ["target-moved"]},
                     ensure_ascii=False, separators=(",", ":")))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
