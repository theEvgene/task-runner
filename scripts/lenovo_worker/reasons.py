"""One owner of launch and readiness refusal reasons (stdlib only).

A reason is concluded only from an observation that supports it: a generic
SMB mapping failure is not proof of bad credentials, and a missing path is
not proof of an unreachable host. Conflicting or insufficient observations
become ``unknown`` with one bounded next action.

Nothing here contacts a host, retries a credential, or changes a share,
firewall, or account. Every field that leaves this module is redacted first
(``sanitize``), so a reason never carries a credential, a host name, or a
private absolute path.
"""

from __future__ import annotations

import os
import re

SCHEMA = "lenovo-launch-reason/1"
UNKNOWN = "unknown"
REMOTE_CWD = ("unc", "network-drive")

REASONS: dict[str, dict] = {
    "none": {"state": "ready", "detail": "worker and cleaner launch is ready",
             "nextAction": "none"},
    "config-unreadable": {"state": "refused", "detail": "cannot read config",
                          "nextAction": "point the shortcut at the local JSON config"},
    "config-invalid": {"state": "refused", "detail": "config is invalid",
                       "nextAction": "fix the named local config field; make no host change"},
    "client-missing": {"state": "refused", "detail": "bridge client not found",
                       "nextAction": "restore the pinned scripts/lan_bridge delivery"},
    "launch-cwd-remote": {
        "state": "refused", "detail": "launch cwd is a UNC path or network drive",
        "nextAction": "start the launcher from a local fixed-disk directory; "
                      "a UNC path or mapped network drive cannot host the worker"},
    "launch-cwd-relative": {
        "state": "refused", "detail": "launch cwd is not an absolute path",
        "nextAction": "start the launcher from an absolute local directory"},
    "launch-cwd-unverifiable": {
        "state": "refused", "detail": "launch cwd cannot be proven local",
        "nextAction": "start the launcher from an absolute local fixed-disk directory"},
    "launch-cwd-invalid": {
        "state": "refused", "detail": "launch directory is absent or not a directory",
        "nextAction": "create that directory or point workerCwd at an existing local "
                      "directory; nothing was started"},
    "shared-dir-invalid": {
        "state": "refused", "detail": "the share is mapped but sharedDir is not a directory",
        "nextAction": "point sharedDir at the existing mapped share directory; "
                      "nothing was created"},
    "smb-auth-refused": {
        "state": "refused",
        "detail": "bad credentials: SMB refused the username/password (check credential file)",
        "nextAction": "correct the credential file for the recorded account; "
                      "no automatic retry or host change"},
    "smb-credential-file-invalid": {
        "state": "refused", "detail": "bad credentials: credential file is empty or malformed",
        "nextAction": "write one single-line password into the local credential file"},
    "smb-share-unconfirmed": {
        "state": "refused", "detail": "share unavailable: check path/mount",
        "nextAction": "check host, share name, cable or VPN, then re-run; a generic "
                      "mapping failure is not proof of bad credentials"},
    "queue-missing": {"state": "refused", "detail": "queue unavailable: check path/mount",
                      "nextAction": "point queueDir at the existing queue; nothing was created"},
    "worker-missing": {"state": "refused", "detail": "worker delivery missing: check workerDir",
                       "nextAction": "restore the pinned worker and cleaner delivery in workerDir"},
    "key-unusable": {"state": "refused", "detail": "key unusable",
                     "nextAction": "re-issue the key outside the queue, the share, and the checkout"},
    "key-misplaced": {"state": "refused", "detail": "key inside the queue, share, or checkout",
                      "nextAction": "move the key outside the queue, the share, and the checkout"},
    "layout-overlap": {"state": "refused",
                       "detail": "trash/state dirs must be outside the queue and the share",
                       "nextAction": "move trashDir and stateDir outside both; nothing was created"},
    "launch-busy": {"state": "refused", "detail": "another launch is in progress",
                    "nextAction": "re-run to adopt the pids of the launch in progress"},
    "role-ambiguous": {"state": "refused", "detail": "ambiguous role state",
                       "nextAction": "verify the role and follow the named state record in detail; "
                                     "the launcher never kills"},
    "role-start-failed": {"state": "refused", "detail": "a role exited at once",
                          "nextAction": "read the role log under stateDir"},
    "tool-missing": {"state": "refused", "detail": "required executable is absent",
                     "nextAction": "install or select the required executable"},
    "component-unconfirmed": {"state": "refused",
                              "detail": "a required local component is not confirmed",
                              "nextAction": "restore the named component to the selected revision"},
    "preflight-incomplete": {"state": "refused", "detail": "local preflight is not confirmed",
                             "nextAction": "restore the failing local requirement, then re-run; "
                                           "no signed job was published"},
    UNKNOWN: {
        "state": "unknown", "detail": "observations conflict or are insufficient",
        "nextAction": "re-read the same report and verify the share manually; never "
                      "retry credentials or run a signed job while the outcome is unknown"},
}


class Refusal(Exception):
    """One classified refusal: a reason code, bounded detail, raw observations."""

    def __init__(self, reason: str = UNKNOWN, detail: str = "",
                 observations: dict | None = None) -> None:
        self.reason = reason if reason in REASONS else UNKNOWN
        self.detail = str(detail or "")
        self.observations = dict(observations or {})
        super().__init__(self.detail or REASONS[self.reason]["detail"])

    def __str__(self) -> str:
        return self.detail or REASONS[self.reason]["detail"]


_PATH_RE = re.compile(r"(?<![\w<])(?:[A-Za-z]:[\\/]|\\\\|//)[^\s\"'(),;]*")


def sanitize(text: object) -> str:
    """Replace drive-letter, UNC, and device paths with one neutral token."""

    return _PATH_RE.sub("<redacted-path>", str(text or ""))


def _drive_type(drive: str) -> int | None:
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive)))
    except Exception:
        return None


def cwd_safety(path: object) -> str:
    """Classify a working directory: local, unc, network-drive, relative, unverifiable."""

    text = "" if path is None else os.fspath(path)
    if not text:
        return "unverifiable"
    lowered = text.lower()
    if lowered.startswith("\\\\?\\unc\\"):
        return "unc"
    if lowered.startswith(("\\\\?\\", "\\\\.\\")):
        return "unverifiable"
    if text.startswith(("\\\\", "//")):
        return "unc"
    if not os.path.isabs(text):
        return "relative"
    if os.name != "nt":
        return "local"
    drive, _ = os.path.splitdrive(text)
    if len(drive) != 2 or drive[1] != ":":
        return "unverifiable"
    kind = _drive_type(drive + "\\")
    if kind is None or kind in (0, 1):  # unknown / no root dir
        return "unverifiable"
    return "network-drive" if kind == 4 else "local"  # 4 = DRIVE_REMOTE


def share_status(first_line: str, exit_code: int | None) -> str:
    """Normalize the bounded PowerShell share probe into one observation token."""

    token = (first_line or "").strip()
    if token.startswith(("ALREADY:", "MAPPED:")):
        return "MAPPED"
    if token == "BAD-CREDENTIAL-FILE":
        return "BAD-CREDENTIAL-FILE"
    if token == "AUTH-DENIED":
        return "AUTH-DENIED"
    if token.startswith("MAP-FAILED"):
        return "MAP-FAILED"
    return "UNOBSERVED"


def classify_cwd(observations: dict) -> Refusal | None:
    for key, presence_key, label in (
            ("cwd", "cwdPresent", "launcher cwd"),
            ("workerCwd", "workerCwdPresent", "worker cwd")):
        kind = observations.get(key)
        if kind in REMOTE_CWD:
            return Refusal("launch-cwd-remote", f"{label} is remote; refusing before spawn",
                           observations)
        if kind == "relative":
            return Refusal("launch-cwd-relative", f"{label} is not absolute", observations)
        if kind == "unverifiable":
            return Refusal("launch-cwd-unverifiable", f"{label} cannot be proven local",
                           observations)
        # A local absolute path still has to be a usable directory: a missing,
        # file-typed, or inaccessible one would otherwise escape as an OSError
        # from the spawn instead of a classified receipt.
        if observations.get(presence_key) is False:
            return Refusal("launch-cwd-invalid",
                           f"{label} is absent or not a directory; nothing was started",
                           observations)
    return None


def classify(observations: dict) -> Refusal | None:
    """Return the supported refusal, or None when every observation is confirmed."""

    verdict = classify_cwd(observations)
    if verdict is not None:
        return verdict
    status = observations.get("shareStatus")
    visible = observations.get("shareVisible")
    if status == "BAD-CREDENTIAL-FILE" or status == "AUTH-DENIED":
        if visible is True:
            return Refusal(UNKNOWN, "auth was refused while the share path is visible",
                           observations)
        return Refusal("smb-credential-file-invalid" if status == "BAD-CREDENTIAL-FILE"
                       else "smb-auth-refused", observations=observations)
    if status == "MAP-FAILED":
        if visible is True:
            return Refusal(UNKNOWN, "mapping failed while the share path is visible",
                           observations)
        # A generic mapping failure supports neither "host unreachable" nor
        # "share name absent": both need an observation this bounded probe does
        # not produce, so the reason stays unconfirmed instead of guessed.
        return Refusal("smb-share-unconfirmed", observations=observations)
    if status == "MAPPED":
        if visible is True and observations.get("sharePathPresent") is False:
            # The share itself is mapped; only the configured sharedDir is
            # wrong. Keep the two facts separate so the receipt stays true.
            return Refusal("shared-dir-invalid", observations=observations)
        if visible is False:
            return Refusal(UNKNOWN, "share reported mapped but its path is not visible",
                           observations)
    if observations.get("queuePresent") is False:
        return Refusal("queue-missing", observations=observations)
    if observations.get("workerPresent") is False:
        return Refusal("worker-missing", observations=observations)
    if status == "UNOBSERVED" or (status is None and observations.get("queuePresent") is None):
        return Refusal(UNKNOWN, "share status could not be observed", observations)
    return None


def classify_local(observations: dict) -> Refusal | None:
    """Local readiness facts only; a missing key never implies a bad credential."""

    if observations.get("queuePresent") is False:
        return Refusal("queue-missing", observations=observations)
    if observations.get("keyPresent") is False:
        return Refusal("key-unusable", observations=observations)
    if observations.get("toolPresent") is False:
        return Refusal("tool-missing", observations=observations)
    if observations.get("componentPresent") is False:
        return Refusal("component-unconfirmed", observations=observations)
    return None


def describe(refusal: Refusal) -> dict:
    """Bounded, path-free receipt fields for one refusal."""

    entry = REASONS[refusal.reason]
    detail = sanitize(refusal.detail) or entry["detail"]
    evidence = []
    for key in sorted(refusal.observations):
        value = refusal.observations[key]
        if value is None:
            continue
        evidence.append(sanitize(f"{key}={value}")[:120])
    return {"schema": SCHEMA, "state": entry["state"], "reason": refusal.reason,
            "detail": detail[:300], "nextAction": entry["nextAction"],
            "evidence": evidence[:8]}
