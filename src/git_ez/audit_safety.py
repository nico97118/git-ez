# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Credential filtering shared by snapshots, streams and the native sequence helper."""

import re
from collections.abc import Iterable, Iterator

MARKER = b"<redacted>"


def redact_chunks(source: Iterable[bytes], secrets: tuple[bytes, ...]) -> Iterator[bytes]:
    if not secrets:
        yield from source
        return
    values = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    if not values:
        yield from source
        return
    pattern = re.compile(re.escape(MARKER) + b"|" + b"|".join(map(re.escape, values)))
    overlap = max(len(MARKER), *map(len, values)) - 1
    pending = b""
    for block in source:
        pending += block
        boundary = max(0, len(pending) - overlap)
        position = 0
        for match in pattern.finditer(pending):
            if match.start() >= boundary:
                break
            yield pending[position : match.start()] + MARKER
            position = match.end()
        end = max(position, boundary)
        yield pending[position:end]
        pending = pending[end:]
    yield pattern.sub(MARKER, pending)


def redact_bytes(content: bytes, secrets: tuple[str, ...]) -> bytes:
    return b"".join(redact_chunks((content,), tuple(secret.encode() for secret in secrets)))
