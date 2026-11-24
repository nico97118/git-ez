# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Wait for provisionally mutating child processes before restoring the worktree."""

import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Event, current_thread, main_thread
from time import monotonic, sleep

from .conflict_state import uninterrupted
from .errors import EzError

_active: set[subprocess.Popen[bytes]] = set()
_cancellation: Event | None = None


class ChildCleanupError(EzError):
    """Restoration cannot start while a child group might still write files."""


def stop_child(child: subprocess.Popen[bytes]) -> None:
    try:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        deadline = monotonic() + 5
        while True:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            if monotonic() >= deadline:
                raise ChildCleanupError("Child process group did not terminate")
            sleep(0.01)
    except OSError as exc:
        raise ChildCleanupError("Cannot confirm child process group termination") from exc


@contextmanager
def interrupt_children() -> Iterator[None]:
    global _cancellation
    cancellation = Event()
    previous_cancellation = _cancellation
    _cancellation = cancellation

    def stop(signum: int, frame: object) -> None:
        # Keep repeated interrupts ignored until the owner finishes restoration.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        cancellation.set()
        with uninterrupted():
            for child in list(_active):
                stop_child(child)
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        _cancellation = previous_cancellation


def run_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    stdin: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    cancellation = _cancellation
    child: subprocess.Popen[bytes] | None = None
    interrupted = False

    def defer(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    # Defer Python's interrupt exception until the new child has been registered.
    # Unlike blocking SIGINT, this does not change the child's signal mask.
    primary = current_thread() is main_thread()
    previous = signal.signal(signal.SIGINT, defer) if primary else None
    try:
        try:
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            _active.add(child)
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
        if interrupted:
            signal.raise_signal(signal.SIGINT)
        if cancellation is not None and cancellation.is_set():
            raise KeyboardInterrupt
        stdout, stderr = child.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    finally:
        if child is not None:
            with uninterrupted():
                stop_child(child)
                _active.discard(child)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()
