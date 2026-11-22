# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Descriptor-based private audit storage and atomic snapshot publication."""

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4


def encode_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()


@contextmanager
def directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    """Walk from the filesystem root without following directory symlinks."""
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
        os.close(descriptor)


@contextmanager
def regular_fd(path: Path) -> Iterator[int]:
    with directory_fd(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Audit evidence must be a regular file")
        yield descriptor
    finally:
        os.close(descriptor)


def read_regular(path: Path, limit: int | None = None) -> bytes:
    with regular_fd(path) as descriptor:
        result = bytearray()
        while limit is None or len(result) < limit:
            amount = 65536 if limit is None else min(65536, limit - len(result))
            block = os.read(descriptor, amount)
            if not block:
                break
            result.extend(block)
        return bytes(result)


def fingerprint(path: Path, prefix: int | None = None) -> tuple[int, str]:
    with regular_fd(path) as descriptor:
        digest = hashlib.sha256()
        size = 0
        while prefix is None or size < prefix:
            amount = 65536 if prefix is None else min(65536, prefix - size)
            block = os.read(descriptor, amount)
            if not block:
                break
            digest.update(block)
            size += len(block)
        if prefix is not None and size != prefix:
            raise ValueError("Audit stream is shorter than its committed prefix")
        return size, digest.hexdigest()


def publish(path: Path, content: bytes | Iterable[bytes], *, replace: bool = False) -> None:
    """Publish a regular file; snapshots replace atomically, captures never overwrite."""
    with directory_fd(path.parent, create=True) as parent:
        temporary = f".tmp-{uuid4().hex}"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            try:
                for block in (content,) if isinstance(content, bytes) else content:
                    position = 0
                    while position < len(block):
                        written = os.write(descriptor, block[position:])
                        if written <= 0:
                            raise OSError("Incomplete audit write")
                        position += written
            finally:
                os.close(descriptor)
            if replace:
                try:
                    info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("Audit destination must be a regular file")
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
            # Cleanup cannot replace a failure or deny publication.
            with suppress(OSError):
                os.unlink(temporary, dir_fd=parent)


def write_json(path: Path, value: object) -> None:
    """Standalone atomic JSON output, also used by the provider probe."""
    publish(path, encode_json(value), replace=True)


class AuditStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.owned = False
        self._streams: dict[str, int] = {}

    def initialize(self) -> None:
        with directory_fd(self.directory.parent, create=True) as parent:
            os.mkdir(self.directory.name, mode=0o700, dir_fd=parent)
        self.owned = True

    def path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or not path.parts or any(part in ("..", ".") for part in path.parts):
            raise ValueError("Unsafe audit artifact path")
        return self.directory / path

    def write(
        self, relative: str, content: bytes | Iterable[bytes], *, replace: bool = False
    ) -> None:
        if not self.owned:
            raise OSError("Audit directory is not owned by this run")
        publish(self.path(relative), content, replace=replace)

    def snapshot(self, value: object) -> bytes:
        content = encode_json(value)
        self.write("run.json", content, replace=True)
        return content

    def append(self, relative: str, content: bytes) -> None:
        if not self.owned:
            raise OSError("Audit directory is not owned by this run")
        path = self.path(relative)
        with directory_fd(path.parent, create=True) as parent:
            descriptor = os.open(
                path.name,
                os.O_WRONLY
                | os.O_APPEND
                | (os.O_CREAT | os.O_EXCL if relative not in self._streams else 0)
                | os.O_NOFOLLOW
                | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Audit stream must be a regular file")
            if info.st_size != self._streams.get(relative, 0):
                raise ValueError("Audit stream no longer matches its acknowledged size")
            if os.write(descriptor, content) != len(content):
                raise OSError("Incomplete audit append")
        finally:
            os.close(descriptor)
        self._streams[relative] = self._streams.get(relative, 0) + len(content)
