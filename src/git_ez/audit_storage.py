# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Private files, no-follow reads and atomic JSON writes shared with the probe."""

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4


def encode_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()


@contextmanager
def directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        with suppress(OSError):
            os.close(descriptor)


@contextmanager
def regular_fd(path: Path) -> Iterator[int]:
    descriptor = None
    try:
        with directory_fd(path.parent) as parent:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Expected a regular file")
        yield descriptor
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def read_regular(path: Path, limit: int | None = None) -> bytes:
    with regular_fd(path) as descriptor:
        result = bytearray()
        while limit is None or len(result) < limit:
            block = os.read(descriptor, 65536 if limit is None else min(65536, limit - len(result)))
            if not block:
                break
            result.extend(block)
        return bytes(result)


def publish(path: Path, content: bytes, *, replace: bool = False) -> None:
    with directory_fd(path.parent, create=True) as parent:
        temporary = f".tmp-{uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            try:
                position = 0
                while position < len(content):
                    count = os.write(descriptor, content[position:])
                    if count <= 0:
                        raise OSError("Incomplete file write")
                    position += count
            finally:
                os.close(descriptor)
            if replace:
                try:
                    info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("Destination must be a regular file")
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
        finally:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=parent)


def write_json(path: Path, value: object) -> None:
    publish(path, encode_json(value), replace=True)
