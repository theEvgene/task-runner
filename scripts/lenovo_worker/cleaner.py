"""Lenovo share cleaner for the single-shortcut launch (stdlib only).

Moves stale ``*.tmp`` files from the TOP LEVEL of the shared LAN folder into
a dedicated trash directory. It never deletes, never touches the queue
(``queue/jobs``, ``queue/results``), never touches ``*.json`` requests or
manifests, and never recurses. Trash may be created (it is dedicated and
local to this machine's view); the queue and the share never are.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CLEANER_SCHEMA = "lenovo-cleaner/1"


def _fail(message: str) -> int:
    print(f"cleaner refused: {message}", file=sys.stderr)
    return 2


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def run_once(shared: Path, trash: Path, max_age_s: float) -> dict:
    moved: list[str] = []
    skipped = 0
    now = time.time()
    try:
        entries = sorted(shared.iterdir())
    except OSError as error:
        print(f"cleaner: cannot list shared folder ({error})", file=sys.stderr)
        return {"moved": moved, "skipped": skipped, "error": str(error)[:200]}
    try:
        trash.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"cleaner: cannot create trash ({error})", file=sys.stderr)
        return {"moved": moved, "skipped": skipped, "error": str(error)[:200]}
    for entry in entries:
        if entry.name in ("queue",):
            continue  # the signed queue is never a cleaning target
        if not entry.is_file() or entry.is_symlink():
            skipped += 1
            continue
        if not (entry.name.endswith(".tmp") or entry.name.startswith(".")):
            continue  # only temp files; requests/manifests/bundles stay
        try:
            if now - entry.stat().st_mtime < max_age_s:
                skipped += 1
                continue
        except OSError:
            skipped += 1
            continue
        target = trash / f"{entry.name}.{os.getpid()}.{len(moved)}"
        try:
            os.replace(str(entry), str(target))
            moved.append(entry.name)
        except OSError as error:
            print(f"cleaner: cannot move {entry.name} ({error})", file=sys.stderr)
            skipped += 1
    return {"moved": moved, "skipped": skipped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cleaner.py", description="Lenovo share cleaner")
    parser.add_argument("--shared", required=True)
    parser.add_argument("--trash-dir", required=True)
    parser.add_argument("--max-age", type=float, default=3600.0)
    parser.add_argument("--once", action="store_true", help="single pass, then exit 0")
    parser.add_argument("--interval", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    shared, trash = Path(args.shared), Path(args.trash_dir)
    if not shared.is_dir():
        return _fail(f"shared folder unavailable at {args.shared}: check path/mount")
    queue = shared / "queue"
    if queue.exists() and _is_inside(trash, queue):
        return _fail("trash inside the queue: name a trash folder outside queue/")
    if not 0 <= float(args.max_age) <= 30 * 24 * 3600:
        return _fail("max-age out of range")
    import json

    if args.once:
        result = run_once(shared, trash, float(args.max_age))
        print(json.dumps({"schema": CLEANER_SCHEMA, "mode": "once", **result}))
        return 0 if "error" not in result else 2
    if not 1 <= float(args.interval) <= 3600:
        return _fail("interval must be within [1, 3600] seconds")
    while True:
        result = run_once(shared, trash, float(args.max_age))
        print(json.dumps({"schema": CLEANER_SCHEMA, "mode": "loop",
                          "moved": len(result["moved"]),
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
              flush=True)
        time.sleep(float(args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
