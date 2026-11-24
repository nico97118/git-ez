# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Native signing, signature verification, and commit hooks."""

import pytest

from git_ez.errors import EzError
from git_ez.options import CommitOptions, RebaseOptions
from tests.fakes import FakeResolver
from tests.helpers import integrate

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "operation,hook", [("merge", "pre-merge-commit"), ("rebase", "pre-rebase")]
)
def test_user_hooks_are_honored(repo, operation, hook):
    hooks = repo.root / ".git" / "custom-hooks"
    hooks.mkdir()
    executable = hooks / hook
    executable.write_text("#!/bin/sh\necho 'blocked by project hook' >&2\nexit 1\n")
    executable.chmod(0o755)
    repo.git.run("config", "core.hooksPath", str(hooks))
    if operation == "merge":
        # A non-conflicting merge reaches pre-merge-commit rather than stopping on conflicts.
        repo.git.run("checkout", "-b", "clean", repo.base_oid)
        (repo.root / "independent.txt").write_text("independent\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "independent change")
    audit = integrate(repo, operation)
    assert audit.view["status"] == "failed", audit.view


def test_commit_signing_configuration_is_not_disabled(repo):
    repo.git.run("config", "commit.gpgsign", "true")
    repo.git.run("config", "gpg.format", "openpgp")
    repo.git.run("config", "gpg.program", "/nonexistent/ez-test-signing-program")
    audit = integrate(repo, "merge")
    assert audit.view["status"] == "failed", audit.view


@pytest.mark.parametrize("operation", ["rebase", "merge", "cherry-pick"])
@pytest.mark.parametrize("explicit_key", [True, False])
def test_signing_options_create_verifiable_commits_after_conflict(
    repo, ssh_signing, operation, explicit_key
):
    if explicit_key:
        repo.git.run("config", "user.signingkey", "/nonexistent/default-signing-key")
    key = str(ssh_signing) if explicit_key else ""
    audit = integrate(repo, operation, commit_options=CommitOptions(gpg_sign=key, signoff=True))
    assert audit.view["status"] == "succeeded", audit.view
    repo.git.run("verify-commit", "HEAD")
    assert "Signed-off-by: Test <test@example.invalid>" in repo.git.text(
        "show", "-s", "--format=%B", "HEAD"
    )
    assert len(repo.resolver.calls) == 1


@pytest.mark.parametrize("operation", ["rebase", "merge", "cherry-pick"])
def test_explicit_no_sign_overrides_signing_configuration(repo, operation):
    repo.git.run("config", "commit.gpgsign", "true")
    repo.git.run("config", "gpg.program", "/nonexistent/signing-program")
    audit = integrate(repo, operation, commit_options=CommitOptions(gpg_sign=False, signoff=False))
    assert audit.view["status"] == "succeeded", audit.view
    assert "gpgsig " not in repo.git.text("cat-file", "commit", "HEAD")
    assert "Signed-off-by:" not in repo.git.text("show", "-s", "--format=%B", "HEAD")


def test_retained_empty_commit_honors_signing_and_signoff(repo, ssh_signing):
    repo.resolver = FakeResolver(content="parent\n")
    audit = integrate(
        repo,
        rebase_options=RebaseOptions(empty="keep"),
        commit_options=CommitOptions(gpg_sign=str(ssh_signing), signoff=True),
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.run("diff", "HEAD^", "HEAD").stdout == b""
    repo.git.run("verify-commit", "HEAD")
    assert "Signed-off-by:" in repo.git.text("show", "-s", "--format=%B", "HEAD")


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_explicit_signing_failure_is_not_bypassed(repo, operation):
    repo.git.run("config", "gpg.program", "/nonexistent/explicit-signing-program")
    audit = integrate(repo, operation, commit_options=CommitOptions(gpg_sign=""))
    assert audit.view["status"] == "failed"
    assert not audit.view["integrated"]
    assert repo.git.operation_active()


def test_merge_verifies_signed_target(repo, ssh_signing):
    repo.git.run("checkout", "main")
    repo.git.run("commit", "--allow-empty", "-S", "-m", "signed target")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, "merge", commit_options=CommitOptions(verify_signatures=True))
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-parse", "HEAD^2") == target


def test_merge_signature_verification_refuses_unsigned_target_without_model(repo):
    audit = integrate(repo, "merge", commit_options=CommitOptions(verify_signatures=True))
    assert audit.view["status"] == "failed"
    assert not audit.view["integrated"]
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.resolver.calls == []
    assert not repo.git.operation_active()


def test_explicit_no_verify_signatures_overrides_configuration(repo):
    repo.git.run("config", "merge.verifySignatures", "true")
    audit = integrate(repo, "merge", commit_options=CommitOptions(verify_signatures=False))
    assert audit.view["status"] == "succeeded", audit.view


@pytest.mark.parametrize(
    "options", [CommitOptions(message="invalid"), CommitOptions(verify_signatures=True)]
)
def test_rebase_rejects_merge_only_commit_options(repo, options):
    with pytest.raises(EzError, match="only supported for merge"):
        integrate(repo, commit_options=options)
    assert not (repo.root / ".git/ez").exists()


@pytest.mark.parametrize("operation,hook_name", [("rebase", "pre-rebase"), ("merge", "commit-msg")])
def test_commit_options_do_not_retry_rejected_commit_hooks(repo, operation, hook_name):
    hook = repo.root / f".git/hooks/{hook_name}"
    count = repo.root / ".git/commit-msg-count"
    hook.write_text("#!/bin/sh\necho rejected >> .git/commit-msg-count\nexit 1\n")
    hook.chmod(0o755)
    options = CommitOptions(message="custom" if operation == "merge" else None, signoff=True)
    audit = integrate(repo, operation, commit_options=options)
    assert audit.view["status"] == "failed"
    assert not audit.view["integrated"]
    assert count.read_text().splitlines() == ["rejected"]
    assert repo.git.operation_active() is (operation == "merge")
