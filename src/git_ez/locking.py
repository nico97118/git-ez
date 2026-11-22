# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Serialize git-ez integrations within a checkout on supported Unix platforms."""

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import EzError
from .git import Git


@contextmanager
def checkout_lock(git: Git) -> Iterator[None]:
    """Hold a nonblocking process lock in the checkout's own Git directory."""
    directory = Path(git.text("rev-parse", "--absolute-git-dir"))
    path = directory / "ez-execution.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        raise EzError(f"Cannot open checkout execution lock {path}: {exc}") from exc
    try:
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise EzError(f"Checkout execution lock must be a regular file: {path}")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EzError(
                f"Another git-ez integration is running in this checkout: {git.path}. "
                "Wait for it to finish before retrying."
            ) from exc
        except OSError as exc:
            raise EzError(f"Cannot acquire checkout execution lock {path}: {exc}") from exc
        yield
    finally:
        # Closing releases the lock even on exceptions; Python opens non-inheritable FDs.
        # Never unlink the file: concurrent openers must always lock the same inode.
        os.close(descriptor)
