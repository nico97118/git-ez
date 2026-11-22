# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Conflict paths cannot redirect file access outside the checkout or into Git metadata."""

import pytest

from git_ez.errors import EzError
from git_ez.git import Git

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["/absolute/file", "../outside.txt", ".git/config"])
def test_unsafe_conflict_paths_are_rejected(tmp_path, name):
    with pytest.raises(EzError, match="Unsafe conflict path"):
        Git(tmp_path).safe_path(name)


def test_conflict_paths_cannot_follow_a_symlinked_parent(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence = outside / "file.txt"
    evidence.write_text("must remain untouched")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(EzError, match="Symlinks require manual resolution"):
        Git(root).safe_path("linked/file.txt")
    assert evidence.read_text() == "must remain untouched"


def test_non_utf8_git_conflict_paths_require_manual_resolution(tmp_path, mocker):
    from subprocess import CompletedProcess

    output = b"100644 " + b"a" * 40 + b" 2\tinvalid-\xff.txt\0"
    git = Git(tmp_path)
    mocker.patch.object(git, "run", return_value=CompletedProcess([], 0, stdout=output))
    with pytest.raises(EzError, match="Non-UTF-8 filenames require manual resolution"):
        git.conflicts()


def test_non_utf8_supporting_paths_are_reported_as_resolution_errors(tmp_path, mocker):
    from subprocess import CompletedProcess

    from git_ez.checks import WorktreeChecks
    from git_ez.config import Config

    git = Git(tmp_path)
    mocker.patch.object(
        git,
        "run",
        return_value=CompletedProcess(
            [],
            0,
            stdout=b"invalid-\xff.txt\0",
        ),
    )
    with pytest.raises(EzError, match="Non-UTF-8 changed filenames require manual resolution"):
        WorktreeChecks(Config(model="test")).verify(git, [], {})
