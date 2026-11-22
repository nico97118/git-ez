# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Bounded capture of the native Git sequence, without starting Git."""

import hashlib

import pytest

from git_ez.sequence_editor import capture_sequence

pytestmark = pytest.mark.unit


def test_sequence_capture_preserves_instructions(tmp_path):
    source = tmp_path / "todo"
    destination = tmp_path / "captured"
    contents = b"pick abc change\nfixup def fixup! change\n"
    source.write_bytes(contents)
    capture_sequence(source, destination, len(contents))
    assert source.read_bytes() == contents
    assert destination.read_bytes() == contents
    assert (tmp_path / "captured.sha256").read_text() == hashlib.sha256(contents).hexdigest()


@pytest.mark.parametrize("kind", ["symlink", "directory", "oversized"])
def test_sequence_capture_rejects_incomplete_or_nonregular_evidence(tmp_path, kind):
    source = tmp_path / "todo"
    destination = tmp_path / "captured"
    if kind == "symlink":
        target = tmp_path / "real-todo"
        target.write_text("pick abc change\n")
        source.symlink_to(target)
    elif kind == "directory":
        source.mkdir()
    else:
        source.write_bytes(b"x" * 33)
    with pytest.raises((OSError, ValueError)):
        capture_sequence(source, destination, 32)
    assert not destination.exists()


@pytest.mark.parametrize("artifact", ["captured", "captured.sha256"])
def test_sequence_capture_does_not_follow_destination_symlink(tmp_path, artifact):
    source = tmp_path / "todo"
    source.write_bytes(b"noop\n")
    target = tmp_path / "untouched"
    target.write_bytes(b"original\n")
    destination = tmp_path / "captured"
    (tmp_path / artifact).symlink_to(target)
    capture_sequence(source, destination, 32)
    assert destination.read_bytes() == b"noop\n"
    assert not destination.is_symlink()
    assert target.read_bytes() == b"original\n"
    assert not (tmp_path / artifact).is_symlink()


def test_sequence_capture_requires_a_writable_fingerprint(tmp_path):
    source = tmp_path / "todo"
    source.write_bytes(b"noop\n")
    (tmp_path / "captured.sha256.tmp").write_bytes(b"existing\n")
    with pytest.raises(FileExistsError):
        capture_sequence(source, tmp_path / "captured", 32)
