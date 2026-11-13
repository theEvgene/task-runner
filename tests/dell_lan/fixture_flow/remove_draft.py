"""Fixture double for the undraft seam (tests only, never production).

Implements the real ``remove_draft.py`` CLI the Lenovo publish driver consumes
to mark a fixture MR Ready: ``remove_draft.py --mr-url <url> [--host <host>]
[--project <project>]`` (no ``--repo`` flag, matching the real script).
It strips one leading ``Draft: `` prefix from the record title, marks the
record ``draft: false``, and preserves the description and both SHAs
byte-for-byte. Records persist in the fixture JSON state file. A zero exit
means the same MR was marked Ready; anything else fails closed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _state_path() -> Path:
    path = os.environ.get("DELL_FIXTURE_STATE", "")
    if not path:
        print("remove_draft fixture refused: DELL_FIXTURE_STATE is not set",
              file=sys.stderr)
        raise SystemExit(1)
    return Path(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="remove_draft.py")
    parser.add_argument("--mr-url", "--url", dest="mr_url", required=True)
    parser.add_argument("--host", required=False, default=None)
    parser.add_argument("--project", required=False, default=None)
    args = parser.parse_args(argv)
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"remove_draft fixture refused: unreadable fixture state: {error}",
              file=sys.stderr)
        return 1
    record = None
    for candidate in state.get("mrs", {}).values():
        if isinstance(candidate, dict) and candidate.get("web_url") == args.mr_url:
            record = candidate
            break
    if record is None:
        print(f"remove_draft fixture refused: unknown fixture MR: {args.mr_url}",
              file=sys.stderr)
        return 1
    title = str(record.get("title") or "")
    if title.startswith("Draft: "):
        record["title"] = title[len("Draft: "):]
    record["draft"] = False
    try:
        _state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"remove_draft fixture refused: cannot persist state: {error}",
              file=sys.stderr)
        return 1
    print(json.dumps({"mr_url": record["web_url"], "iid": record["iid"],
                      "draft": False}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
