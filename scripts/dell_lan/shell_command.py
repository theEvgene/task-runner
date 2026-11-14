"""Render one argv command for an explicitly declared Windows shell."""

from __future__ import annotations

from collections.abc import Sequence

POWERSHELL_LAUNCHER = "powershell.exe -NoProfile -NonInteractive -Command"


class ShellDialectError(ValueError):
    """The command cannot be represented in the declared shell dialect."""


def _parts(command: Sequence[str], dialect: str) -> list[str]:
    parts = list(command)
    if not parts:
        raise ShellDialectError("command must contain at least one part")
    if any(not isinstance(part, str) for part in parts):
        raise ShellDialectError("command parts must be strings")
    if any("\x00" in part or "\n" in part or "\r" in part for part in parts):
        raise ShellDialectError(f"{dialect} command parts contain control characters")
    if dialect == "cmd" and any("%" in part or "!" in part for part in parts):
        raise ShellDialectError(
            f"{dialect} command parts contain environment expansion markers")
    if dialect == "cmd" and any('"' in part for part in parts):
        raise ShellDialectError(
            f"{dialect} command parts contain an unrepresentable double quote")
    return parts


def _cmd_quote(part: str) -> str:
    """Quote one Windows argv part, including trailing backslashes."""

    trailing = len(part) - len(part.rstrip("\\"))
    body = part[:-trailing] if trailing else part
    return '"' + body + ("\\" * (trailing * 2)) + '"'


def _ps_quote(part: str) -> str:
    """Quote one fully literal PowerShell argument."""

    return "'" + part.replace("'", "''") + "'"


def build(command: Sequence[str], dialect: str) -> str:
    """Build one command for exactly ``cmd`` or ``powershell``.

    No dialect is inferred. The PowerShell form is the literal program that
    begins with its explicit call operator; transport wrapping is a separate
    boundary because the signed worker accepts only a supported launcher.
    """

    if dialect not in ("cmd", "powershell"):
        raise ShellDialectError("dialect must be exactly 'cmd' or 'powershell'")
    parts = _parts(command, dialect)
    if dialect == "powershell":
        return "& " + " ".join(_ps_quote(part) for part in parts)
    return " ".join(_cmd_quote(part) for part in parts)


def build_for_transport(command: Sequence[str], dialect: str) -> str:
    """Build argv and adapt it once to the signed worker transport.

    The worker's supported PowerShell launcher carries one double-quoted
    ``-Command`` script. A literal double quote inside that script would close
    the transport quote before the worker can encode it, so that one shape
    remains unrepresentable. Percent and exclamation characters are safe in
    PowerShell single-quoted arguments: the worker structurally replaces the
    launcher with an encoded program before invoking ``cmd.exe``.
    """

    program = build(command, dialect)
    if dialect == "powershell":
        if '"' in program:
            raise ShellDialectError(
                "powershell command parts contain a transport-unsafe double quote")
        return f'{POWERSHELL_LAUNCHER} "{program}"'
    return program
