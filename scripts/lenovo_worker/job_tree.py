"""Owned-tree containment and descendant verification (stdlib only).

Two layers, both enforced after every normal exit and every stop:

1. Containment (Windows): the child is created suspended, assigned to a
   Job Object, published as a durable start record, and only then
   resumed, so no command code runs before kill-on-close ownership
   exists. Even a deliberately detached grandchild (DETACHED_PROCESS
   with closed handles) cannot escape the job. The worker terminates the
   job explicitly before sealing, then verifies.
2. Sweep + verify (all platforms): enumerate the transitive descendants
   of the shell pid (plus same-group members on POSIX), kill stragglers,
   and re-check within a bound. Survivors raise TreeTerminationUnknown
   instead of sealing success.

Windows inventory uses a Toolhelp32 snapshot via ctypes (no powershell
dependency). POSIX inventory scans /proc with a process-group fallback
for kernels without /proc: killpg covers group members, but a
double-fork setsid daemon that reparents to init is invisible to a
ppid walk and remains a documented producer-level boundary.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time


class TreeTerminationUnknown(RuntimeError):
    """The worker could not prove that the complete child tree stopped."""


class _InventoryUnavailable(RuntimeError):
    """No descendant inventory mechanism on this host."""


# CreateProcess flags used before any command code is allowed to run.
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200


# ---------------------------------------------------------------------------
# Containment


def contain(child_pid: int) -> int | None:
    """Assign a live child to a job object (Windows). Else None.

    No limit flags except kill-on-close: when the last job handle closes
    (including a worker crash), every member dies, so a crash cannot leave
    the claimed tree active. Termination is explicit via terminate_job(),
    which kills every member including deliberately detached grandchildren
    (only CREATE_BREAKAWAY_FROM_JOB escapes, and the worker never grants
    it). Returns None on any failure; the caller must fail closed.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p,
                                              ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool,
                                         ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p,
                                                      ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p,
                                                    ctypes.c_int,
                                                    ctypes.c_void_p,
                                                    ctypes.c_ulong]
        kernel32.SetInformationJobObject.restype = ctypes.c_bool
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION with only
        # BasicLimitInformation.LimitFlags =
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000). The extended
        # struct is 144 bytes on 64-bit; LimitFlags sits at offset 16
        # (after two LARGE_INTEGER time limits).
        try:
            import struct

            buf = bytearray(144)
            struct.pack_into("<I", buf, 16, 0x2000)
            ok = kernel32.SetInformationJobObject(
                job, 9, bytes(buf), 144)
            if not ok:
                kernel32.CloseHandle(job)
                return None
        except Exception:
            try:
                kernel32.CloseHandle(job)
            except Exception:
                pass
            return None
        proc = kernel32.OpenProcess(0x0101, False, int(child_pid))
        if not proc:
            kernel32.CloseHandle(job)
            return None
        try:
            if not kernel32.AssignProcessToJobObject(job, proc):
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(proc)
        return int(job)
    except Exception:
        return None


def resume_process(pid: int) -> bool:
    """Resume every thread of a child created with CREATE_SUSPENDED.

    A process created suspended runs no command code and cannot create
    further threads, so its primary thread is the only one to resume.
    Returns False when any thread cannot be observed or resumed; the
    caller must then terminate the still-suspended child and stay
    unknown instead of assuming it started.
    """

    if os.name != "nt":
        return True
    try:
        import ctypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ThreadID", ctypes.c_ulong),
                ("th32OwnerProcessID", ctypes.c_ulong),
                ("tpBasePri", ctypes.c_long),
                ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong,
                                                      ctypes.c_ulong]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Thread32First.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32First.restype = ctypes.c_bool
        kernel32.Thread32Next.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32Next.restype = ctypes.c_bool
        kernel32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_bool,
                                        ctypes.c_ulong]
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel32.ResumeThread.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return False
        resumed = 0
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
                return False
            while True:
                if int(entry.th32OwnerProcessID) == int(pid):
                    thread = kernel32.OpenThread(
                        0x0002, False, int(entry.th32ThreadID))
                    if not thread:
                        return False
                    try:
                        if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            return False
                        resumed += 1
                    finally:
                        kernel32.CloseHandle(thread)
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            try:
                kernel32.CloseHandle(snapshot)
            except Exception:
                pass
        return resumed > 0
    except Exception:
        return False


def terminate_job(handle: int | None) -> bool:
    """Terminate every member of a job object. True when none can remain.

    On Windows a None handle means no containment was established, so
    termination is unproven and the result is False (fail closed). On
    POSIX there is no job object; None means nothing to terminate.
    """

    if os.name != "nt":
        return True
    if handle is None:
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p,
                                                ctypes.c_uint]
        kernel32.TerminateJobObject.restype = ctypes.c_bool
        return bool(kernel32.TerminateJobObject(handle, 0))
    except Exception:
        return False


def close_job(handle: int | None) -> None:
    """Release a job handle. Terminate first; close alone kills nothing."""

    if handle is None or os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle(handle)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Inventory


def _windows_process_table() -> dict[int, int]:
    import ctypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong),
            ("th32Threads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong,
                                                  ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ProcessEntry)]
    kernel32.Process32FirstW.restype = ctypes.c_bool
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise _InventoryUnavailable("process snapshot unavailable")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        table: dict[int, int] = {}
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return table
        while True:
            table[int(entry.th32ProcessID)] = int(
                entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return table
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass


def _posix_process_table() -> dict[int, int]:
    table: dict[int, int] = {}
    try:
        names = os.listdir("/proc")
    except OSError as error:
        raise _InventoryUnavailable(f"/proc unavailable ({error})") from error
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                data = handle.read(512)
        except OSError:
            continue
        try:
            rest = data.split(b")", 1)[1].split()
            table[int(name)] = int(rest[1])
        except (IndexError, ValueError):
            continue
    return table


def descendants(root_pid: int) -> list[int]:
    """Live transitive descendants of root_pid (root excluded)."""

    if os.name == "nt":
        table = _windows_process_table()
    else:
        table = _posix_process_table()
    children: dict[int, list[int]] = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    seen: list[int] = []
    stack = [int(root_pid)]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child != int(root_pid) and child not in seen:
                seen.append(child)
                stack.append(child)
    return sorted(seen)


def _kill_member(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False)
        else:
            os.kill(int(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def _reap_group(pgid: int, timeout_seconds: float) -> None:
    """Fallback without /proc: the process group is the whole tree."""

    try:
        os.killpg(int(pgid), signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as error:
        raise TreeTerminationUnknown(
            f"process group {pgid} termination was not observed "
            f"({error})") from error
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.killpg(int(pgid), 0)
        except ProcessLookupError:
            return
        except OSError as error:
            raise TreeTerminationUnknown(
                f"process group {pgid} state was not observed "
                f"({error})") from error
        if time.monotonic() >= deadline:
            raise TreeTerminationUnknown(
                f"process group {pgid} survived termination")
        time.sleep(0.05)


def _posix_pg_members(pgid: int) -> list[int]:
    """Live processes in pgid (catches reparented group members)."""

    members: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError as error:
        raise _InventoryUnavailable(
            f"/proc unavailable ({error})") from error
    for name in names:
        if not name.isdigit() or int(name) == int(pgid):
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                data = handle.read(512)
        except OSError:
            continue
        try:
            if int(data.split(b")", 1)[1].split()[2]) == int(pgid):
                members.append(int(name))
        except (IndexError, ValueError):
            continue
    return sorted(members)


def _live_members(root_pid: int) -> list[int]:
    try:
        alive = descendants(int(root_pid))
    except _InventoryUnavailable:
        if os.name == "nt":
            raise TreeTerminationUnknown(
                "descendant inventory was not observed")
        _reap_group(int(root_pid), 3.0)
        return []
    if os.name != "nt":
        try:
            alive = sorted(
                set(alive) | set(_posix_pg_members(int(root_pid))))
        except _InventoryUnavailable:
            pass
    return alive


def reap_descendants(root_pid: int, timeout_seconds: float = 3.0) -> None:
    """Kill stragglers under root_pid and prove none survive.

    Returns when no descendant is observable. Raises
    TreeTerminationUnknown when inventory is impossible (non-Windows
    host without /proc and an unprovable group) or a descendant
    survives the bound.
    """

    try:
        alive = _live_members(int(root_pid))
    except _InventoryUnavailable:
        if os.name == "nt":
            raise TreeTerminationUnknown(
                "descendant inventory was not observed")
        _reap_group(int(root_pid), timeout_seconds)
        return
    if not alive:
        return
    for pid in alive:
        _kill_member(pid)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            alive = _live_members(int(root_pid))
        except _InventoryUnavailable:
            if os.name == "nt":
                raise TreeTerminationUnknown(
                    "descendant inventory was not observed")
            _reap_group(int(root_pid), deadline - time.monotonic())
            return
        if not alive:
            return
        if time.monotonic() >= deadline:
            raise TreeTerminationUnknown(
                f"descendant tree survived termination: {alive[:8]}")
        time.sleep(0.05)
