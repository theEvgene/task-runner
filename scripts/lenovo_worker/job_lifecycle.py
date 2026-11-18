"""Owned subprocess trees and authenticated resource helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import job_tree
from job_tree import TreeTerminationUnknown


def resource_identity(working_directory: str) -> str:
    return "working-directory:" + os.path.normcase(
        os.fspath(Path(working_directory).resolve()))


_PATH_CLAIM_PREFIXES = ("working-directory:", "file:")
_CAPACITY_CLAIM_PREFIX = "capacity:"


def _path_claims(resources: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(item, item.split(":", 1)[1]) for item in resources
            if item.startswith(_PATH_CLAIM_PREFIXES)]


def _exclusive(resources: tuple[str, ...]) -> tuple[str, ...]:
    # A configured ``capacity:`` claim is a shared quota, not an exclusive
    # owner: the reservation referee counts live holders against the
    # explicitly configured limit at publish time.
    return tuple(item for item in resources
                 if not item.startswith(_CAPACITY_CLAIM_PREFIX))


def overlapping_resources(left: tuple[str, ...],
                          right: tuple[str, ...]) -> list[str]:
    """Canonical ``left`` tokens that conflict with ``right``.

    The global fallback overlaps everything. Every other claim is
    exclusive and is compared as an exact token or as a path containment
    either way round, so one worktree and its nested worktree collide.
    ``capacity:`` claims are shared quotas and never conflict here.
    """

    if "exclusive:global" in left or "exclusive:global" in right:
        return ["exclusive:global"]
    exclusive_left = _exclusive(left)
    exclusive_right = _exclusive(right)
    right_tokens = set(exclusive_right)
    conflicts = {item for item in exclusive_left if item in right_tokens}
    right_paths = _path_claims(exclusive_right)
    for token, first in _path_claims(exclusive_left):
        for _, second in right_paths:
            try:
                common = os.path.commonpath((first, second))
            except (OSError, ValueError):
                continue
            if os.path.normcase(common) in {
                    os.path.normcase(first), os.path.normcase(second)}:
                conflicts.add(token)
                break
    return sorted(conflicts)


def resources_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Authenticated resource sets overlap; global fallback overlaps all."""

    return bool(overlapping_resources(left, right))


class OwnedProcess:
    """One subprocess tree whose lifecycle is independent of its neighbors."""

    SETTLE_SECONDS = 3.0

    def __init__(self, command: str, working_directory: str) -> None:
        self.command = command
        self.working_directory = working_directory
        self.process: subprocess.Popen[str] | None = None
        self._job: int | None = None

    def _close_job(self) -> None:
        handle, self._job = self._job, None
        job_tree.close_job(handle)

    def start(self, on_start=None) -> None:
        """Spawn the owned tree without running command code uncontained.

        Windows: the child is created suspended, assigned to a
        kill-on-close Job Object, durably recorded by ``on_start``, and
        only then resumed. Any failure before the resume terminates and
        reaps the still-suspended child, so no command side effect can
        happen outside the ownership boundary.

        POSIX: the process group/session is established by setsid before
        exec, so ownership precedes command code by construction.
        """

        options: dict = {
            "shell": True, "cwd": self.working_directory,
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL, "text": True,
            "encoding": "utf-8", "errors": "replace",
        }
        if os.name == "nt":
            options["creationflags"] = (
                job_tree.CREATE_NEW_PROCESS_GROUP
                | job_tree.CREATE_SUSPENDED)
        else:
            options["start_new_session"] = True
        self.process = subprocess.Popen(self.command, **options)
        if os.name == "nt":
            self._job = job_tree.contain(int(self.process.pid))
            if self._job is None:
                self._abort_unstarted()
                raise TreeTerminationUnknown(
                    "job-object containment unavailable; "
                    "child tree unproven")
            if on_start is not None:
                try:
                    on_start(int(self.process.pid))
                except TreeTerminationUnknown:
                    self._abort_unstarted()
                    raise
                except Exception as error:
                    self._abort_unstarted()
                    raise TreeTerminationUnknown(
                        f"start record unproven ({error})") from error
            if not job_tree.resume_process(int(self.process.pid)):
                self._abort_unstarted()
                raise TreeTerminationUnknown(
                    "suspended child could not be resumed; "
                    "execution unproven")
        elif on_start is not None:
            try:
                on_start(int(self.process.pid))
            except TreeTerminationUnknown:
                raise
            except Exception as error:
                try:
                    self.stop()
                except TreeTerminationUnknown:
                    raise
                except Exception:
                    pass
                raise TreeTerminationUnknown(
                    f"start record unproven ({error})") from error

    def wait(self, timeout_seconds: float) -> tuple[str, str, int]:
        if self.process is None:
            raise RuntimeError("process not started")
        try:
            stdout, stderr = self.process.communicate(
                timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise
        except Exception as error:
            # Post-spawn I/O failure: the child may still own its
            # resources, so its output is unprovable. Kill the tree and
            # report unknown instead of sealing a terminal error.
            try:
                self.stop()
            except TreeTerminationUnknown:
                raise
            except Exception:
                pass
            raise TreeTerminationUnknown(
                "lifecycle I/O failed after spawn; outcome unproven"
            ) from error
        return stdout or "", stderr or "", int(self.process.returncode)

    def settle_tree(self) -> None:
        """Prove no descendant survives a normal exit (kill stragglers).

        Raises TreeTerminationUnknown when containment or termination is
        unproven, or when a descendant survives the bound. Never seals a
        terminal result on unproven containment.
        """

        try:
            if self.process is not None:
                if os.name == "nt":
                    if self._job is None:
                        raise TreeTerminationUnknown(
                            "job-object containment unproven; "
                            "terminal result refused")
                    if not job_tree.terminate_job(self._job):
                        try:
                            job_tree.reap_descendants(
                                int(self.process.pid), self.SETTLE_SECONDS)
                        except Exception:
                            pass
                        raise TreeTerminationUnknown(
                            "job-object termination unproven; "
                            "terminal result refused")
                job_tree.reap_descendants(
                    int(self.process.pid), self.SETTLE_SECONDS)
        finally:
            self._close_job()

    def _abandon_pipes(self) -> None:
        assert self.process is not None
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _abort_unstarted(self) -> None:
        """Terminate and reap a child whose command code never ran."""

        proc = self.process
        assert proc is not None
        try:
            if self._job is not None:
                job_tree.terminate_job(self._job)
        except Exception:
            pass
        finally:
            self._close_job()
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.communicate(timeout=self.SETTLE_SECONDS)
        except Exception:
            self._abandon_pipes()
            try:
                proc.wait(timeout=self.SETTLE_SECONDS)
            except Exception:
                pass

    def stop(self) -> tuple[str, str, int, str | None]:
        """Stop the complete tree, or raise promptly without claiming terminal."""

        if self.process is None:
            raise RuntimeError("process not started")
        proc = self.process
        if proc.poll() is None:
            if os.name == "nt":
                try:
                    killed = subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=self.SETTLE_SECONDS, check=False)
                except (OSError, subprocess.SubprocessError) as error:
                    self._abandon_pipes()
                    raise TreeTerminationUnknown(
                        "taskkill tree termination was not observed") from error
                if killed.returncode != 0:
                    self._abandon_pipes()
                    raise TreeTerminationUnknown(
                        f"taskkill tree termination failed ({killed.returncode})")
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    self._abandon_pipes()
                    raise TreeTerminationUnknown(
                        "process-group termination was not observed") from error
        if os.name == "nt":
            if self._job is None:
                # No proven containment: a detached survivor may outlive
                # taskkill /T. Stay unknown instead of claiming terminal.
                self._abandon_pipes()
                raise TreeTerminationUnknown(
                    "job-object containment unproven; "
                    "terminal result refused")
            # Job members include detached grandchildren that taskkill /T
            # cannot reach; terminate them before collecting output.
            if not job_tree.terminate_job(self._job):
                self._abandon_pipes()
                raise TreeTerminationUnknown(
                    "job-object termination unproven; "
                    "terminal result refused")
        self._close_job()
        try:
            stdout, stderr = proc.communicate(timeout=self.SETTLE_SECONDS)
        except subprocess.TimeoutExpired as error:
            self._abandon_pipes()
            raise TreeTerminationUnknown(
                "terminated tree did not settle within the bound") from error
        # The tree kill above must leave no detached survivor behind.
        job_tree.reap_descendants(int(proc.pid), self.SETTLE_SECONDS)
        code = int(proc.returncode)
        signal_name = None
        if os.name != "nt" and code < 0:
            try:
                signal_name = signal.Signals(-code).name
            except ValueError:
                signal_name = f"signal-{code}"
        return stdout or "", stderr or "", code, signal_name


def execute(command: str, working_directory: str, timeout_seconds: int,
            output_limit: int, on_start=None) -> dict:
    started = time.monotonic()
    owned = OwnedProcess(command, working_directory)
    # The start hook is part of the ownership boundary: on Windows it
    # runs after containment but before the suspended child is resumed;
    # a hook failure terminates the still-suspended child and seals
    # nothing.
    owned.start(on_start)
    try:
        stdout, stderr, code = owned.wait(timeout_seconds)
    except subprocess.TimeoutExpired:
        stdout, stderr, process_code, signal_name = owned.stop()
        detail = f"timed out on the worker after {timeout_seconds}s"
        if stderr:
            detail += "\n" + stderr
        return {
            "status": "error", "exit_code": 124,
            "process_exit_code": process_code, "termination": "timeout",
            "signal": signal_name, "stdout": stdout[:output_limit],
            "stderr": detail[:output_limit], "timed_out": True,
            "output_truncated": (len(stdout) > output_limit
                                 or len(detail) > output_limit),
            "elapsed_seconds": time.monotonic() - started,
        }
    # A normal shell exit seals nothing until the whole tree is proven
    # absent; a detached survivor is killed here, or the job stays
    # owned/unknown instead of sealing a false success.
    owned.settle_tree()
    return {
        "status": "ok" if code == 0 else "error", "exit_code": code,
        "process_exit_code": code, "termination": "exit", "signal": None,
        "stdout": stdout[:output_limit], "stderr": stderr[:output_limit],
        "timed_out": False,
        "output_truncated": (len(stdout) > output_limit
                             or len(stderr) > output_limit),
        "elapsed_seconds": time.monotonic() - started,
    }
