# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Git caller settings are preserved while checkout redirections are excluded."""

import pytest

from git_ez.git import Git

pytestmark = pytest.mark.integration


def test_git_environment_keeps_configuration_and_identity_but_not_repository_routing(
    tmp_path, monkeypatch
):
    selected = tmp_path / "selected"
    selected.mkdir()
    git = Git(selected)
    git.run("init", "-b", "main")
    other = tmp_path / "other"
    other.mkdir()
    Git(other).run("init", "-b", "main")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Caller identity")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Author from environment")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
    assert git.text("rev-parse", "--show-toplevel") == str(selected)
    assert git.text("config", "user.name") == "Caller identity"
    assert git.text("var", "GIT_AUTHOR_IDENT").startswith(
        "Author from environment <author@example.invalid>"
    )
