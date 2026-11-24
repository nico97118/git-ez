# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Single-commit cherry-pick integration and manual continuation."""

import json
import sys

import pytest

from git_ez.audit import Audit
from git_ez.engine import Engine
from git_ez.errors import EzError
from tests.helpers import integrate

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("interactive", [False, True])
def test_cherry_pick_resolves_and_records_selected_commit(repo, mocker, interactive):
    if interactive:
        mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, "cherry-pick", interactive=interactive)
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["integrated"]
    assert repo.git.text("rev-parse", "HEAD^") == repo.source_oid
    assert repo.git.branch_oid("main") == repo.parent_oid
    assert repo.git.text("show", "-s", "--format=%s", "HEAD") == "parent change"
    assert repo.file.read_text() == "combined\n"
    assert not repo.git.operation_active()
    conflict = repo.resolver.calls[0]
    assert (conflict.operation, conflict.base, conflict.ours, conflict.theirs) == (
        "cherry-pick",
        "base\n",
        "source\n",
        "parent\n",
    )
    saved = json.loads((audit.directory / "run.json").read_text())
    assert saved["target_oid"] == repo.parent_oid
    record = saved["conflicts"][0]
    assert record["status"] == "applied"
    assert record["resolution"]["rationale"] == "Combine both intentions"
    assert (audit.directory / record["artifacts"]["conflict_diff"]).is_file()
    assert "cherry-pick" in (audit.directory / "report.md").read_text()


@pytest.mark.parametrize("root_commit", [False, True])
def test_cherry_pick_without_conflicts_does_not_call_model(repo, root_commit):
    repo.git.run("checkout", "--orphan", "unrelated")
    repo.git.run("rm", "-rf", ".")
    (repo.root / ("other.txt" if root_commit else "seed.txt")).write_text("independent\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "root")
    if not root_commit:
        (repo.root / "other.txt").write_text("changed\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "second")
    target = repo.git.commit_oid("HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, "cherry-pick", target=target)
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.resolver.calls == []
    assert repo.git.text("rev-parse", "HEAD^") == repo.source_oid
    assert (repo.root / "other.txt").read_text() == (
        "independent\n" if root_commit else "changed\n"
    )


@pytest.mark.parametrize("failure", ["confidence", "empty", "hook", "target", "control"])
def test_cherry_pick_failure_preserves_state_and_audit(repo, failure):
    if failure == "confidence":
        repo.resolver.confidence = 0.1
    elif failure == "empty":
        repo.resolver.content = "source\n"
    elif failure == "hook":
        hook = repo.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    elif failure in ("target", "control"):
        original = repo.resolver.run

        def change_control(*args):
            result = original(*args)
            if failure == "target":
                repo.git.run("update-ref", "refs/heads/main", repo.base_oid)
            else:
                repo.git.run("update-ref", "CHERRY_PICK_HEAD", repo.base_oid)
            return result

        repo.resolver.run = change_control
    audit = integrate(repo, "cherry-pick")
    assert audit.view["status"] == "failed", audit.view
    assert not audit.view["integrated"]
    assert repo.git.commit_oid("HEAD") == repo.source_oid
    assert repo.git.integration_active("cherry-pick")
    assert audit.published
    assert audit.view["conflicts"]


def test_cherry_pick_rejects_merge_commit_before_starting(repo):
    repo.git.run("merge", "--no-commit", "main", check=False)
    repo.file.write_text("merged\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "merge")
    target = repo.git.commit_oid("HEAD")
    repo.git.run("reset", "--hard", repo.source_oid)
    with pytest.raises(EzError, match="merge commits"):
        integrate(repo, "cherry-pick", target=target)
    assert not repo.git.operation_active()
    assert repo.resolver.calls == []


@pytest.mark.parametrize("validate", [False, True])
def test_cherry_pick_manual_continuation_uses_git_without_model(repo, validate):
    repo.git.run("cherry-pick", "main", check=False)
    repo.file.write_text("manual\n")
    repo.git.run("add", ".")
    result = Engine(repo.root, repo.config, repo.resolver).continue_operation(
        "cherry-pick", (sys.executable, "-c", "pass") if validate else ()
    )
    if validate:
        assert isinstance(result, Audit)
        assert result.view["status"] == "succeeded", result.view
        assert result.view["validation"]["status"] == "passed"
    else:
        assert result == 0
    assert repo.resolver.calls == []
    assert repo.file.read_text() == "manual\n"
    assert repo.git.text("rev-parse", "HEAD^") == repo.source_oid
    assert not repo.git.operation_active()


@pytest.mark.parametrize("target", ["main..feature", "main...feature"])
def test_cherry_pick_rejects_ranges_without_changing_repository(repo, target):
    with pytest.raises(EzError):
        integrate(repo, "cherry-pick", target=target)
    assert repo.git.commit_oid("HEAD") == repo.source_oid
    assert not repo.git.operation_active()
    assert repo.resolver.calls == []


def test_cherry_pick_requires_clean_checkout(repo):
    repo.file.write_text("local work\n")
    with pytest.raises(EzError, match="must be clean"):
        integrate(repo, "cherry-pick")
    assert repo.file.read_text() == "local work\n"
    assert not repo.git.operation_active()


def test_cherry_pick_sequence_continuation_is_rejected_without_changes(repo):
    repo.git.run("cherry-pick", repo.parent_oid, repo.base_oid, check=False)
    assert repo.git.sequencer_active()
    before = repo.git.run("ls-files", "--stage", "-z").stdout
    content = repo.file.read_bytes()
    with pytest.raises(EzError, match="sequences require native Git"):
        Engine(repo.root, repo.config, repo.resolver).continue_operation("cherry-pick")
    assert repo.git.run("ls-files", "--stage", "-z").stdout == before
    assert repo.file.read_bytes() == content
    assert repo.git.sequencer_active()
    assert repo.resolver.calls == []


def test_cherry_pick_unresolved_continuation_keeps_manual_conflict(repo):
    repo.git.run("cherry-pick", "main", check=False)
    result = Engine(repo.root, repo.config, repo.resolver).continue_operation("cherry-pick")
    assert isinstance(result, int) and result != 0
    assert repo.git.conflicts()
    assert repo.resolver.calls == []


@pytest.mark.parametrize("change", ["failure", "extra_commit"])
def test_cherry_pick_final_validation_rejects_failure_or_extra_commit(repo, change):
    code = (
        "raise SystemExit(3)"
        if change == "failure"
        else (
            "import subprocess; subprocess.run("
            "['git', 'commit', '--allow-empty', '-m', 'unexpected'], check=True)"
        )
    )
    audit = integrate(repo, "cherry-pick", validation=(sys.executable, "-c", code))
    assert audit.view["status"] == "failed", audit.view
    assert audit.view["integrated"]
    assert not repo.git.operation_active()
    assert audit.published
    assert audit.view["validation"]["status"] == ("failed" if change == "failure" else "passed")
