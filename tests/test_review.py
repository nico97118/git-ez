# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicit proposal input without Git or model runtimes."""

import json

import pytest

from git_ez.cli import parser
from git_ez.conflicts import Decision
from git_ez.review import review_proposal

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_interactive_parser(operation):
    assert parser().parse_args([operation, "main", "--interactive"]).interactive
    assert not parser().parse_args([operation, "main"]).interactive


def test_empty_invalid_and_blank_feedback_never_accept(tmp_path, mocker):
    (tmp_path / "worktree.diff").write_text("proposal diff")
    (tmp_path / "files.json").write_text(json.dumps({"files": [], "capture_errors": []}))
    mocker.patch("builtins.input", side_effect=["", "invalid", "r", "", "s"])
    assert review_proposal(tmp_path, Decision("checked syntax; tests not run", 0.9)) == (
        "stop",
        None,
    )
