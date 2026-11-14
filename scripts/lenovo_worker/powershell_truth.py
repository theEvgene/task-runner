"""PowerShell executor truth for the signed LAN worker (stdlib only).

Structural, never stderr heuristics: a PowerShell error (including a
parse-time ``ParseException`` and a swallowed non-terminating error) is
observed as a non-zero executor exit. The verdict comes from the exit
code only; ``stderr`` text never changes the verdict in either
direction.

Mechanism: a supported PowerShell launcher command
(``powershell -Command <script>``) is replaced by an already-valid encoded
outer program. The outer program decodes the inner text as data, parses it
with ``ScriptBlock.Create`` inside a ``try`` boundary, and invokes it with
``$ErrorActionPreference='Stop'``. Native exit is preserved with
``exit $LASTEXITCODE``. ``cmd`` commands run untouched through the platform
shell.

Supported dialect: ``powershell -Command "<script>"`` or a bare,
unquoted script, matching the submit syntax gate. Any other shape that
starts with a PowerShell launcher (``-File``, ``-EncodedCommand``, a
single-quoted script, trailing cmd tokens, a nested ``cmd /c
powershell``) refuses through :class:`UnsupportedLauncher` instead of
running unwrapped: an unwrapped launcher may report exit 0 for a command
it failed to parse, which is exactly the false success this module
exists to prevent. Diagnostics carry only the dialect and a fixed-shape
reason, never command content or key material.
"""

from __future__ import annotations

import base64
import subprocess
from datetime import datetime, timezone

LAUNCHER_NAMES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_OUTER_SCRIPT = """$ErrorActionPreference='Stop'
try {
    $innerText = [Text.Encoding]::Unicode.GetString(
        [Convert]::FromBase64String('__INNER_SCRIPT__'))
    $inner = [ScriptBlock]::Create($innerText)
    & $inner
    if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }
    exit 0
} catch {
    # Formatting the ErrorRecord repeats parser input (and can expose
    # credentials embedded in the signed command). Keep the receipt useful
    # without reflecting any command-controlled exception text.
    [Console]::Error.WriteLine('PowerShell command failed')
    exit 1
}
"""
# Mirrors scripts/lan_bridge/syntax.py: unquoted cmd.exe metacharacters
# are refused so a signed command cannot grow a second cmd command.
_CMD_OPERATORS = "&|<>^()\r\n"
# Launcher flags that hand the script to PowerShell by a different route
# (file or encoded payload) and therefore cannot be wrapped in place.
_UNSUPPORTED_FLAGS = {
    "-file": "launcher-file-flag",
    "-f": "launcher-file-flag",
    "-encodedcommand": "launcher-encoded-command",
    "-e": "launcher-encoded-command",
    "-ec": "launcher-encoded-command",
    "-ecx": "launcher-encoded-command",
}
# cmd.exe switches that may precede the command selector. They do not change
# the fact that the payload after /c or /k is the command we must classify.
_CMD_PREFIX_FLAGS = {"/d", "/q", "/a", "/u", "/s"}
_CMD_PREFIXES = ("/e:", "/f:", "/v:")
_START_OPTIONS_WITH_VALUE = {"/affinity", "/d", "/machine", "/node"}
_CHAIN_SAFE = "safe"
_CHAIN_POWERSHELL = "powershell"
_CHAIN_AMBIGUOUS = "ambiguous"


class UnsupportedLauncher(ValueError):
    """A detected PowerShell launcher shape that cannot be wrapped safely.

    ``reason`` is a fixed token (never command text), safe to seal in a
    result envelope and print in a diagnostic.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _launcher_name(token: str) -> str:
    name = token.strip().strip('"').replace("/", "\\")
    return name.rsplit("\\", 1)[-1].lower()


def _first_token(text: str) -> tuple[str, int]:
    """Return cmd's first executable token and its end offset.

    Double quotes can surround all or only part of a token. Removing those
    syntactic quotes is what makes ``po"wer"shell.exe`` the same executable
    boundary as ``powershell.exe`` while leaving later documentation text
    irrelevant to classification.
    """

    token, end, _ = _first_cmd_token(text)
    return token, end


def _first_cmd_token(text: str) -> tuple[str, int, bool]:
    """Return a decoded cmd token, its end, and whether it used quotes."""

    i = 0
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    token: list[str] = []
    quoted = False
    used_quotes = False
    while i < n:
        char = text[i]
        if char == "^" and not quoted and i + 1 < n:
            # cmd removes an unquoted caret before resolving the executable.
            # Classification must see the same boundary: po^wer^shell.exe is
            # powershell.exe, while a later argument remains only an argument.
            token.append(text[i + 1])
            i += 2
            continue
        if char == '"':
            quoted = not quoted
            used_quotes = True
            i += 1
            continue
        if char in " \t" and not quoted:
            break
        token.append(char)
        i += 1
    return "".join(token), i, used_quotes


def is_powershell_command(command: str) -> bool:
    """True when the command starts with an explicit PowerShell launcher."""

    if not isinstance(command, str) or not command.strip():
        return False
    token, _ = _first_token(command)
    return _launcher_name(token) in LAUNCHER_NAMES


def _split_launcher(command: str) -> int:
    """End offset of the launcher token, or -1 when it cannot be read."""

    if not is_powershell_command(command):
        return -1
    _, end = _first_token(command)
    return end if 0 < end <= len(command) else -1


def _strip_cmd_command_quotes(payload: str) -> str:
    """Remove cmd's outer /S /C quote pair, preserving executable quotes."""

    text = payload.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].strip()
    return text


def _split_cmd_chain(command: str) -> list[str] | None:
    """Split a cmd chain at real separators, or refuse ambiguous syntax.

    Carets escape the next character and double quotes protect separators.
    Parenthesized compound commands need cmd's full parser, so this narrow
    worker boundary refuses them rather than guessing which text executes.
    """

    segments: list[str] = []
    start = 0
    quoted = False
    i = 0
    while i < len(command):
        char = command[i]
        if char == "^" and not quoted:
            if i + 1 >= len(command):
                return None
            i += 2
            continue
        if char == '"':
            quoted = not quoted
            i += 1
            continue
        if not quoted and char in "()":
            return None
        if not quoted and char in "&|\r\n":
            segment = command[start:i].strip()
            if not segment:
                return None
            segments.append(segment)
            if char in "&|" and i + 1 < len(command) \
                    and command[i + 1] == char:
                i += 1
            start = i + 1
        i += 1
    if quoted:
        return None
    tail = command[start:].strip()
    if not tail:
        return None
    segments.append(tail)
    return segments


def _classify_start(remainder: str, depth: int) -> str:
    """Classify START's command after its optional quoted window title."""

    _, end, quoted = _first_cmd_token(remainder)
    if quoted:
        # For START the first quoted argument is always the window title,
        # including the conventional empty title: start "" program args.
        remainder = remainder[end:]
    while True:
        option, option_end, _ = _first_cmd_token(remainder)
        if not option:
            return _CHAIN_SAFE
        lowered = option.lower()
        if not lowered.startswith("/"):
            return _classify_cmd_chain(remainder, depth + 1)
        remainder = remainder[option_end:]
        if lowered in _START_OPTIONS_WITH_VALUE:
            value, value_end, _ = _first_cmd_token(remainder)
            if not value:
                return _CHAIN_AMBIGUOUS
            remainder = remainder[value_end:]


def _classify_simple_cmd(command: str, depth: int) -> str:
    stripped = command.lstrip()
    while stripped.startswith("@"):
        stripped = stripped[1:].lstrip()
    token, end = _first_token(stripped)
    if not token:
        return _CHAIN_AMBIGUOUS
    name = _launcher_name(token)
    if name in LAUNCHER_NAMES:
        return _CHAIN_POWERSHELL
    remainder = stripped[end:]
    if name == "call":
        return _classify_cmd_chain(remainder, depth + 1)
    if name in ("start", "start.exe"):
        return _classify_start(remainder, depth + 1)
    if name == "if":
        condition, condition_end = _first_token(remainder)
        if condition.lower() == "not":
            remainder = remainder[condition_end:]
            condition, condition_end = _first_token(remainder)
        if not condition:
            return _CHAIN_AMBIGUOUS
        remainder = remainder[condition_end:]
        if condition.lower() in ("cmdextversion", "errorlevel", "exist",
                                  "defined"):
            operand, operand_end = _first_token(remainder)
            if not operand:
                return _CHAIN_AMBIGUOUS
            remainder = remainder[operand_end:]
        result = _classify_cmd_chain(remainder, depth + 1)
        # ELSE and parenthesized forms require the full cmd grammar. A direct
        # PowerShell consequent is decisive; everything else is ambiguous.
        return result if result == _CHAIN_POWERSHELL else _CHAIN_AMBIGUOUS
    if name not in ("cmd", "cmd.exe", "%comspec%"):
        return _CHAIN_SAFE

    offset = end
    unknown_prefix = False
    while offset < len(stripped):
        option, option_end = _first_token(stripped[offset:])
        if not option:
            return _CHAIN_AMBIGUOUS
        lowered = option.lower()
        offset += option_end
        if lowered in ("/c", "/k"):
            payload = _strip_cmd_command_quotes(stripped[offset:])
            result = _classify_cmd_chain(payload, depth + 1)
            if unknown_prefix and result == _CHAIN_POWERSHELL:
                return _CHAIN_AMBIGUOUS
            return result
        if (lowered in _CMD_PREFIX_FLAGS
                or lowered.startswith(_CMD_PREFIXES)):
            continue
        unknown_prefix = True
    return _CHAIN_AMBIGUOUS


def _classify_cmd_chain(command: str, depth: int = 0) -> str:
    """Classify every executable boundary in a cmd command chain."""

    if depth > 12:
        return _CHAIN_AMBIGUOUS
    segments = _split_cmd_chain(command)
    if segments is None:
        return _CHAIN_AMBIGUOUS
    verdict = _CHAIN_SAFE
    for segment in segments:
        result = _classify_simple_cmd(segment, depth)
        if result == _CHAIN_POWERSHELL:
            return result
        if result == _CHAIN_AMBIGUOUS:
            verdict = result
    return verdict


def _nested_launcher_kind(command: str) -> str | None:
    """Fixed reason for PowerShell hidden behind cmd executable syntax.

    A job that reaches the worker without the submit syntax gate can wrap
    the launcher in ``cmd /c`` or a leading ``&``; wrapping that shape in
    place is not safe, so it refuses rather than running unwrapped.
    """

    token, end = _first_token(command)
    name = _launcher_name(token)
    chain_kind = _classify_cmd_chain(command)
    if token.startswith("@") and chain_kind != _CHAIN_SAFE:
        return ("nested-echo-suppressed-powershell"
                if chain_kind == _CHAIN_POWERSHELL else "nested-cmd-ambiguous")
    if name == "call" and chain_kind != _CHAIN_SAFE:
        return ("nested-call-powershell"
                if chain_kind == _CHAIN_POWERSHELL else "nested-cmd-ambiguous")
    if name in ("cmd", "cmd.exe", "%comspec%"):
        if chain_kind == _CHAIN_POWERSHELL:
            return "nested-cmd-powershell"
        if chain_kind == _CHAIN_AMBIGUOUS:
            return "nested-cmd-ambiguous"
        return None
    if token == "&":
        second, _ = _first_token(command[end:])
        if _launcher_name(second) in LAUNCHER_NAMES:
            return "nested-call-operator-powershell"
    # The chain classifier inspects every executable boundary, not only a
    # cmd-prefixed payload.  Its verdict must also govern direct legacy
    # chains such as ``echo ready & powershell ...`` and ``start ...``.
    if chain_kind == _CHAIN_POWERSHELL:
        return "nested-cmd-powershell"
    if chain_kind == _CHAIN_AMBIGUOUS:
        return "nested-cmd-ambiguous"
    return None


def _scan_launcher_flags(command: str, launcher_end: int) -> tuple[int, int]:
    """Offsets around ``-Command``; raise for an unsafe launcher shape."""

    n = len(command)
    i = launcher_end
    in_quotes = False
    flag_start = -1
    flag_end = -1
    while i < n:
        ch = command[i]
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
            raise UnsupportedLauncher("operator-outside-script")
        start = i
        while i < n and command[i] not in " \t":
            if command[i] in _CMD_OPERATORS:
                raise UnsupportedLauncher("operator-outside-script")
            i += 1
        lowered = command[start:i].lower()
        if lowered == "-command":
            flag_start = start
            flag_end = i
            break
        reason = _UNSUPPORTED_FLAGS.get(lowered)
        if reason is not None:
            raise UnsupportedLauncher(reason)
    if in_quotes:
        raise UnsupportedLauncher("unbalanced-launcher-quote")
    if flag_end == -1:
        raise UnsupportedLauncher("missing-command-flag")
    return flag_start, flag_end


def _split_script(command: str, flag_end: int) -> tuple[str, str]:
    """Return ``(head, inner_script)``; raise for an unsafe script shape."""

    n = len(command)
    i = flag_end
    while i < n and command[i] in " \t":
        i += 1
    if i >= n:
        raise UnsupportedLauncher("empty-command-script")
    head = command[:i]
    rest = command[i:]
    if rest[0] == '"':
        close = rest.find('"', 1)
        if close == -1:
            raise UnsupportedLauncher("unbalanced-script-quote")
        if rest[close + 1:].strip():
            raise UnsupportedLauncher("trailing-token-after-script")
        inner = rest[1:close]
    elif rest[0] == "'":
        # cmd.exe does not treat single quotes as quoting: PowerShell would
        # receive a string literal instead of the script (a silent no-op).
        raise UnsupportedLauncher("single-quoted-script")
    else:
        for ch in rest:
            if ch == '"':
                raise UnsupportedLauncher("bare-script-with-quotes")
            if ch in _CMD_OPERATORS:
                raise UnsupportedLauncher("operator-outside-script")
        inner = rest
    if not inner.strip():
        raise UnsupportedLauncher("empty-command-script")
    return head, inner


def launcher_kind(command: str) -> str | None:
    """Classify a command without executing it.

    Returns ``None`` for a non-PowerShell command, ``"wrappable"`` for a
    supported ``-Command`` launcher, or a fixed refusal reason for a
    launcher shape that must not run unwrapped.
    """

    if not is_powershell_command(command):
        return _nested_launcher_kind(command)
    launcher_end = _split_launcher(command)
    if launcher_end == -1:
        return "launcher-token-unparseable"
    try:
        _, flag_end = _scan_launcher_flags(command, launcher_end)
        _split_script(command, flag_end)
    except UnsupportedLauncher as error:
        return error.reason
    return "wrappable"


def wrap_powershell(command: str) -> str:
    """Build a valid outer PowerShell program with error propagation.

    Non-launcher input is returned unchanged. A detected launcher that is
    not the supported ``-Command <script>`` dialect raises
    :class:`UnsupportedLauncher` instead of returning the command
    unwrapped: running it unchanged could report ``ok`` for a parse error.
    """

    if not is_powershell_command(command):
        return command
    launcher_end = _split_launcher(command)
    if launcher_end == -1:
        raise UnsupportedLauncher("launcher-token-unparseable")
    flag_start, flag_end = _scan_launcher_flags(command, launcher_end)
    _, inner = _split_script(command, flag_end)

    # The outer program must already be valid before PowerShell sees user
    # text.  Treat the inner script as UTF-16LE/base64 data, parse it with
    # ScriptBlock.Create inside the established try/catch, and only then
    # invoke it.  Encoding the fixed outer program also avoids introducing
    # cmd.exe quoting or metacharacter behavior at the worker shell boundary.
    inner_payload = base64.b64encode(inner.encode("utf-16le")).decode("ascii")
    outer = _OUTER_SCRIPT.replace("__INNER_SCRIPT__", inner_payload)
    outer_payload = base64.b64encode(outer.encode("utf-16le")).decode("ascii")
    return command[:flag_start] + "-EncodedCommand " + outer_payload


def envelope_status(returncode: int) -> str:
    """``ok`` only for exit zero, otherwise ``error`` (stderr is ignored)."""

    return "ok" if returncode == 0 else "error"


def sanitize_diagnostic(reason: str) -> str:
    """Secret-free one-line diagnosis (dialect + fixed reason only)."""

    text = str(reason or "powershell execution failed").strip()
    return ("powershell executor: " + text)[:240]


def _refusal(reason: str, started: str, limit: int) -> dict:
    detail = sanitize_diagnostic(reason)
    return {"status": "error", "exit_code": 2, "stdout": "",
            "stderr": detail[:limit], "started_at": started,
            "timed_out": False, "output_truncated": len(detail) > limit}


def execute(command: str, workdir: str, timeout_s: int, limit: int,
            runner=None) -> dict:
    """Run one command with structural PowerShell propagation.

    ``runner`` is an injectable ``subprocess.run``-compatible callable
    for deterministic unit tests. The verdict derives from the exit
    code only; ``stderr`` content never flips it. An unsupported launcher
    shape refuses with a non-zero exit instead of running unwrapped.
    """

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kind = launcher_kind(command)
    if kind is None:
        effective = command
    elif kind == "wrappable":
        try:
            effective = wrap_powershell(command)
        except UnsupportedLauncher as error:
            return _refusal(f"launcher shape {error.reason} refuses",
                            started, limit)
    else:
        return _refusal(f"launcher shape {kind} refuses", started, limit)
    run = runner or (lambda *a, **k: subprocess.run(*a, **k))
    try:
        proc = run(
            effective, shell=True, cwd=workdir, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as error:
        out = error.stdout if isinstance(error.stdout, str) else ""
        err = error.stderr if isinstance(error.stderr, str) else ""
        return {"status": "error", "exit_code": 124,
                "stdout": (out or "")[:limit],
                "stderr": ("timed out on the worker\n" + (err or ""))[:limit],
                "started_at": started, "timed_out": True,
                "output_truncated": len(out or "") > limit}
    except Exception as error:
        detail = sanitize_diagnostic(f"spawn failed: {type(error).__name__}")
        return {"status": "error", "exit_code": 2, "stdout": "",
                "stderr": detail[:limit], "started_at": started,
                "timed_out": False, "output_truncated": False}
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    code = proc.returncode
    return {"status": envelope_status(code), "exit_code": code,
            "stdout": stdout[:limit], "stderr": stderr[:limit],
            "started_at": started, "timed_out": False,
            "output_truncated": len(stdout) > limit or len(stderr) > limit}
