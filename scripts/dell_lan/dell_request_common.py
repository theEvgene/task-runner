"""Small CLI helpers shared by Dell request routes."""

from __future__ import annotations

import argparse
from pathlib import Path

from dell_lan_common import Stop
from shell_command import build_for_transport

RECONCILE_DRIVERS = {
    "lenovo_publish.py",
    "lenovo_publish_existing.py",
    "lenovo_transfer.py",
}


class SingleDialect(argparse.Action):
    """Accept exactly one explicit dialect declaration."""

    def __call__(self, parser, namespace, value, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} must be declared exactly once")
        setattr(namespace, self.dest, value)


def require_absolute_lenovo_paths(*pairs: tuple[str, str]) -> None:
    for label, value in pairs:
        if not value or not Path(value).is_absolute():
            raise Stop("usage", code=2, detail=(
                f"{label} must be the absolute path on the Lenovo machine"))


def add_common_arguments(item: argparse.ArgumentParser) -> None:
    """Register the arguments every Dell request route shares."""

    item.add_argument("--shared", required=True,
                      help="owner-provided LAN share that holds the queue")
    item.add_argument("--token", required=True,
                      help="16 lowercase hex request token")
    item.add_argument("--lenovo-skill-dir", required=True,
                      help="absolute installed skill dir on Lenovo")
    item.add_argument("--lenovo-driver-dir", default=None,
                      help="absolute dir holding the Lenovo drivers on that "
                           "machine (default: <lenovo-skill-dir>/scripts)")
    item.add_argument("--python", required=True,
                      help="absolute interpreter path on the Lenovo machine")
    item.add_argument("--out-dir",
                      help="where to write the request (default: the shared dir)")
    item.add_argument("--request-name", required=True,
                      help="request file name on the shared dir")
    item.add_argument("--command-file", required=True,
                      help="UTF-8 command file for the bridge")
    item.add_argument("--expected-cwd", default=None,
                      help="absolute Lenovo working directory the job must "
                           "run in; defaults to the pinned --lenovo-checkout "
                           "when the route has one, otherwise no expectation")
    item.add_argument("--repo",
                      help="Dell checkout that must not contain the shared dir")


def flow_args(args) -> list[str]:
    """Extra Lenovo command arguments for a publication driver."""

    result: list[str] = []
    flow_dir = getattr(args, "lenovo_flow_dir", None)
    if flow_dir:
        require_absolute_lenovo_paths(("--lenovo-flow-dir", flow_dir))
        result += ["--flow-dir", str(Path(flow_dir))]
    if getattr(args, "reconcile", False):
        result.append("--reconcile")
    return result


def add_reconcile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reconcile", action="store_true",
                        help="render the consent-gated read-only recovery command")


def render_reconcile(command_parts: list[str], driver: str, dialect: str,
                     enabled: bool) -> str | None:
    if enabled or driver not in RECONCILE_DRIVERS:
        return None
    return build_for_transport(command_parts + ["--reconcile"], dialect)
