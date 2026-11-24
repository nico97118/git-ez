# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Native merge fast-forward, message, and cleanup policies."""

import json

import pytest

from git_ez.errors import EzError
from git_ez.options import CommitOptions
from tests.helpers import integrate

pytestmark = pytest.mark.integration


def test_merge_fast_forwards_like_git(repo):
    repo.git.run("checkout", "-b", "behind", repo.base_oid)
    audit = integrate(repo, "merge")
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.branch_oid("behind") == repo.parent_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize(
    "policy,configuration,expected",
    [
        (None, None, "ff"),
        (None, "false", "merge"),
        (None, "only", "ff"),
        ("ff", "false", "ff"),
        ("no-ff", "only", "merge"),
        ("ff-only", "false", "ff"),
    ],
)
def test_merge_fast_forward_policy_overrides_configuration(repo, policy, configuration, expected):
    repo.git.run("checkout", "-b", "behind", repo.base_oid)
    if configuration is not None:
        repo.git.run("config", "merge.ff", configuration)
    audit = integrate(repo, "merge", merge_ff=policy)
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["merge_ff"] == policy
    if expected == "ff":
        assert repo.git.branch_oid("behind") == repo.parent_oid
    else:
        assert repo.git.text("rev-parse", "HEAD^1") == repo.base_oid
        assert repo.git.text("rev-parse", "HEAD^2") == repo.parent_oid
    assert not repo.git.operation_active()
    assert repo.git.run("status", "--porcelain").stdout == b""
    assert repo.resolver.calls == []
    saved = json.loads((audit.directory / "run.json").read_text())
    assert saved["merge_ff"] == policy


@pytest.mark.parametrize("policy", [None, "ff", "no-ff", "ff-only"])
def test_merge_policy_leaves_up_to_date_branch_unchanged(repo, policy):
    audit = integrate(repo, "merge", target="HEAD", merge_ff=policy)
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize("policy", ["ff", "no-ff"])
def test_merge_policy_survives_conflict_continuation(repo, policy):
    repo.git.run("config", "merge.ff", "only")
    audit = integrate(repo, "merge", merge_ff=policy)
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD^1") == repo.source_oid
    assert repo.git.text("rev-parse", "HEAD^2") == repo.parent_oid
    assert repo.file.read_text() == "combined\n"
    assert len(repo.resolver.calls) == 1
    assert not repo.git.operation_active()


def test_ff_only_refusal_preserves_state_and_audits_failure(repo):
    repo.git.run("config", "merge.ff", "false")
    refs = repo.git.text("show-ref")
    audit = integrate(repo, "merge", merge_ff="ff-only")
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"] is False
    assert repo.git.text("show-ref") == refs
    assert repo.file.read_text() == "source\n"
    assert repo.git.run("status", "--porcelain").stdout == b""
    assert not repo.git.operation_active()
    assert repo.resolver.calls == []


@pytest.mark.parametrize("operation,policy", [("rebase", "ff"), ("merge", "invalid")])
def test_invalid_merge_policy_is_rejected_before_creating_audit(repo, operation, policy):
    with pytest.raises(EzError, match="[Ff]ast-forward"):
        integrate(repo, operation, merge_ff=policy)
    assert not (repo.root / ".git/ez").exists()
    assert repo.git.branch_oid("feature") == repo.source_oid


@pytest.mark.parametrize("conflict", [True, False])
def test_merge_message_and_signoff_survive_continuation(repo, conflict):
    if not conflict:
        repo.git.run("checkout", "-b", "clean", repo.base_oid)
        (repo.root / "independent.txt").write_text("new\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "independent")
    message = "Merge the feature\n\nKeep both intentions."
    audit = integrate(repo, "merge", commit_options=CommitOptions(message=message, signoff=True))
    assert audit.view["status"] == "succeeded", audit.view
    actual = repo.git.text("show", "-s", "--format=%B", "HEAD")
    assert actual == message + "\n\nSigned-off-by: Test <test@example.invalid>"
    assert audit.view["commit_options"]["message"] == message
    assert len(repo.resolver.calls) == int(conflict)


def test_merge_message_does_not_force_a_commit_on_fast_forward(repo):
    repo.git.run("checkout", "-b", "behind", repo.base_oid)
    audit = integrate(repo, "merge", commit_options=CommitOptions(message="unused", signoff=True))
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.branch_oid("behind") == repo.parent_oid
    assert "Signed-off-by:" not in repo.git.text("show", "-s", "--format=%B", "HEAD")


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("cleanup", ["strip", "whitespace", "verbatim", "scissors", "default"])
@pytest.mark.parametrize("log", [0, 1, 20])
def test_merge_log_cleanup_matches_native_messages(repo, conflict, cleanup, log):
    if not conflict:
        repo.git.run("checkout", "-b", "clean-message", repo.base_oid)
        (repo.root / "independent.txt").write_text("new\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "independent")
    branch = repo.git.text("branch", "--show-current")
    before = repo.git.text("rev-parse", "HEAD")
    message = "  Merge title  \n\n# custom comment\n\nDetails   \n"
    repo.git.run("checkout", "-b", "native-message", before)
    result = repo.git.run(
        "merge",
        "--no-edit",
        f"--log={log}",
        f"--cleanup={cleanup}",
        "--signoff",
        "-m",
        message,
        repo.parent_oid,
        check=False,
    )
    if conflict:
        assert result.returncode != 0
        repo.file.write_text("combined\n")
        repo.git.run("add", "file.txt")
        repo.git.run("commit", "--no-edit", "--signoff", f"--cleanup={cleanup}")
    else:
        assert result.returncode == 0
    expected = repo.git.run("show", "-s", "--format=%B", "HEAD").stdout
    repo.git.run("checkout", branch)
    audit = integrate(
        repo,
        "merge",
        commit_options=CommitOptions(
            message=message,
            log=log,
            cleanup=cleanup,
            signoff=True,
        ),
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.run("show", "-s", "--format=%B", "HEAD").stdout == expected
    assert audit.view["commit_options"]["log"] == log
    assert audit.view["commit_options"]["cleanup"] == cleanup
    assert len(repo.resolver.calls) == int(conflict)


@pytest.mark.parametrize("options", [CommitOptions(log=1), CommitOptions(cleanup="strip")])
def test_merge_message_options_rejected_for_rebase(repo, options):
    with pytest.raises(EzError, match="only supported for merge"):
        integrate(repo, commit_options=options)
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert not (repo.root / ".git/ez").exists()


def test_merge_log_cleanup_do_not_force_commit_on_fast_forward(repo):
    repo.git.run("checkout", "-b", "behind-message", repo.base_oid)
    audit = integrate(repo, "merge", commit_options=CommitOptions(log=20, cleanup="strip"))
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD") == repo.parent_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize("requested", [None, 0, 1])
def test_merge_log_configuration_survives_conflict_with_custom_message(repo, requested):
    repo.git.run("config", "merge.log", "true")
    audit = integrate(
        repo,
        "merge",
        commit_options=CommitOptions(
            message="Configured summary",
            log=requested,
            signoff=True,
        ),
    )
    assert audit.view["status"] == "succeeded", audit.view
    message = repo.git.text("show", "-s", "--format=%B", "HEAD")
    assert message.startswith("Configured summary\n")
    assert ("  parent change" in message) is (requested != 0)
    assert message.count("Signed-off-by:") == 1
    assert repo.git.text("config", "merge.log") == "true"


def test_merge_cleanup_failure_preserves_active_merge(repo):
    hooks = repo.root / ".git/hooks"
    hook = hooks / "commit-msg"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    audit = integrate(repo, "merge", commit_options=CommitOptions(log=1, cleanup="strip"))
    assert audit.view["status"] == "failed", audit.view
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert "parent change" in (repo.root / ".git/MERGE_MSG").read_text()


@pytest.mark.parametrize("limit", [0, 1, 20])
def test_merge_log_limits_multiple_incoming_subjects(repo, limit):
    repo.git.run("checkout", "main")
    repo.git.run("commit", "--allow-empty", "-m", "second incoming")
    repo.git.run("commit", "--allow-empty", "-m", "third incoming")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, "merge", commit_options=CommitOptions(log=limit, cleanup="strip"))
    assert audit.view["status"] == "succeeded", audit.view
    message = repo.git.text("show", "-s", "--format=%B", "HEAD")
    assert ("  third incoming" in message) is (limit > 0)
    assert ("  second incoming" in message) is (limit > 1)
    assert ("  parent change" in message) is (limit > 1)
