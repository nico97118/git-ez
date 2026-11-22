# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Capture Git's generated rebase sequence without changing its instructions."""

import argparse
import hashlib
import os
import shlex
import stat
import sys
from pathlib import Path

from git_ez.audit_storage import publish


def read_regular(path: Path, limit: int) -> bytes:
    """Read bounded regular-file evidence without following symlinks or blocking on FIFOs."""
    if limit <= 0:
        raise ValueError("Rebase sequence capture limit must be positive")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Rebase sequence evidence must be a regular file")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Rebase sequence evidence exceeds the audit capture limit")
    return content


def capture_sequence(source: Path, destination: Path, limit: int) -> None:
    content = read_regular(source, limit)
    digest = destination.with_name(destination.name + ".sha256")
    # Record the initial fingerprint before returning control to Git's replay and hooks.
    for path, data in (
        (destination, content),
        (digest, hashlib.sha256(content).hexdigest().encode()),
    ):
        publish(path, data)


def editor_command(destination: Path, limit: int) -> str:
    # Git invokes its sequence editor through a shell and appends the todo path.
    return shlex.join([sys.executable, str(Path(__file__).resolve()), str(destination), str(limit)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("limit", type=int)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        capture_sequence(args.source, args.destination, args.limit)
    except (OSError, ValueError) as exc:
        print(f"git-ez: Cannot capture rebase sequence: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
