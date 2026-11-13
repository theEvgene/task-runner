"""Pre-submit syntax gate for the signed LAN bridge (syntax-only, stdlib only).

Small seam checked before seal/publish on the Dell submit path: the
caller only knows ``validate(command, dialect)`` and the returned
verdict. Dialect adapters stay behind the seam.

Supported dialects: ``powershell`` and ``cmd``. The dialect is explicit:
anything else (including an empty or missing value passed to
:func:`validate`) is an invalid verdict that names the two allowed
dialects. Ambiguity is a refusal, never a guess.

``cmd`` checks a deliberately narrow subset: one command line, balanced
double quotes, no NUL, and no unquoted ``| & < > ^ ( )`` operator. That
subset refuses pipelines, redirection, compounds, and subshells
fail-closed; it is not a full cmd grammar check. A pass therefore never
claims the line is idiomatic cmd, exists, or is safe.

``powershell`` requires the explicit launcher the worker can execute
through its platform shell (``shell=True``; ``cmd.exe`` on Windows):
``powershell -Command <script>`` (or a ``pwsh``/``powershell.exe``
equivalent, with optional launcher flags). A bare PowerShell line
without that launcher refuses as ambiguous even when its PowerShell
syntax is valid, because ``cmd.exe`` would not execute it as PowerShell.
The launcher is checked for shape only: no unquoted cmd operator may sit
outside the launcher's double-quoted script, and no cmd token may follow
the closing quote. Only ``-Command`` inline scripts are supported;
``-File`` and other launcher shapes refuse. The inner ``<script>`` alone
goes through ``Parser.ParseInput``.

``Parser.ParseInput`` only parses (it returns an AST plus ``Token[]`` and
``ParseError[]``). A passed check says nothing about the runtime:
existence of cmdlets/paths, execution policy, providers, rights, or the
network are out of scope, and a pass must never be read as "exists" or
"safe". Only syntax errors refuse.

Fail-closed: when the gate is active (an explicit dialect was given) and
its validator cannot run (no runtime, nonzero probe exit even with
output, unreadable probe output), validation raises
:exc:`ValidatorUnavailable` and the submit path must refuse before any
JOBID or queue write. With no dialect the gate stays off and the submit
path keeps its legacy behavior; that fallback is documented in
``docs/lan-bridge.md``.

Diagnostics never carry secrets: verdict messages hold only the dialect,
a fixed-shape reason, and an optional command offset. Parser free text
and caught-exception text pass through :func:`sanitize_diagnostic`,
which redacts command-derived tokens (any length: short tokens at word
boundaries, longer tokens as substrings) and truncates. Key material and
queue contents are never included.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_DIALECTS = ("powershell", "cmd")

_POWERSHELL_PROBE_TIMEOUT_SECONDS = 30

# Unquoted characters that make cmd.exe split or redirect a line. They
# are refused outside the launcher's double-quoted script so a signed
# command cannot grow a second cmd command.
_CMD_OPERATORS = "&|<>^()"

_POWERSHELL_PROBE_SCRIPT = """\
param([Parameter(Mandatory=$true)][string]$InputPath)
$content = [System.IO.File]::ReadAllText($InputPath)
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseInput($content, [ref]$tokens, [ref]$errors)
$out = @()
foreach ($e in $errors) {
  $start = -1
  try { $start = $e.Extent.StartOffset } catch { $start = -1 }
  $end = -1
  try { $end = $e.Extent.EndOffset } catch { $end = -1 }
  $msg = ''
  try { $msg = $e.Message } catch { $msg = 'parse error' }
  $id = ''
  try { $id = $e.ErrorId } catch { $id = '' }
  $out += @{ message = [string]$msg; start = [int]$start; end = [int]$end; errorId = [string]$id }
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 4
"""


class SyntaxGateError(Exception):
    """Base error for the pre-submit syntax gate."""


class ValidatorUnavailable(SyntaxGateError):
    """The requested dialect validator cannot run (fail-closed)."""


@dataclass(frozen=True)
class SyntaxVerdict:
    """Result of :func:`validate`: ``ok`` plus a secret-free diagnosis."""

    ok: bool
    dialect: str
    message: str = ""
    position: int | None = None


_UNIT_RE = re.compile(r"[A-Za-z0-9_./\\:\-]+")


def _command_units(command: str) -> list[str]:
    """Return candidate secret tokens from ``command``, longest first."""
    units: set[str] = set()
    for token in command.split():
        token = token.strip("\"'")
        if token:
            units.add(token)
    units.update(_UNIT_RE.findall(command))
    return sorted(units, key=len, reverse=True)


def sanitize_diagnostic(message: object, command: object,
                        max_len: int = 240) -> str:
    """Return a secret-free diagnostic string.

    Every command-derived unit is redacted from ``message``: units of
    three or more characters wherever they occur, two-character units at
    word boundaries, and the whole command when it is short enough to be
    echoed. Short secrets therefore do not survive merely because the
    earlier token rule had a length floor. The result is truncated.
    """
    text = str(message or "")
    if isinstance(command, str) and command:
        stripped = command.strip()
        if stripped and len(stripped) <= 500 and stripped in text:
            text = text.replace(stripped, "[redacted]")
        for unit in _command_units(command):
            if len(unit) >= 3:
                text = text.replace(unit, "[redacted]")
            else:
                pattern = (r"(?<![A-Za-z0-9_])" + re.escape(unit)
                           + r"(?![A-Za-z0-9_])")
                text = re.sub(pattern, "[redacted]", text)
    if len(text) > max_len:
        text = text[:max_len]
    return text


def _validate_cmd(command: str) -> SyntaxVerdict:
    lead = len(command) - len(command.lstrip())
    text = command.strip()
    if "\x00" in text:
        return SyntaxVerdict(False, "cmd", "nul byte is not valid command syntax",
                             position=lead + text.index("\x00"))
    if "\n" in text or "\r" in text:
        at = next(i for i, ch in enumerate(text) if ch in "\r\n")
        return SyntaxVerdict(False, "cmd",
                             "multi-line input is not one command: submit a "
                             "single command line", position=lead + at)
    if text.count('"') % 2 != 0:
        return SyntaxVerdict(False, "cmd", "unbalanced double quote",
                             position=lead + text.rfind('"'))
    in_quotes = False
    for idx, ch in enumerate(text):
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if in_quotes:
            continue
        if ch in _CMD_OPERATORS:
            hint = ""
            if ch == "&" and idx == len(text) - len(text.lstrip()):
                hint = (" (the PowerShell call operator needs an explicit "
                        "powershell -Command launcher; --dialect powershell)")
            return SyntaxVerdict(False, "cmd",
                                 "unsupported cmd operator outside quotes: "
                                 "the cmd gate accepts one simple command "
                                 "line only (no pipelines, redirection, "
                                 "compounds, or subshells)" + hint,
                                 position=lead + idx)
    return SyntaxVerdict(True, "cmd", "syntax ok")


_LAUNCHER_NAMES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def _launcher_name(token: str) -> str:
    name = token.strip().strip('"').replace("/", "\\")
    return name.rsplit("\\", 1)[-1].lower()


def _refuse(message: str, position: int | None = None) -> SyntaxVerdict:
    return SyntaxVerdict(False, "powershell", message, position=position)


def _split_launcher(command: str) -> tuple[int, int] | None:
    """Return ``(launcher_end, launcher_start)`` or None when not a launcher."""
    n = len(command)
    i = 0
    while i < n and command[i] in " \t":
        i += 1
    start = i
    if i < n and command[i] == '"':
        end = command.find('"', i + 1)
        if end == -1:
            return None
        token = command[i + 1:end]
        i = end + 1
    else:
        while i < n and command[i] not in " \t":
            i += 1
        token = command[start:i]
    if _launcher_name(token) not in _LAUNCHER_NAMES:
        return None
    return i, start


def _extract_ps_script(command: str) -> tuple[str | None, int, SyntaxVerdict | None]:
    """Split a launcher-wrapped command into its inner script and offset.

    Returns ``(inner, offset, None)`` on success, or
    ``(None, 0, refusal)`` when the launcher is missing, unsupported, or
    would hand cmd.exe an extra command. The worker runs through
    ``cmd.exe``, so a bare PowerShell line without
    ``powershell -Command`` refuses as ambiguous even when its syntax is
    valid.
    """
    split = _split_launcher(command)
    if split is None:
        return None, 0, _refuse(
            "powershell command without explicit 'powershell -Command' "
            "launcher is not portable to the worker cmd shell: wrap it as "
            "powershell -NoProfile -Command \"...\" and resubmit with "
            "--dialect powershell")
    launcher_end, _ = split
    remainder = command[launcher_end:]
    n = len(remainder)
    i = 0
    in_quotes = False
    flag_end = -1
    while i < n:
        ch = remainder[i]
        if ch == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if in_quotes:
            i += 1
            continue
        if ch in " \t":
            i += 1
            continue
        if ch in _CMD_OPERATORS:
            return None, 0, _refuse(
                "cmd operator outside the launcher's double-quoted "
                "-Command script would run a second command on the worker",
                position=launcher_end + i)
        start = i
        while i < n and remainder[i] not in " \t":
            if remainder[i] in _CMD_OPERATORS:
                return None, 0, _refuse(
                    "cmd operator outside the launcher's double-quoted "
                    "-Command script would run a second command on the "
                    "worker", position=launcher_end + i)
            i += 1
        token = remainder[start:i]
        lowered = token.lower()
        if lowered == "-command":
            flag_end = i
            break
        if lowered in ("-file", "-f"):
            return None, 0, _refuse(
                "only 'powershell -Command <script>' is supported: -File "
                "and other launcher shapes refuse")
    if in_quotes:
        return None, 0, _refuse("unbalanced double quote in the launcher")
    if flag_end == -1:
        return None, 0, _refuse(
            "powershell launcher without '-Command <script>' refuses: "
            "wrap it as powershell -NoProfile -Command \"...\"")
    j = flag_end
    while j < n and remainder[j] in " \t":
        j += 1
    script_start = launcher_end + j
    rest = remainder[j:]
    if not rest:
        return None, 0, _refuse("empty powershell -Command script",
                                position=script_start)
    if rest[0] == '"':
        close = rest.find('"', 1)
        if close == -1:
            return None, 0, _refuse("unbalanced double quote in the "
                                    "-Command script", position=script_start)
        trailing = rest[close + 1:]
        if trailing.strip():
            return None, 0, _refuse(
                "unexpected cmd token after the closing quote of the "
                "-Command script: the launcher must end at that quote",
                position=script_start + close + 1)
        inner = rest[1:close]
    else:
        if rest[0] == "'":
            return None, 0, _refuse(
                "single-quoted -Command script is not portable through "
                "cmd.exe: use double quotes around the script",
                position=script_start)
        quoted = False
        for idx, ch in enumerate(rest):
            if ch == '"':
                quoted = not quoted
                continue
            if quoted:
                continue
            if ch in _CMD_OPERATORS:
                return None, 0, _refuse(
                    "cmd operator outside quotes in the -Command script "
                    "would run a second command on the worker",
                    position=script_start + idx)
        if quoted:
            return None, 0, _refuse("unbalanced double quote in the "
                                    "-Command script", position=script_start)
        inner = rest
    if not inner.strip():
        return None, 0, _refuse("empty powershell -Command script",
                                position=script_start)
    return inner, script_start, None


def _find_powershell() -> str | None:
    """Return a PowerShell executable path, or None when no runtime exists.

    The operator may override discovery with ``TASK_RUNNER_POWERSHELL_BIN``:
    an empty value forces "unavailable" (fail-closed probe), otherwise
    the value is used as the executable path verbatim.
    """
    override = os.environ.get("TASK_RUNNER_POWERSHELL_BIN")
    if override is not None:
        override = override.strip()
        return override or None
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path(r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe")
    if fallback.is_file():
        return str(fallback)
    return None


def _parse_with_powershell(script: str, executable: str) -> list:
    """Run ``Parser.ParseInput`` through ``executable``; fail closed."""
    with tempfile.TemporaryDirectory(prefix="ps-syntax-") as tmp:
        cmd_file = Path(tmp) / "command.txt"
        probe_file = Path(tmp) / "probe.ps1"
        cmd_file.write_text(script, encoding="utf-8")
        probe_file.write_text(_POWERSHELL_PROBE_SCRIPT, encoding="utf-8")
        try:
            proc = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass",
                 "-File", str(probe_file),
                 "-InputPath", str(cmd_file)],
                capture_output=True, text=True,
                timeout=_POWERSHELL_PROBE_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidatorUnavailable(
                "powershell validator unavailable: cannot run the parser "
                "probe") from exc
    if proc.returncode != 0:
        # Nonzero exit is unavailable even when stdout happens to parse:
        # accepting that output would skip the check on a broken backend.
        raise ValidatorUnavailable(
            f"powershell validator unavailable: parser probe exited "
            f"{proc.returncode}")
    output = (proc.stdout or "").strip()
    if not output:
        raise ValidatorUnavailable(
            "powershell validator unavailable: parser probe returned no "
            "output")
    try:
        errors = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise ValidatorUnavailable(
            "powershell validator unavailable: unreadable parser probe "
            "output") from exc
    if isinstance(errors, dict):
        if "message" not in errors and "errorId" not in errors:
            raise ValidatorUnavailable(
                "powershell validator unavailable: unreadable parser probe "
                "output")
        errors = [errors]
    if not isinstance(errors, list):
        raise ValidatorUnavailable(
            "powershell validator unavailable: unreadable parser probe "
            "output")
    return errors


def _validate_powershell(command: str) -> SyntaxVerdict:
    text = command.strip()
    if "\n" in text or "\r" in text:
        at = next(i for i, ch in enumerate(command) if ch in "\r\n")
        return _refuse("multi-line input is not portable through the "
                       "worker cmd shell: submit one command line",
                       position=at)
    inner, offset, refusal = _extract_ps_script(command)
    if refusal is not None:
        return refusal
    assert inner is not None
    executable = _find_powershell()
    if executable is None:
        raise ValidatorUnavailable(
            "powershell validator unavailable: no pwsh/powershell runtime")
    errors = _parse_with_powershell(inner, executable)
    if not errors:
        return SyntaxVerdict(True, "powershell", "syntax ok")
    first = errors[0] if isinstance(errors[0], dict) else {}
    raw_message = str(first.get("message") or "powershell syntax error")
    error_id = str(first.get("errorId") or "").strip()
    if error_id:
        raw_message = f"{raw_message} [{error_id}]"
    message = sanitize_diagnostic(raw_message, command)
    if not message:
        message = "powershell syntax error"
    try:
        start = int(first.get("start", -1))
    except (TypeError, ValueError):
        start = -1
    position = offset + start if start >= 0 else None
    return SyntaxVerdict(False, "powershell", message, position=position)


_ADAPTERS = {
    "powershell": _validate_powershell,
    "cmd": _validate_cmd,
}


def validate(command: str, dialect: str) -> SyntaxVerdict:
    """Check ``command`` syntax for ``dialect``; never guesses the dialect.

    Returns a :class:`SyntaxVerdict`. Unknown, empty, or missing
    dialects return ``ok=False`` naming the explicit choice. Only a
    missing or broken validator backend raises :exc:`ValidatorUnavailable`
    (fail-closed by the caller). Pure syntax: a pass never claims the
    command exists or is safe to run.
    """
    if not isinstance(dialect, str) or dialect not in _ADAPTERS:
        return SyntaxVerdict(False, str(dialect) if isinstance(dialect, str) else "",
                             "explicit dialect required: powershell|cmd")
    if not isinstance(command, str) or not command.strip():
        return SyntaxVerdict(False, dialect, "empty command", position=0)
    if "\x00" in command:
        return SyntaxVerdict(False, dialect, "nul byte is not valid command syntax",
                             position=command.index("\x00"))
    adapter = _ADAPTERS[dialect]
    return adapter(command)


def add_cli(parser) -> None:
    """Register the submit ``--dialect`` switch (gate is opt-in)."""
    parser.add_argument("--dialect", default=None, choices=SUPPORTED_DIALECTS,
                        help="pre-submit syntax gate before any JOBID/write; "
                             "default: gate off")


def check(command: str, dialect: str | None) -> str | None:
    """Return a ready secret-free refusal message, or None when it passes.

    Used by the submit path. A refusal and an unavailable backend both
    return a message (fail-closed) instead of raising, so the caller can
    refuse uniformly before any JOBID or queue write.
    """
    if dialect is None:
        return None
    try:
        verdict = validate(command, dialect)
    except Exception:
        detail = "validator backend unavailable"
        return (f"syntax validator unavailable (dialect={dialect}): "
                f"{detail}")
    if verdict.ok:
        return None
    at = (f" at offset {verdict.position}"
          if verdict.position is not None else "")
    return (f"syntax refused (dialect={verdict.dialect}): "
            f"{verdict.message}{at}")


def enforce(command: str, dialect: str | None, error_type) -> None:
    """Raise ``error_type(problem)`` before any write, or return on pass."""
    if dialect is None:
        return
    problem = check(command, dialect)
    if problem:
        raise error_type(problem)
