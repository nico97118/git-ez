# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Native autosquash sequencing, capture, and result verification."""

import json
import sys

import pytest

from git_ez.git import Git
from git_ez.options import CommitOptions, RebaseOptions
from tests.fakes import FakeResolver
from tests.helpers import integrate

pytestmark = pytest.mark.integration


def autosquash_series(repo, marker="fixup!"):
    repo.git.run("checkout", "-b", "topic", repo.base_oid)
    (repo.root / "a.txt").write_text("first\n")
    repo.git.run("add", "a.txt")
    repo.git.run("commit", "-m", "feature A")
    original = repo.git.text("rev-parse", "HEAD")
    (repo.root / "b.txt").write_text("unrelated\n")
    repo.git.run("add", "b.txt")
    repo.git.run("commit", "-m", "unrelated")
    (repo.root / "a.txt").write_text("fixed\n")
    body = "Replacement title\n\nreplacement body" if marker == "amend!" else "fixup body"
    repo.git.run("commit", "-am", f"{marker} feature A\n\n{body}")
    return original


@pytest.mark.parametrize("marker", ["fixup!", "squash!", "amend!"])
def test_autosquash_groups_native_markers_and_records_messages(repo, marker):
    original = autosquash_series(repo, marker)
    author = repo.git.text("show", "-s", "--format=%an <%ae>%n%aI", original)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "succeeded", audit.view
    commits = repo.git.text("rev-list", "--reverse", f"{repo.parent_oid}..HEAD").splitlines()
    assert len(commits) == 2
    message = repo.git.text("show", "-s", "--format=%B", commits[0])
    assert repo.git.text("show", "-s", "--format=%s", commits[1]) == "unrelated"
    assert repo.git.text("show", "-s", "--format=%an <%ae>%n%aI", commits[0]) == author
    if marker == "amend!":
        assert message == "Replacement title\n\nreplacement body"
    else:
        assert message.startswith("feature A")
        assert ("fixup body" in message) is (marker == "squash!")
    assert (repo.root / "a.txt").read_text() == "fixed\n"
    assert (repo.root / "b.txt").read_text() == "unrelated\n"
    assert repo.resolver.calls == []
    assert audit.view["rebase_sequence"]["status"] == "captured"
    plan = (audit.directory / "rebase-todo.txt").read_text()
    assert plan.index("feature A") < plan.index(f"{marker} feature A") < plan.index("unrelated")
    assert not repo.git.operation_active()


@pytest.mark.parametrize("autosquash", [None, False])
def test_autosquash_requires_explicit_opt_in_even_with_native_configuration(repo, autosquash):
    autosquash_series(repo)
    repo.git.run("config", "rebase.autoSquash", "true")
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=autosquash))
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-list", "--count", f"{repo.parent_oid}..HEAD") == "3"
    assert "rebase_sequence" not in audit.view
    assert repo.resolver.calls == []


def test_autosquash_uses_quoted_editor_paths_and_a_fixed_instruction_format(repo, tmp_path):
    destination = tmp_path / "working ' ;$ spaces"
    repo.root.rename(destination)
    repo.root = destination
    repo.git = Git(destination)
    repo.file = destination / "file.txt"
    autosquash_series(repo)
    configured = "%s%nexec touch unexpected"
    repo.git.run("config", "rebase.instructionFormat", configured)
    repo.git.run("config", "rebase.autoSquash", "false")
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "succeeded", audit.view
    assert not (repo.root / "unexpected").exists()
    assert "\nexec " not in (audit.directory / "rebase-todo.txt").read_text()
    assert repo.git.text("config", "--get", "rebase.instructionFormat") == configured
    assert repo.git.text("rev-list", "--count", f"{repo.parent_oid}..HEAD") == "2"


@pytest.mark.parametrize("marker", ["fixup!", "squash!"])
@pytest.mark.parametrize("policy", ["keep", "stop"])
def test_empty_fixup_resolution_preserves_the_group(repo, mocker, marker, policy):
    autosquash_series(repo, marker)
    repo.git.run("checkout", "main")
    (repo.root / "a.txt").write_text("upstream\n")
    repo.git.run("add", "a.txt")
    repo.git.run("commit", "-m", "upstream A")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "topic")
    repo.git.run("config", "rebase.abbreviateCommands", "true")
    resolve = repo.resolver.run

    def resolve_a(git, audit, round_number, paths, check):
        repo.resolver.content = "combined\n"
        return resolve(git, audit, round_number, paths, check)

    mocker.patch.object(repo.resolver, "run", side_effect=resolve_a)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True, empty=policy))
    assert audit.view["status"] == "succeeded", audit.view
    assert len(repo.resolver.calls) == 2
    assert repo.git.text("rev-list", "--count", f"{target}..HEAD") == "2"
    assert (repo.root / "a.txt").read_text() == "combined\n"
    assert not audit.view.get("empty_commits")
    assert not repo.git.operation_active()


def test_autosquash_resulting_commits_are_signed(repo, ssh_signing):
    autosquash_series(repo)
    audit = integrate(
        repo,
        rebase_options=RebaseOptions(autosquash=True),
        commit_options=CommitOptions(gpg_sign=str(ssh_signing), signoff=True),
    )
    assert audit.view["status"] == "succeeded", audit.view
    for commit in repo.git.text("rev-list", f"{repo.parent_oid}..HEAD").splitlines():
        repo.git.run("verify-commit", commit)
        assert "Signed-off-by:" in repo.git.text("show", "-s", "--format=%B", commit)


def test_autosquash_capture_limit_failure_is_audited_before_replay(repo):
    from dataclasses import replace

    autosquash_series(repo)
    before = repo.git.text("rev-parse", "HEAD")
    repo.config = replace(repo.config, max_file_bytes=16)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed"
    assert not audit.view["integrated"]
    assert audit.view["rebase_sequence"]["status"] == "unavailable"
    assert repo.git.branch_oid("topic") == before
    assert repo.resolver.calls == []


@pytest.mark.parametrize("conflict", [False, True])
def test_autosquash_missing_sequence_cannot_report_overall_success(repo, conflict):
    autosquash_series(repo)
    if conflict:
        repo.git.run("checkout", "main")
        (repo.root / "a.txt").write_text("upstream\n")
        repo.git.run("add", "a.txt")
        repo.git.run("commit", "-m", "upstream A")
        repo.git.run("checkout", "topic")
    hook = repo.root / ".git/hooks/post-rewrite"
    hook.write_text(
        '#!/bin/sh\nfor plan in .git/ez/runs/*/rebase-todo.txt; do rm -f -- "$plan"; done\n'
    )
    hook.chmod(0o755)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed", audit.view
    assert audit.view["integrated"]
    assert "sequence capture is unavailable" in audit.view["error"]
    assert audit.view["rebase_sequence"]["status"] == "unavailable"
    assert not repo.git.operation_active()


@pytest.mark.parametrize("policy", [None, "drop", "keep", "stop"])
def test_autosquash_keeps_the_existing_default_empty_policy(repo, policy):
    repo.git.run("checkout", "main")
    repo.file.write_text("source\n")
    repo.git.run("commit", "-am", "equivalent upstream change")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(
        repo,
        rebase_options=RebaseOptions(autosquash=True, empty=policy, reapply_cherry_picks=True),
    )
    assert audit.view["rebase_sequence"]["status"] == "captured"
    assert repo.resolver.calls == []
    if policy == "stop":
        assert audit.view["status"] == "failed"
        assert repo.git.rebase_active()
    else:
        assert audit.view["status"] == "succeeded", audit.view
        assert repo.git.text("rev-list", "--count", f"{target}..HEAD") == (
            "1" if policy == "keep" else "0"
        )


def test_autosquash_handles_an_empty_sequence(repo):
    before = repo.git.text("rev-parse", "HEAD")
    audit = integrate(repo, target="HEAD", rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["result_oid"] == before
    assert audit.view["rebase_sequence"]["status"] == "captured"
    assert repo.resolver.calls == []


def test_autosquash_leaves_unmatched_markers_as_picks(repo):
    autosquash_series(repo)
    repo.git.run("commit", "--amend", "-m", "fixup! missing target")
    audit = integrate(
        repo, onto="main", target=repo.base_oid, rebase_options=RebaseOptions(autosquash=True)
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("rev-list", "--count", f"{repo.parent_oid}..HEAD") == "3"
    assert repo.git.text("show", "-s", "--format=%s", "HEAD") == "fixup! missing target"
    assert repo.resolver.calls == []


def test_autosquash_conflict_failure_keeps_the_initial_sequence(repo):
    autosquash_series(repo)
    before = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "main")
    (repo.root / "a.txt").write_text("upstream\n")
    repo.git.run("add", "a.txt")
    repo.git.run("commit", "-m", "upstream A")
    repo.git.run("checkout", "topic")
    repo.resolver = FakeResolver(error="offline")
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed"
    assert repo.git.rebase_active()
    assert repo.git.branch_oid("topic") == before
    assert audit.view["rebase_sequence"]["status"] == "captured"
    assert "fixup " in (audit.directory / "rebase-todo.txt").read_text()
    assert audit.view["conflicts"]
    assert "result_commits" not in audit.view


def test_autosquash_does_not_bypass_pre_rebase_hook(repo):
    autosquash_series(repo)
    hook = repo.root / ".git/hooks/pre-rebase"
    hook.write_text("#!/bin/sh\necho autosquash-rejected >&2\nexit 1\n")
    hook.chmod(0o755)
    before = repo.git.text("rev-parse", "HEAD")
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed"
    assert repo.git.branch_oid("topic") == before
    assert audit.view["rebase_sequence"]["status"] == "unavailable"
    assert repo.resolver.calls == []


def test_autosquash_detects_sequence_changes_after_a_conflict(repo, mocker):
    autosquash_series(repo)
    repo.git.run("checkout", "main")
    (repo.root / "a.txt").write_text("upstream\n")
    repo.git.run("add", "a.txt")
    repo.git.run("commit", "-m", "upstream A")
    repo.git.run("checkout", "topic")
    resolve = repo.resolver.run

    def edit_evidence(git, audit, round_number, paths, check):
        result = resolve(git, audit, round_number, paths, check)
        (audit.directory / "rebase-todo.txt").write_text("noop\n")
        return result

    mocker.patch.object(repo.resolver, "run", side_effect=edit_evidence)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert audit.view["rebase_sequence"]["status"] == "unavailable"
    assert "sequence changed" in audit.view["rebase_sequence"]["error"]


def test_autosquash_sequence_is_checked_after_final_validation(repo):
    autosquash_series(repo)
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "[p.unlink() for p in Path('.git/ez/runs').glob('*/rebase-todo.txt')]",
    )
    audit = integrate(repo, validation=command, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert audit.view["validation"]["returncode"] == 0
    assert audit.view["rebase_sequence"]["status"] == "unavailable"
    assert "sequence capture is unavailable" in audit.view["error"]


@pytest.mark.parametrize("artifact", ["rebase-todo.txt", "rebase-todo.txt.sha256"])
def test_autosquash_rejects_evidence_replaced_by_post_rewrite_hook(repo, artifact):
    autosquash_series(repo)
    hook = repo.root / ".git/hooks/post-rewrite"
    hook.write_text(
        f"#!/bin/sh\nfor evidence in .git/ez/runs/*/{artifact}; "
        'do printf "noop\\n" > "$evidence"; done\n'
    )
    hook.chmod(0o755)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed", audit.view
    assert audit.view["integrated"]
    assert not repo.git.operation_active()
    assert audit.view["rebase_sequence"]["status"] == "unavailable"
    assert "sequence changed" in audit.view["rebase_sequence"]["error"]
    saved = json.loads((audit.directory / "run.json").read_text())
    assert saved["status"] == "failed"
    assert "sequence capture is unavailable" in (audit.directory / "report.md").read_text()


def test_autosquash_fingerprint_is_checked_after_final_validation(repo):
    autosquash_series(repo)
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "[p.write_text('0' * 64) for p in Path('.git/ez/runs').glob('*/rebase-todo.txt.sha256')]",
    )
    audit = integrate(repo, validation=command, rebase_options=RebaseOptions(autosquash=True))
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert audit.view["validation"]["returncode"] == 0
    assert "fingerprint changed" in audit.view["rebase_sequence"]["error"]
