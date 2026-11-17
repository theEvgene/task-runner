"""Dell-side builder and receipt parser for the Lenovo default-playback mute route.

One end-to-end operation through the existing signed LAN transport
(``scripts/lan_bridge/lan_bridge.py`` ``submit``/``fetch`` plus
``scripts/lan_bridge/get.py``): mute the default Windows playback endpoint on
Lenovo, or read its current mute state without changing it. The agent never
hand-assembles the PowerShell/COM command: :func:`build` renders the exact
signed command, and :func:`decide` reports confirmed success only after the
target state was read back.

This module writes the command file only (``build`` subcommand). It never
submits a job, never touches the queue or the key, and never reaches Lenovo.
Live execution stays a separately authorized step (see ``docs/lenovo-audio.md``).
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parent / "lan_bridge"
sys.path[:0] = [str(BRIDGE_DIR)] if str(BRIDGE_DIR) not in sys.path else []

import wait as waitmod  # noqa: E402  (single owner of job-state wording)

SCHEMA = "lenovo-audio-state/1"
COMMAND_SCHEMA = "dell-lenovo-audio-command/1"
OPERATIONS = ("mute", "read")
LAUNCHER = "powershell.exe -NoProfile -NonInteractive -Command"
KNOWN_ERRORS = ("no-default-playback-device", "audio-operation-failed")

# E_NOTFOUND (0x80070490) as a signed 32-bit HResult: the MMDevice API reports
# exactly this when no default render endpoint exists. Derived, not recalled.
NO_DEVICE_HRESULT = ctypes.c_int32(0x80070490).value

# Default playback endpoint addressed on Lenovo: eRender + eMultimedia.
# Only this endpoint is supported; no other device selection exists.
DATA_FLOW_RENDER = 0
ROLE_MULTIMEDIA = 1


class ReceiptError(ValueError):
    """Stdout is not a usable ``lenovo-audio-state/1`` receipt (never success)."""


CSHARP_SOURCE = r"""using System;
using System.Runtime.InteropServices;

public static class LenovoAudioEndpoint
{
    [ComImport]
    [Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    private class MMDeviceEnumerator
    {
    }

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator
    {
        // Slot 0 is EnumAudioEndpoints; this route never enumerates, so only
        // the slot is reserved to keep GetDefaultAudioEndpoint on slot 1.
        int UnusedEnumAudioEndpointsSlot();
        [PreserveSig]
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
    }

    [Guid("D666063F-1587-4E43-81F1-B948E807363F")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice
    {
        [PreserveSig]
        int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object @interface);
    }

    [Guid("5CDF2C82-841E-4546-9722-0CF74078229A")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioEndpointVolume
    {
        int RegisterControlChangeNotify(IntPtr notify);
        int UnregisterControlChangeNotify(IntPtr notify);
        int GetChannelCount(out uint channelCount);
        int SetMasterVolumeLevel(float levelDB, ref Guid eventContext);
        int SetMasterVolumeLevelScalar(float level, ref Guid eventContext);
        int GetMasterVolumeLevel(out float levelDB);
        int GetMasterVolumeLevelScalar(out float level);
        int SetChannelVolumeLevel(uint channel, float levelDB, ref Guid eventContext);
        int SetChannelVolumeLevelScalar(uint channel, float level, ref Guid eventContext);
        int GetChannelVolumeLevel(uint channel, out float levelDB);
        int GetChannelVolumeLevelScalar(uint channel, out float level);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid eventContext);
        int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
        int GetVolumeStepInfo(out uint step, out uint stepCount);
        int VolumeStepUp(ref Guid eventContext);
        int VolumeStepDown(ref Guid eventContext);
        int QueryHardwareSupport(out uint hardwareSupportMask);
        int GetVolumeRange(out float levelMinDB, out float levelMaxDB, out float volumeIncrementDB);
    }

    private const int DataFlowRender = 0;
    private const int RoleMultimedia = 1;
    private const int ClsCtxAll = 23;

    private static IAudioEndpointVolume OpenDefault()
    {
        IMMDeviceEnumerator enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDevice device;
        int hr = enumerator.GetDefaultAudioEndpoint(DataFlowRender, RoleMultimedia, out device);
        if (hr != 0)
        {
            Marshal.ThrowExceptionForHR(hr);
        }
        Guid iid = typeof(IAudioEndpointVolume).GUID;
        object instance;
        hr = device.Activate(ref iid, ClsCtxAll, IntPtr.Zero, out instance);
        if (hr != 0)
        {
            Marshal.ThrowExceptionForHR(hr);
        }
        return (IAudioEndpointVolume)instance;
    }

    public static bool GetMute()
    {
        bool mute;
        int hr = OpenDefault().GetMute(out mute);
        if (hr != 0)
        {
            Marshal.ThrowExceptionForHR(hr);
        }
        return mute;
    }

    public static bool SetMute(bool mute)
    {
        Guid context = Guid.Empty;
        int hr = OpenDefault().SetMute(mute, ref context);
        if (hr != 0)
        {
            Marshal.ThrowExceptionForHR(hr);
        }
        return GetMute();
    }
}
"""

# Single-line, single-quote-only PowerShell: the transport wrapper carries this
# inside one double-quoted -Command script, so the inner text must hold no
# double quote and no line break. The C# source travels base64-encoded, which
# keeps the quote rule intact. {CSB64}/{OP}/{NOHR} are replaced literally
# (never str.format: the script itself is full of braces).
INNER_TEMPLATE = (
    "$b='{CSB64}';"
    "$s=[Text.Encoding]::Utf8.GetString([Convert]::FromBase64String($b));"
    "Add-Type -TypeDefinition $s -Language CSharp;"
    "$o='{OP}';"
    "try{"
    "if($o -eq 'mute'){$m=[LenovoAudioEndpoint]::SetMute($true)}"
    "else{$m=[LenovoAudioEndpoint]::GetMute()};"
    "$r=@{schema='lenovo-audio-state/1';operation=$o;muted=($m -eq $true);ok=$true};"
    "$r|ConvertTo-Json -Compress;exit 0"
    "}catch{"
    "$c=$_.Exception.HResult;$e='audio-operation-failed';"
    "if($c -eq {NOHR}){$e='no-default-playback-device'};"
    "$r=@{schema='lenovo-audio-state/1';operation=$o;ok=$false;error=$e};"
    "$r|ConvertTo-Json -Compress;exit 1}"
)


def csharp_base64() -> str:
    """Base64 of the exact C# interop source sealed into the command."""

    return base64.b64encode(CSHARP_SOURCE.encode("utf-8")).decode("ascii")


def render_inner(operation: str) -> str:
    """The inner PowerShell script for one operation (no transport wrapper)."""

    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {OPERATIONS}")
    inner = INNER_TEMPLATE.replace("{CSB64}", csharp_base64())
    inner = inner.replace("{OP}", operation)
    inner = inner.replace("{NOHR}", str(NO_DEVICE_HRESULT))
    if '"' in inner:
        raise ReceiptError("rendered audio script carries a double quote")
    if "\n" in inner or "\r" in inner or "\x00" in inner:
        raise ReceiptError("rendered audio script is not one command line")
    return inner


def build(operation: str) -> str:
    """The exact signed-queue command line for one operation.

    Shape: ``powershell.exe -NoProfile -NonInteractive -Command "<inner>"``,
    the launcher the worker executes through its platform shell. ``mute`` sets
    mute unconditionally and reads the state back in the same run; ``read``
    only reads. Neither form toggles, so a repeated ``mute`` never unmutes.
    """

    return f"{LAUNCHER} \"{render_inner(operation)}\""


def parse_stdout(stdout: object, operation: str) -> dict:
    """Parse one Lenovo receipt; raise :class:`ReceiptError` when unusable.

    Strict and secret-free: diagnostics name the expected shape only, never
    the received text (a foreign stdout may carry anything).
    """

    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {OPERATIONS}")
    if not isinstance(stdout, str) or not stdout.strip():
        raise ReceiptError("audio receipt is missing or empty")
    try:
        receipt = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReceiptError("audio receipt is not JSON") from error
    if not isinstance(receipt, dict):
        raise ReceiptError("audio receipt must be a JSON object")
    if receipt.get("schema") != SCHEMA:
        raise ReceiptError(f"audio receipt schema must be {SCHEMA}")
    if receipt.get("operation") != operation:
        raise ReceiptError("audio receipt is not bound to the requested operation")
    if type(receipt.get("ok")) is not bool:
        raise ReceiptError("audio receipt field 'ok' must be a boolean")
    if receipt["ok"]:
        if type(receipt.get("muted")) is not bool:
            raise ReceiptError("audio receipt field 'muted' must be a boolean")
    elif receipt.get("error") not in KNOWN_ERRORS:
        raise ReceiptError("audio receipt carries an unrecognized error token")
    return receipt


def _receipt_error_or_none(stdout: object, operation: str) -> str | None:
    try:
        receipt = parse_stdout(stdout, operation)
    except (ReceiptError, ValueError):
        return None
    if not receipt["ok"]:
        return str(receipt["error"])
    return None


def decide(operation: str, payload: dict) -> dict:
    """Verdict for one MAC-verified result envelope bound to its own id.

    Transport first (``wait.classify_result`` owns that wording), receipt
    second. Confirmed success needs all of: no worker timeout, no truncation,
    a recognized status, an exact zero exit, a bound ``lenovo-audio-state/1``
    receipt with ``ok`` true, and ``muted`` true when muting. Anything else is
    never success: terminal failures name a NEW id, while only the still-empty
    wait (handled by ``fetch``/``get.py``) keeps the SAME id.
    """

    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {OPERATIONS}")
    if not isinstance(payload, dict):
        return {"outcome": "unknown", "confirmed": False,
                "detail": "verified result is not an object; never success",
                "action": "re-run as a NEW job id; do not treat as success"}
    transport = waitmod.classify_result(payload)
    if transport["outcome"] != "success":
        token = _receipt_error_or_none(payload.get("stdout"), operation)
        if token is not None:
            transport = dict(transport)
            transport["detail"] = f"{transport['detail']}; lenovo receipt error={token}"
        return transport
    try:
        receipt = parse_stdout(payload.get("stdout"), operation)
    except ReceiptError:
        return {"outcome": "unknown_receipt", "confirmed": True,
                "detail": "verified envelope with an unrecognized audio receipt; "
                          "never success",
                "action": "re-run as a NEW job id; never treat as success"}
    if not receipt["ok"]:
        return {"outcome": "worker_failure", "confirmed": True,
                "detail": f"lenovo audio operation failed: {receipt['error']}; "
                          f"re-run as a NEW job id after fixing the device",
                "action": "fix the Lenovo playback device, then a NEW job id"}
    if operation == "mute" and receipt.get("muted") is not True:
        return {"outcome": "worker_failure", "confirmed": True,
                "detail": "mute did not hold: the read-back state is unmuted; "
                          "re-run as a NEW job id",
                "action": "inspect the Lenovo endpoint, then a NEW job id"}
    state = "muted" if receipt["muted"] else "unmuted"
    return {"outcome": "success", "confirmed": True,
            "detail": f"default playback {state} confirmed for '{operation}'",
            "action": "consume the verified mute state"}


def cmd_build(args: argparse.Namespace) -> int:
    try:
        command = build(args.operation)
    except ValueError as error:
        print(f"audio build refused: {error}", file=sys.stderr)
        return 2
    try:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(command, encoding="utf-8")
    except OSError as error:
        print(f"audio build refused: cannot write command file: {error}",
              file=sys.stderr)
        return 1
    print(json.dumps({"schema": COMMAND_SCHEMA, "operation": args.operation,
                      "commandFile": str(out), "dialect": "powershell",
                      "command": command}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio.py",
        description="Build the signed-queue command file for one Lenovo "
                    "default-playback mute/read operation (writes files only).")
    sub = parser.add_subparsers(dest="command", required=True)
    writer = sub.add_parser("build", help="write one command file, print its JSON")
    writer.add_argument("--operation", required=True, choices=OPERATIONS,
                        help="mute (set true, read back) or read (no change)")
    writer.add_argument("--out", required=True,
                        help="Dell-local command file to write (UTF-8, no newline)")
    writer.set_defaults(func=cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
