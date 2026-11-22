"""One-command Lenovo queue/worker screen (stdlib only).

Reads the existing signed queue and launcher state and reprints the
``queue_status`` screen every ``--interval`` seconds. Read-only: no stop,
no restart, no retry, no recovery, no telemetry. Only the watched queue
path, job ids, states, counts and timings appear in output: no command,
working directory, command output, return-file content, key, password or
host name.

Usage (one command on Lenovo, same config file as the launcher)::

    python queue_screen.py --config %LOCALAPPDATA%\\task-runner\\lenovo-worker.json

or with explicit paths::

    python queue_screen.py --queue <queueDir> --key-file <keyFile>
        [--key-format raw|base64] [--state-dir <stateDir>]
        [--interval 5] [--recent 10] [--once] [--json]

Exit codes: 0 = screen shown (even with connection lost: the loss itself is
the truthful result); 2 = refused (bad config/key/args: nothing verifiable).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lan_bridge"))

import queue_status as model  # noqa: E402


def _fail(message: str) -> int:
    print(f"screen refused: {message}", file=sys.stderr)
    return 2


def _load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read config: {error}")
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="queue_screen.py",
        description="Local auto-refreshing Lenovo queue/worker screen")
    parser.add_argument("--config", default=None,
                        help="launcher JSON config (queueDir/keyFile/...)")
    parser.add_argument("--queue", default=None)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--key-format", default=None,
                        choices=("raw", "base64"))
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--interval", type=float, default=5.0,
                        help="refresh seconds for watch mode (1..300)")
    parser.add_argument("--recent", type=int, default=10,
                        help="recent results shown (0..50)")
    parser.add_argument("--once", action="store_true",
                        help="print once instead of watching")
    parser.add_argument("--json", action="store_true",
                        help="print the snapshot JSON instead of text")
    return parser


def resolve(args: argparse.Namespace) -> tuple:
    queue, key_file, key_format, state_dir = (
        args.queue, args.key_file, args.key_format, args.state_dir)
    if args.config:
        config = _load_config(Path(args.config))
        queue = queue or config.get("queueDir")
        key_file = key_file or config.get("keyFile")
        key_format = key_format or config.get("keyFormat", "raw")
        state_dir = state_dir or config.get("stateDir")
    key_format = key_format or "raw"
    if not queue or not key_file:
        raise ValueError("queue and key file are required "
                         "(--queue/--key-file or --config)")
    if key_format not in ("raw", "base64"):
        raise ValueError("key_format must be raw or base64")
    if (isinstance(args.interval, bool)
            or not 1 <= float(args.interval) <= 300):
        raise ValueError("interval must be within [1, 300] seconds")
    if (isinstance(args.recent, bool)
            or not 0 <= int(args.recent) <= 50):
        raise ValueError("recent must be within [0, 50]")
    return (Path(queue), Path(key_file), key_format,
            Path(state_dir) if state_dir else None)


def show_once(queue: Path, key: bytes, state_dir: Path | None,
              recent: int, as_json: bool,
              interval: float | None = None) -> None:
    snapshot = model.build_snapshot(queue, key, state_dir,
                                    recent_limit=int(recent))
    if as_json:
        print(model.snapshot_json(snapshot))
    else:
        print(model.render_text(snapshot, interval_s=interval))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        queue, key_file, key_format, state_dir = resolve(args)
        key = model._bridge.load_key(
            str(key_file), key_format, str(queue))
    except ValueError as error:
        return _fail(str(error))
    except Exception as error:
        return _fail(f"key unusable ({error}): check key file and queue path")
    try:
        if args.once:
            show_once(queue, key, state_dir, args.recent, args.json)
            return 0
        if args.json:
            show_once(queue, key, state_dir, args.recent, True)
            return 0
        while True:
            print("\n" + "=" * 72)
            show_once(queue, key, state_dir, args.recent, False,
                      interval=args.interval)
            time.sleep(float(args.interval))
    except KeyboardInterrupt:
        print("\nscreen stopped (queue untouched).")
        return 0
    except Exception as error:  # queue vanished mid-watch: stay truthful
        print(f"\nscreen lost the queue ({error}); re-run to resume.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
