"""Conservative cmd execution-boundary classifier for worker fallback."""

from __future__ import annotations

LAUNCHERS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
UNKNOWN = "unprovable-command-chain"
_START_VALUE_OPTIONS = {"/affinity", "/d", "/machine", "/node"}


def _token(text: str) -> tuple[str, int, bool]:
    index = 0
    while index < len(text) and text[index] in " \t":
        index += 1
    token: list[str] = []
    quoted = False
    used_quotes = False
    while index < len(text):
        char = text[index]
        if char == "^" and not quoted:
            if index + 1 >= len(text):
                return "", len(text), used_quotes
            token.append(text[index + 1])
            index += 2
            continue
        if char == '"':
            quoted = not quoted
            used_quotes = True
            index += 1
            continue
        if char in " \t" and not quoted:
            break
        token.append(char)
        index += 1
    if quoted:
        return "", len(text), used_quotes
    return "".join(token), index, used_quotes


def _segments(command: str) -> list[str] | None:
    segments: list[str] = []
    start = 0
    quoted = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "^" and not quoted:
            if index + 1 >= len(command):
                return None
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "()<>\r\n":
            return None
        elif not quoted and char in "&|":
            segment = command[start:index].strip()
            if not segment:
                return None
            segments.append(segment)
            if index + 1 < len(command) and command[index + 1] == char:
                index += 1
            start = index + 1
        index += 1
    tail = command[start:].strip()
    if quoted or not tail:
        return None
    return segments + [tail]


def _start(remainder: str, depth: int) -> str | None:
    _, end, quoted = _token(remainder)
    if quoted:
        remainder = remainder[end:]
    while True:
        option, end, _ = _token(remainder)
        if not option:
            return UNKNOWN
        if not option.startswith("/"):
            return launcher_kind(remainder, depth + 1)
        remainder = remainder[end:]
        if option.lower() in _START_VALUE_OPTIONS:
            value, end, _ = _token(remainder)
            if not value:
                return UNKNOWN
            remainder = remainder[end:]


def _if(remainder: str, depth: int) -> str | None:
    token, end, _ = _token(remainder)
    if token.lower() == "/i":
        remainder = remainder[end:]
        token, end, _ = _token(remainder)
    if token.lower() == "not":
        remainder = remainder[end:]
        token, end, _ = _token(remainder)
    if not token:
        return UNKNOWN
    remainder = remainder[end:]
    if token.lower() in ("cmdextversion", "errorlevel", "exist", "defined"):
        operand, end, _ = _token(remainder)
        if not operand:
            return UNKNOWN
        remainder = remainder[end:]
    if " else " in f" {remainder.lower()} ":
        return UNKNOWN
    return launcher_kind(remainder, depth + 1)


def launcher_kind(command: str, depth: int = 0) -> str | None:
    """Return PowerShell, unknown, or safe for every direct cmd boundary."""
    if not isinstance(command, str) or not command.strip() or depth > 12:
        return UNKNOWN
    segments = _segments(command)
    if segments is None:
        return UNKNOWN
    verdict = None
    if len(segments) > 1:
        for segment in segments:
            kind = launcher_kind(segment, depth + 1)
            if kind == "powershell-launcher":
                return kind
            if kind == UNKNOWN:
                verdict = kind
        return verdict
    stripped = command.lstrip()
    while stripped.startswith("@"):
        stripped = stripped[1:].lstrip()
    executable, end, _ = _token(stripped)
    if not executable or "%" in executable or "!" in executable:
        return UNKNOWN
    executable = executable.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if executable in LAUNCHERS:
        return "powershell-launcher"
    remainder = stripped[end:].lstrip()
    if executable == "call":
        if remainder.startswith(":") or "%" in remainder or "!" in remainder:
            return UNKNOWN
        return launcher_kind(remainder, depth + 1)
    if executable in ("start", "start.exe"):
        return _start(remainder, depth)
    if executable == "if":
        return _if(remainder, depth)
    if executable in ("for", "forfiles") or executable.endswith((".bat", ".cmd")):
        return UNKNOWN
    if executable in ("cmd", "cmd.exe", "%comspec%"):
        while remainder:
            option, end, _ = _token(remainder)
            if not option:
                return UNKNOWN
            remainder = remainder[end:].lstrip()
            if option.lower() in ("/c", "/k"):
                payload = remainder.strip()
                if len(payload) >= 2 and payload[0] == payload[-1] == '"':
                    payload = payload[1:-1].strip()
                return launcher_kind(payload, depth + 1)
        return UNKNOWN
    return None
