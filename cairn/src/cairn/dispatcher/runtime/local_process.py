from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from contextlib import suppress

from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
STREAM_JOIN_TIMEOUT_SECONDS = 5.0
FORCE_KILL_REAP_TIMEOUT_SECONDS = 2.0
TASKKILL_TIMEOUT_SECONDS = 10.0

IS_WINDOWS = os.name == "nt"
# SIGKILL is POSIX-only. On Windows the hard kill goes through taskkill /F instead,
# and this constant is only a label for which branch _signal_group should take.
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


class LocalProcess:
    """Runs a worker command as a host subprocess.

    Mirrors the container ManagedProcess surface (start/communicate/kill/cancel) but
    executes on the dispatcher host: its own process group so children are killed as a
    group, a Python-enforced timeout instead of the ``timeout`` coreutil, and a
    graceful-then-forced shutdown so the CLI can flush its session before dying.

    The group handling is platform-specific. POSIX gets its own session via setsid and
    is signalled with killpg. Windows has neither, so the child is started in a new
    process group, asked to stop with CTRL_BREAK_EVENT, then torn down with taskkill /T.
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int | None = None,
        term_grace_seconds: int = 5,
    ):
        self.command = command
        self.env = env
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._term_grace = max(1.0, float(term_grace_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._stdout_chunks: list[str] = []
        self._stderr_chunks: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._kill_lock = threading.Lock()

    def start(self) -> None:
        # setsid does not exist on Windows; CREATE_NEW_PROCESS_GROUP is the closest
        # equivalent and is also what makes CTRL_BREAK_EVENT deliverable later.
        group_kwargs: dict[str, object] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if IS_WINDOWS
            else {"start_new_session": True}
        )
        self._process = subprocess.Popen(
            self.command,
            cwd=self._cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **group_kwargs,
        )
        self._stdout_thread = threading.Thread(
            target=self._drain, args=(self._process.stdout, self._stdout_chunks), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain, args=(self._process.stderr, self._stderr_chunks), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        wait_for = float(self._timeout_seconds) if self._timeout_seconds is not None else timeout
        try:
            self._process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self._terminate()
        with suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=FORCE_KILL_REAP_TIMEOUT_SECONDS)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        returncode = self._process.returncode
        if returncode is None:
            returncode = 137 if self._timed_out else 1
        return ProcessResult(
            returncode=returncode,
            stdout="".join(self._stdout_chunks),
            stderr="".join(self._stderr_chunks),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        self._terminate()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self._terminate()

    def _terminate(self) -> None:
        with self._kill_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            self._signal_group(process, hard=False)
            try:
                process.wait(timeout=self._term_grace)
                return
            except subprocess.TimeoutExpired:
                pass
            self._signal_group(process, hard=True)

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], *, hard: bool) -> None:
        """Ask the whole worker group to stop (``hard=False``) or force it down.

        Taking the step as a flag rather than a signal number matters on Windows,
        where there is no SIGKILL to tell the two steps apart.
        """
        if IS_WINDOWS:
            LocalProcess._signal_group_windows(process, hard=hard)
            return
        try:
            os.killpg(os.getpgid(process.pid), KILL_SIGNAL if hard else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            with suppress(ProcessLookupError, PermissionError, ValueError):
                process.send_signal(KILL_SIGNAL if hard else signal.SIGTERM)

    @staticmethod
    def _signal_group_windows(process: subprocess.Popen[str], *, hard: bool) -> None:
        """Approximate the POSIX group signalling with what Windows offers.

        CTRL_BREAK_EVENT reaches every process in the group created by
        CREATE_NEW_PROCESS_GROUP, so it stands in for SIGTERM-to-group. For the hard
        kill, taskkill /T ends the whole child tree; Popen.kill() would end only the
        direct child and orphan whatever the worker CLI spawned.
        """
        if not hard:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                return
            except (OSError, ValueError):
                pass  # fall through to the forced kill
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TASKKILL_TIMEOUT_SECONDS,
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
        with suppress(OSError, ValueError):
            process.kill()

    @staticmethod
    def _drain(pipe, sink: list[str]) -> None:
        try:
            for chunk in iter(lambda: pipe.read(READ_CHUNK_SIZE), ""):
                sink.append(chunk)
        except (ValueError, OSError):
            pass
        finally:
            with suppress(Exception):
                pipe.close()
