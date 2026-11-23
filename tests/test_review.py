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


@pytest.mark.parametrize("artifact", ["worktree.diff", "files/captured.bin"])
def test_review_escapes_controls_and_filters_every_display_source(
    tmp_path, mocker, capsys, artifact
):
    secret = "synthetic-review-secret"
    (tmp_path / "files").mkdir()
    (tmp_path / "worktree.diff").write_text("normal diff\n")
    (tmp_path / "files/captured.bin").write_text("normal content\n")
    (tmp_path / artifact).write_text("first\n" + secret + "\x1b[2J\rforged\n")
    (tmp_path / "files.json").write_text(
        json.dumps(
            {
                "files": [{"path": "file\x1b[31m", "artifact": "files/captured.bin"}],
                "capture_errors": [{"path": secret}],
            }
        )
    )
    mocker.patch("builtins.input", return_value="s")
    assert review_proposal(tmp_path, Decision(secret + "\x1b[31m", 0.9), (secret,)) == (
        "stop",
        None,
    )
    output = capsys.readouterr().out
    assert secret not in output
    assert "<redacted>" in output
    assert "\x1b" not in output and "\r" not in output
    assert "first\n" in output
    assert "\\u001b" in output


@pytest.mark.parametrize("unsafe", ["symlink", "parent_symlink", "traversal"])
def test_review_refuses_unsafe_capture_reads(tmp_path, mocker, unsafe):
    root = tmp_path / "review"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("must not be displayed")
    (root / "worktree.diff").write_text("diff")
    if unsafe == "symlink":
        (root / "capture").symlink_to(outside / "private")
        artifact = "capture"
    elif unsafe == "parent_symlink":
        (root / "capture").symlink_to(outside, target_is_directory=True)
        artifact = "capture/private"
    else:
        artifact = "../outside/private"
    (root / "files.json").write_text(
        json.dumps({"files": [{"path": "file", "artifact": artifact}], "capture_errors": []})
    )
    prompt = mocker.patch("builtins.input", return_value="a")
    with pytest.raises((OSError, ValueError)):
        review_proposal(root, Decision("resolve", 0.9))
    prompt.assert_not_called()
