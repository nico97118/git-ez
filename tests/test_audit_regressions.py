# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Regression cases for the agreed audit scope and review findings."""

import sys

import pytest

from git_ez.audit import Audit
from git_ez.engine import Engine
from git_ez.errors import AuditError
from tests.test_engine import repo as repo

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("ending", ["error", "interrupted", "low_confidence"])
def test_failed_attempt_retains_supporting_changes(repo, monkeypatch, interactive, ending):
    monkeypatch.setattr("builtins.input", lambda prompt: "s")
    from git_ez.errors import EzError

    def fail(*args):
        repo.file.write_text("partial conflict resolution\n")
        (repo.root / "support.txt").write_text("SUPPORTING-CHANGE-REVIEW\n")
        if ending == "interrupted":
            raise KeyboardInterrupt
        if ending == "low_confidence":
            return {"rationale": "Needs manual review", "confidence": 0.1}
        raise EzError("resolver failed")

    repo.resolver.run = fail
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "merge", interactive=interactive
    )
    assert audit.view["status"] == ("interrupted" if ending == "interrupted" else "failed")
    files = list(audit.directory.rglob("*"))
    assert any(p.is_file() and b"SUPPORTING-CHANGE-REVIEW" in p.read_bytes() for p in files)


def test_resolution_diff_without_final_newline_is_readable(repo):
    repo.file.write_text("source")
    repo.git.run("commit", "--amend", "--no-edit", "-a")
    repo.resolver.content = "combined"
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "succeeded", audit.view
    record = audit.view["conflicts"][0]
    diff = (audit.directory / record["artifacts"]["diff"]).read_text()
    assert "\n+combined" in diff, repr(diff)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_missing_validation_stdout_is_diagnostic_not_run_failure(
    repo, monkeypatch, stream, exit_code
):
    original = Audit.write

    def fail(self, artifact, content):
        if artifact == f"validation.{stream}":
            raise AuditError("Cannot export validation stdout")
        return original(self, artifact, content)

    monkeypatch.setattr(Audit, "write", fail)
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "merge", validation=(sys.executable, "-c", f"raise SystemExit({exit_code})")
    )
    assert audit.view["integrated"]
    assert audit.view["validation"]["status"] == ("passed" if exit_code == 0 else "failed")
    assert audit.view["status"] == ("succeeded" if exit_code == 0 else "failed")
    assert audit.published
    assert any(error["phase"] == f"validation {stream}" for error in audit.audit_errors)
    if exit_code:
        assert audit.view["error"] == "Validation failed with code 7"


@pytest.mark.parametrize(
    "before,after", [("old", "new"), ("old\n", "new"), ("old", "new\n"), ("", "new"), ("old", "")]
)
def test_no_newline_diffs_can_be_applied_by_git(repo, before, after):
    from git_ez.conflicts import text_diff

    repo.file.write_text(before)
    patch = repo.root / "resolution.patch"
    patch.write_text(text_diff(before, after, "a/file.txt", "b/file.txt"))
    repo.git.run("apply", "--check", str(patch))
    repo.git.run("apply", str(patch))
    assert repo.file.read_text() == after
