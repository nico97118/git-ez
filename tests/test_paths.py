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
