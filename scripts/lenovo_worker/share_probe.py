"""Bounded SMB share probe for the Lenovo launcher (stdlib only).

Returns observations, never conclusions: the caller classifies them in
``reasons.py``. The password travels only inside a transient PowerShell
``-File`` script that reads it from the local credential file, so it never
appears in process args, stdout, stderr, logs, or receipts. Nothing here
creates, retries, kills, or changes a share, credential, or host setting.

``fixtureLocalDir`` verifies a local directory (fixtures only) and never
touches SMB. The real path maps the existing share and reads the probe's own
token plus exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import reasons  # noqa: E402  (sibling helper, same delivery)


def _read_password(password_file: Path) -> str:
    try:
        text = password_file.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise reasons.Refusal(
            "smb-credential-file-invalid",
            "bad credentials: credential file unreadable") from error
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise reasons.Refusal("smb-credential-file-invalid",
                              "bad credentials: credential file holds no password")
    if len(lines) > 1:
        raise reasons.Refusal("smb-credential-file-invalid",
                              "bad credentials: credential file must hold a single line")
    return lines[0]


def connect_share(smb: dict) -> dict:
    """Make the existing share visible and return probe observations only."""

    if not isinstance(smb, dict):
        raise reasons.Refusal("config-invalid", "smb must be an object")
    fixture = smb.get("fixtureLocalDir")
    password_file = smb.get("passwordFile")
    if fixture:
        local = Path(str(fixture))
        if not local.is_dir():
            return {"shareStatus": "MAP-FAILED", "shareVisible": False}
        expected = smb.get("fixtureRequirePassword")
        if expected is not None:
            if not password_file:
                return {"shareStatus": "BAD-CREDENTIAL-FILE", "shareVisible": None}
            if _read_password(Path(str(password_file))) != str(expected):
                return {"shareStatus": "AUTH-DENIED", "shareVisible": None}
        return {"shareStatus": "MAPPED", "shareVisible": True}
    unc = str(smb.get("unc") or "")
    username = str(smb.get("username") or "")
    if not unc or not username or not password_file:
        raise reasons.Refusal("config-invalid", "smb needs unc, username and passwordFile")
    secret = _read_password(Path(str(password_file)))
    if len(secret) > 256:
        raise reasons.Refusal("smb-credential-file-invalid",
                              "bad credentials: credential file malformed")
    script = (
        "$ErrorActionPreference='Stop'\n"
        f"$unc={json.dumps(unc)}\n"
        f"$user={json.dumps(username)}\n"
        f"$pwFile={json.dumps(str(password_file))}\n"
        "if (Test-Path -LiteralPath $unc) { 'ALREADY:'+$unc; exit 0 }\n"
        # Same normalization as _read_password (first line, trimmed) so a
        # trailing CRLF written by any editor matches the fixture check.
        # The plaintext lives only in this transient script, never in
        # process args, stdout, stderr, logs, or receipts.
        "$pwRaw=Get-Content -Raw -LiteralPath $pwFile\n"
        "if ($null -eq $pwRaw -or $pwRaw.Trim().Length -eq 0) { 'BAD-CREDENTIAL-FILE'; exit 12 }\n"
        "$pwPlain=(($pwRaw -split \"\\r?\\n\")[0]).Trim()\n"
        "try {\n"
        "  $sec=ConvertTo-SecureString -String $pwPlain -AsPlainText -Force\n"
        "  $pwPlain=$null\n"
        "  $cred=New-Object PSCredential($user,$sec)\n"
        "  $null=New-SmbMapping -RemotePath $unc -UserName $user -Password $cred.GetNetworkCredential().Password -Persistent $false\n"
        "} catch [System.UnauthorizedAccessException] { 'AUTH-DENIED'; exit 10 }\n"
        "catch { 'MAP-FAILED:'+$_.Exception.Message; exit 11 }\n"
        "if (Test-Path -LiteralPath $unc) { 'MAPPED:'+$unc; exit 0 }\n"
        "'MAP-FAILED:unreachable after mapping'; exit 11\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script_path],
            capture_output=True, text=True, timeout=120, check=False,
        )
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
    out = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()
    first = out[0].strip() if out else ""
    status = reasons.share_status(first, completed.returncode)
    if status == "MAPPED":
        return {"shareStatus": "MAPPED", "shareVisible": True}
    if status == "MAP-FAILED":
        return {"shareStatus": status, "shareVisible": False,
                "shareDetail": first[:120]}
    if status == "UNOBSERVED":
        # An unexpected token is not evidence for any specific cause.
        return {"shareStatus": "UNOBSERVED", "shareVisible": None}
    return {"shareStatus": status, "shareVisible": None}
