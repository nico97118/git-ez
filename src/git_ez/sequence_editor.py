# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Capture Git's generated rebase sequence without changing its instructions."""

import argparse
import hashlib
import json
import os
import shlex
import sys
from contextlib import nullcontext, suppress
from pathlib import Path

from git_ez.audit_safety import redact_bytes
from git_ez.audit_storage import publish
from git_ez.audit_storage import read_regular as read_evidence


class SequenceCaptureError(ValueError):
    """A project-owned, safe diagnostic for the bounded native capture."""


def read_regular(path: Path, limit: int) -> bytes:
    """Read bounded regular-file evidence without following symlinks or blocking on FIFOs."""
    if limit <= 0:
        raise SequenceCaptureError("Rebase sequence capture limit must be positive")
    content = read_evidence(path, limit + 1)
    if len(content) > limit:
        raise SequenceCaptureError("Rebase sequence evidence exceeds the audit capture limit")
    return content


def capture_sequence(
    source: Path, destination: Path, limit: int, secrets: tuple[str, ...] = ()
) -> None:
    content = read_regular(source, limit)
    source_size, source_hash = len(content), hashlib.sha256(content).hexdigest()
    content = redact_bytes(content, secrets)
    if len(content) > limit:
        raise SequenceCaptureError("Redacted rebase sequence exceeds the audit capture limit")
    digest = destination.with_name(destination.name + ".sha256")
    # Record the initial fingerprint before returning control to Git's replay and hooks.
    for path, data in (
        (destination, content),
        (digest, hashlib.sha256(content).hexdigest().encode()),
        (
            destination.with_name(destination.name + ".capture.json"),
            json.dumps(
                {
                    "source_bytes": source_size,
                    "source_sha256": source_hash,
                    "retained_sha256": hashlib.sha256(content).hexdigest(),
                    "redacted": source_hash != hashlib.sha256(content).hexdigest(),
                }
            ).encode(),
        ),
    ):
        publish(path, data)


def editor_command(destination: Path, limit: int, redactions_fd: int | None = None) -> str:
    # Git invokes its sequence editor through a shell and appends the todo path.
    args = [sys.executable, str(Path(__file__).resolve()), str(destination), str(limit)]
    if redactions_fd is not None:
        args.extend(["--redactions-fd", str(redactions_fd)])
    return shlex.join(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("limit", type=int)
    parser.add_argument("--redactions-fd", type=int)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        channel_context = (
            os.fdopen(args.redactions_fd, "r+b")
            if args.redactions_fd is not None
            else nullcontext(None)
        )
        with channel_context as channel:
            secrets: tuple[str, ...] = ()
            if channel is not None:
                values = json.load(channel)
                if not isinstance(values, list) or any(
                    not isinstance(value, str) for value in values
                ):
                    raise ValueError("Invalid redaction configuration")
                secrets = tuple(values)

            def report(phase: str) -> None:
                if channel is not None:
                    channel.seek(0)
                    channel.truncate()
                    channel.write(json.dumps({"phase": phase}).encode())
                    channel.flush()

            report("started")
            try:
                capture_sequence(args.source, args.destination, args.limit, secrets)
            except (OSError, ValueError):
                with suppress(OSError):
                    report("failed")
                raise
            report("captured")
    except (OSError, ValueError) as exc:
        diagnostic = str(exc) if isinstance(exc, SequenceCaptureError) else type(exc).__name__
        print(f"git-ez: Cannot capture rebase sequence ({diagnostic})", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
