# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local integrations on real repositories, without a model or network."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from git_ez.config import Config
from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.git import Git
from git_ez.options import CommitOptions, RebaseOptions
from tests.fakes import FakeResolver

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "working"
    root.mkdir()
    git = Git(root)
    git.run("init", "-b", "main")
    git.run("config", "user.name", "Test")
    git.run("config", "user.email", "test@example.invalid")
    file = root / "file.txt"
    file.write_text("base\n")
    git.run("add", ".")
    git.run("commit", "-m", "base")
    base_oid = git.branch_oid("main")
    git.run("checkout", "-b", "feature")
    file.write_text("source\n")
    git.run("commit", "-am", "source change")
    source_oid = git.branch_oid("feature")
    git.run("checkout", "main")
    file.write_text("parent\n")
    git.run("commit", "-am", "parent change")
    parent_oid = git.branch_oid("main")
    git.run("checkout", "feature")
    return SimpleNamespace(
        root=root,
        git=git,
        file=file,
        base_oid=base_oid,
        source_oid=source_oid,
        parent_oid=parent_oid,
        resolver=FakeResolver(),
        config=Config(model="test"),
    )


def integrate(repo, operation="rebase", target="main", **kwargs):
    return Engine(repo.root, repo.config, repo.resolver).run(target, operation, **kwargs)


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_integration_resolves_locally_and_traces_without_remote_or_extra_refs(
    repo, operation, mocker
):
    refs = repo.git.text("for-each-ref", "--format=%(refname)", "refs/heads/")
    original_run = Git.run
    calls = []

    def local_only(git, *args, **kwargs):
        calls.append(args)
        assert not set(args) & {"push", "fetch", "pull", "ls-remote"}
        return original_run(git, *args, **kwargs)

    mocker.patch.object(Git, "run", local_only)
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["integrated"]
    result = audit.manifest["result_oid"]
    assert repo.git.branch_oid("feature") == result
    assert repo.git.branch_oid("main") == repo.parent_oid
    assert repo.git.text("branch", "--show-current") == "feature"
    assert repo.file.read_text() == "combined\n"
    if operation == "merge":
        assert repo.git.text("rev-parse", "HEAD^1") == repo.source_oid
        assert repo.git.text("rev-parse", "HEAD^2") == repo.parent_oid
        assert "range_diff" not in audit.manifest
        assert not (audit.directory / "range-diff.txt").exists()
    else:
        assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
        comparison = audit.manifest["range_diff"]
        assert comparison["before"] == f"{repo.parent_oid}..{repo.source_oid}"
        assert comparison["after"] == f"{repo.parent_oid}..{result}"
        assert (audit.directory / comparison["artifact"]).read_bytes() == repo.git.run(
            *comparison["command"][1:]
        ).stdout
    assert repo.git.run("status", "--porcelain").stdout == b""
    assert repo.git.text("for-each-ref", "--format=%(refname)", "refs/heads/") == refs
    assert repo.git.run("worktree", "list", "--porcelain").stdout.count(b"worktree ") == 1
    assert audit.manifest["worktree"] == str(repo.root)
    assert "result_branch" not in audit.manifest
    assert "push" not in audit.manifest
    conflict = repo.resolver.calls[0]
    expected = ("parent\n", "source\n") if operation == "rebase" else ("source\n", "parent\n")
    assert (conflict.ours, conflict.theirs) == expected
    assert conflict.operation == operation
    record = audit.manifest["conflicts"][0]
    for artifact in record["artifacts"].values():
        assert (audit.directory / artifact).is_file()
    assert record["resolution"]["rationale"] == "Combine both intentions"
    report = (audit.directory / "report.md").read_text()
    assert "Combine both intentions" in report
    assert operation in report
    assert "Revue facultative" not in report
    assert (audit.directory / "result.diff").read_bytes() == repo.git.run(
        "diff", "--no-ext-diff", "--no-textconv", "--binary", repo.source_oid, result
    ).stdout
    assert f"git {operation}" in (audit.directory / "integration.log").read_text()
    saved = json.loads((audit.directory / "run.json").read_text())
    assert saved["before_oid"] == repo.source_oid
    assert saved["target_oid"] == repo.parent_oid
    assert calls


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_existing_remote_is_never_updated(repo, tmp_path, operation):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    remote_git = Git(remote)
    remote_git.run("init", "--bare")
    repo.git.run("remote", "add", "origin", str(remote))
    repo.git.run("push", "-u", "origin", "feature")
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert remote_git.branch_oid("feature") == repo.source_oid
    assert repo.git.branch_oid("feature") != repo.source_oid


def test_multiple_replayed_commits(repo):
    repo.file.write_text("source second\n")
    repo.git.run("commit", "-am", "second change")
    audit = integrate(repo)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert len(repo.resolver.calls) == 2


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_dirty_repository_is_refused_without_changes(repo, staged, operation):
    repo.file.write_text("user work\n")
    if staged:
        repo.git.run("add", "file.txt")
    with pytest.raises(EzError, match="clean"):
        integrate(repo, operation)
    assert repo.file.read_text() == "user work\n"
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert not (repo.root / ".git/ez").exists()


def test_detached_head_is_refused(repo):
    repo.git.run("checkout", "--detach")
    with pytest.raises(EzError, match="checked out"):
        integrate(repo)


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_active_operation_is_refused(repo, operation):
    repo.git.run(operation, "main", check=False)
    with pytest.raises(EzError, match="in progress|checked out"):
        integrate(repo)
    assert repo.git.operation_active()


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_failed_validation_keeps_local_result_and_traces(repo, operation):
    audit = integrate(repo, operation, validation=(sys.executable, "-c", "raise SystemExit(7)"))
    assert audit.manifest["status"] == "failed", audit.manifest
    assert audit.manifest["integrated"]
    assert audit.manifest["validation"]["returncode"] == 7
    assert repo.git.branch_oid("feature") == audit.manifest["result_oid"]
    assert (audit.directory / "result.diff").is_file()


def test_successful_validation(repo):
    audit = integrate(repo, validation=(sys.executable, "-c", "print('ok')"))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert (audit.directory / "validation.stdout").read_text() == "ok\n"


@pytest.mark.parametrize(
    "script",
    [
        "from pathlib import Path; Path('file.txt').write_text('changed')",
        "import subprocess; subprocess.run(['git','checkout','main'],check=True)",
        "import subprocess; from pathlib import Path; Path('file.txt').write_text('changed'); "
        "subprocess.run(['git','-c','commit.gpgsign=false','commit','-am','changed'],check=True)",
    ],
)
def test_validation_side_effects_are_failures(repo, script):
    audit = integrate(repo, validation=(sys.executable, "-c", script))
    assert audit.manifest["status"] == "failed", audit.manifest


def test_target_changed_during_validation_is_detected(repo):
    script = (
        "import subprocess; "
        f"subprocess.run(['git','update-ref','refs/heads/main','{repo.source_oid}'],check=True)"
    )
    audit = integrate(repo, validation=(sys.executable, "-c", script))
    assert audit.manifest["status"] == "failed", audit.manifest
    assert "Target reference changed" in audit.manifest["error"]


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_low_confidence_keeps_active_operation_and_saved_proposal(repo, operation):
    repo.resolver.confidence = 0.2
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "failed", audit.manifest
    assert not audit.manifest["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.file.read_text() == "combined\n"
    record = audit.manifest["conflicts"][0]
    assert record["status"] == "low_confidence"
    assert (audit.directory / record["artifacts"]["proposed"]).read_text() == "combined\n"
    assert "Combine both intentions" in (audit.directory / "report.md").read_text()


def test_invalid_edits_keep_proposal(repo):
    repo.resolver.content = "<<<<<<< bad\n"
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert "proposed" in audit.manifest["conflicts"][0]["artifacts"]


def test_provider_error_keeps_input(repo):
    repo.resolver = FakeResolver(error="offline")
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert "request" in audit.manifest["conflicts"][0]["artifacts"]


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_no_conflict_does_not_call_provider(repo, operation):
    repo.git.run("checkout", "-b", "clean", repo.base_oid)
    (repo.root / "new.txt").write_text("new\n")
    repo.git.run("add", "new.txt")
    repo.git.run("commit", "-m", "independent change")
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.resolver.calls == []
    if operation == "rebase":
        assert " = " in (audit.directory / "range-diff.txt").read_text()


def test_merge_fast_forwards_like_git(repo):
    repo.git.run("checkout", "-b", "behind", repo.base_oid)
    audit = integrate(repo, "merge")
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.branch_oid("behind") == repo.parent_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_already_up_to_date_is_a_local_success(repo, operation):
    audit = integrate(repo, operation, target="HEAD")
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["result_oid"] == repo.source_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize("target", ["origin/main", "release", "HEAD~1"])
def test_git_revisions_are_supported_without_remote_access(repo, target):
    repo.git.run("update-ref", "refs/remotes/origin/main", repo.parent_oid)
    repo.git.run("tag", "release", repo.parent_oid)
    audit = integrate(repo, target=target)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    expected = repo.base_oid if target == "HEAD~1" else repo.parent_oid
    assert audit.manifest["target_oid"] == expected


@pytest.mark.parametrize("equivalent", [False, True])
def test_range_diff_handles_empty_or_dropped_series(repo, equivalent):
    repo.git.run("checkout", "-b", "integrated", repo.base_oid)
    if equivalent:
        repo.file.write_text("parent\n")
        repo.git.run("commit", "-am", "Equivalent parent change")
    audit = integrate(repo)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    comparison = (audit.directory / "range-diff.txt").read_text()
    assert " < " in comparison if equivalent else comparison == ""


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("kind", ["binary", "symlink"])
def test_unsupported_conflicts_stay_manual(repo, kind, operation):
    if kind == "binary":
        repo.file.write_bytes(b"source\0\n")
    elif kind == "symlink":
        repo.file.unlink()
        repo.file.symlink_to("/tmp/ez-do-not-touch")
    else:
        repo.file.unlink()
    repo.git.run("add", "-A")
    repo.git.run("commit", "--amend", "-m", kind)
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "failed", audit.manifest
    assert repo.resolver.calls == []


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_add_add_conflict(repo, operation):
    for branch, content in (("feature", "source"), ("main", "parent")):
        repo.git.run("checkout", branch)
        (repo.root / "added.txt").write_text(content)
        repo.git.run("add", "added.txt")
        repo.git.run("commit", "-m", "add file")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert any(c.path == "added.txt" and c.base is None for c in repo.resolver.calls)


@pytest.mark.parametrize("revision", ["--help", "missing"])
def test_invalid_target_does_not_create_run(repo, revision):
    with pytest.raises(EzError):
        integrate(repo, target=revision)
    assert not (repo.root / ".git/ez").exists()


def test_pathspec_characters_are_literal(repo):
    for branch, content in (("feature", "source"), ("main", "parent")):
        repo.git.run("checkout", branch)
        repo.git.run("mv", "file.txt", "[file]*.txt")
        (repo.root / "[file]*.txt").write_text(content + "\n")
        repo.git.run("add", "[file]*.txt")
        repo.git.run("commit", "--amend", "-m", "rename on both sides")
    repo.git.run("checkout", "feature")
    audit = integrate(repo)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert (repo.root / "[file]*.txt").read_text() == "combined\n"
    assert repo.git.run("ls-files", "--", "file.txt").stdout == b""


def test_delete_decision(repo):
    repo.resolver = FakeResolver(action="delete")
    audit = integrate(repo)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert not repo.file.exists()


def test_invocation_from_subdirectory_uses_repository_root(repo):
    subdirectory = repo.root / "src"
    subdirectory.mkdir()
    audit = Engine(subdirectory, repo.config, repo.resolver).run("main")
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["worktree"] == str(repo.root)


def test_wrong_merge_parent_cannot_produce_a_success(repo, mocker):
    original = Git.run

    def change_before_commit(git, *args, **kwargs):
        if args == ("merge", "--continue"):
            metadata = Path(
                git.text("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD")
            )
            metadata.write_text(repo.base_oid + "\n")
        return original(git, *args, **kwargs)

    mocker.patch.object(Git, "run", change_before_commit)
    audit = integrate(repo, "merge")
    assert audit.manifest["status"] == "failed", audit.manifest
    assert "does not contain the pinned target" in audit.manifest["error"]


@pytest.mark.parametrize("commit", [False, True])
def test_exit_snapshot_captures_validation_side_effects(repo, commit):
    script = (
        "from pathlib import Path; Path('file.txt').write_text('changed'); "
        "Path('new.txt').write_text('added')"
    )
    if commit:
        script += (
            "; import subprocess; subprocess.run(['git','add','file.txt','new.txt'],check=True); "
            "subprocess.run(['git','commit','-m','validation side effect'],check=True)"
        )
    audit = integrate(repo, validation=(sys.executable, "-c", script))
    assert audit.manifest["status"] == "failed"
    directory = audit.directory / "exit"
    snapshot = json.loads((directory / "files.json").read_text())
    records = {record["path"]: record for record in snapshot["files"]}
    assert (directory / records["file.txt"]["artifact"]).read_text() == "changed"
    assert (directory / records["new.txt"]["artifact"]).read_text() == "added"


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
    assert audit.manifest["status"] == "failed", audit.manifest
    assert "blocked by project hook" in (audit.directory / "integration.log").read_text()


def test_commit_signing_configuration_is_not_disabled(repo):
    repo.git.run("config", "commit.gpgsign", "true")
    repo.git.run("config", "gpg.format", "openpgp")
    repo.git.run("config", "gpg.program", "/nonexistent/ez-test-signing-program")
    audit = integrate(repo, "merge")
    assert audit.manifest["status"] == "failed", audit.manifest
    assert "ez-test-signing-program" in (audit.directory / "integration.log").read_text()


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_rerere_can_reuse_a_resolution_without_calling_the_provider(repo, operation):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "true")
    first = integrate(repo, operation)
    assert first.manifest["status"] == "succeeded", first.manifest
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    second = integrate(repo, operation)
    assert second.manifest["status"] == "succeeded", second.manifest
    assert repo.resolver.calls == []


def test_rerere_does_not_retry_a_rejected_commit_hook(repo):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "true")
    assert integrate(repo, "merge").manifest["status"] == "succeeded"
    repo.git.run("reset", "--hard", repo.source_oid)
    hook = repo.root / ".git/hooks/pre-commit"
    count = repo.root / ".git/hook-count"
    hook.write_text("#!/bin/sh\necho called >> .git/hook-count\nexit 1\n")
    hook.chmod(0o755)
    audit = integrate(repo, "merge")
    assert audit.manifest["status"] == "failed", audit.manifest
    assert count.read_text().splitlines() == ["called"]


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_interruption_preserves_partial_edits_and_the_active_operation(repo, mocker, operation):
    def interrupt(*args):
        repo.file.write_text("partial resolution\n")
        raise KeyboardInterrupt

    mocker.patch.object(repo.resolver, "run", side_effect=interrupt)
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "interrupted"
    assert not audit.manifest["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    conflict = audit.manifest["conflicts"][0]
    assert conflict["status"] == "interrupted"
    assert (
        audit.directory / conflict["artifacts"]["proposed"]
    ).read_text() == "partial resolution\n"
    assert (audit.directory / "exit/files.json").is_file()


def test_validation_timeout_retains_output_and_the_integrated_commit(repo, mocker):
    import subprocess

    original = subprocess.run
    validation = ("project-validation",)

    def run(command, **kwargs):
        if tuple(command) == validation:
            assert kwargs["timeout"] == repo.config.timeout
            raise subprocess.TimeoutExpired(
                command,
                timeout=kwargs["timeout"],
                output=b"validation started\n",
                stderr=b"validation progress\n",
            )
        return original(command, **kwargs)

    mocker.patch("git_ez.engine.subprocess.run", side_effect=run)
    audit = integrate(repo, validation=validation)
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"]
    assert "Validation command timed out" in audit.manifest["error"]
    assert "validation started" in (audit.directory / "validation.stdout").read_text()
    assert "validation progress" in (audit.directory / "validation.stderr").read_text()
    assert repo.git.branch_oid("feature") == audit.manifest["result_oid"]
    assert not repo.git.operation_active()


def test_rebasing_merge_commits_is_refused_before_creating_an_audit(repo):
    repo.git.run("checkout", "-b", "side")
    (repo.root / "side.txt").write_text("side\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "Side change")
    repo.git.run("checkout", "feature")
    repo.git.run("merge", "--no-ff", "--no-edit", "side")
    before = repo.git.text("rev-parse", "HEAD")
    with pytest.raises(EzError, match="Rebasing merge commits"):
        integrate(repo)
    assert repo.git.text("rev-parse", "HEAD") == before
    assert repo.resolver.calls == []
    assert not (repo.root / ".git/ez").exists()


def test_non_utf8_conflicts_remain_manual_with_a_saved_failure(repo):
    repo.file.write_bytes(b"source \xff\n")
    repo.git.run("commit", "--amend", "-am", "Non-UTF-8 input")
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert "Non-UTF-8 file requires manual resolution" in audit.manifest["error"]
    assert repo.resolver.calls == []
    assert repo.git.operation_active()
    assert (audit.directory / "report.md").is_file()


def test_conflict_round_limit_stops_before_calling_the_resolver_again(repo):
    from dataclasses import replace

    repo.file.write_text("second source change\n")
    repo.git.run("commit", "-am", "Second replayed change")
    repo.config = replace(repo.config, max_rounds=1)
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert "Maximum conflict rounds exceeded" in audit.manifest["error"]
    assert len(repo.resolver.calls) == 1
    assert repo.git.operation_active()
    assert repo.git.conflicts()


def test_oversized_proposals_cannot_be_staged_or_committed(repo):
    from dataclasses import replace

    repo.config = replace(repo.config, max_file_bytes=4)
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert "exceeds size limit" in audit.manifest["error"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert "Proposal exceeds size limit" in audit.manifest["conflicts"][0]["capture_error"]


def test_audit_distinguishes_deleted_and_unavailable_paths_without_following_links(repo, tmp_path):
    audit = integrate(repo)
    repo.file.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("private content")
    (repo.root / "linked.txt").symlink_to(outside)
    (repo.root / "directory").mkdir()
    destination = audit.directory / "inspection"
    audit.capture_worktree(repo.git, destination, 1024, paths=["directory", ".git/config"])
    records = {
        record["path"]: record
        for record in json.loads((destination / "files.json").read_text())["files"]
    }
    assert records["file.txt"]["state"] == "deleted"
    for name in ("linked.txt", "directory", ".git/config"):
        assert records[name]["state"] == "unavailable"
        assert "artifact" not in records[name]
    assert outside.read_text() == "private content"


def test_non_utf8_proposal_is_a_recorded_failure_not_an_unfinished_run(repo, mocker):
    def propose(*args):
        repo.file.write_bytes(b"invalid \xff\n")
        return {"rationale": "Attempted resolution", "confidence": 0.95}

    mocker.patch.object(repo.resolver, "run", side_effect=propose)
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert "Non-UTF-8" in audit.manifest["error"]
    assert not audit.manifest["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.file.read_bytes() == b"invalid \xff\n"
    assert json.loads((audit.directory / "run.json").read_text())["status"] == "failed"
    snapshot = json.loads((audit.directory / "exit/files.json").read_text())
    record = next(item for item in snapshot["files"] if item["path"] == "file.txt")
    assert (audit.directory / "exit" / record["artifact"]).read_bytes() == b"invalid \xff\n"


def test_final_audit_capture_failure_does_not_report_overall_success(repo, mocker):
    mocker.patch("git_ez.audit.Audit.capture_worktree", side_effect=OSError("disk full"))
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"]
    assert "audit capture failed" in audit.manifest["error"]
    assert audit.manifest["capture_error"] == "disk full"
    assert repo.git.branch_oid("feature") == audit.manifest["result_oid"]
    assert not repo.git.operation_active()
    saved = json.loads((audit.directory / "run.json").read_text())
    assert saved["status"] == "failed"
    assert "Final capture failed: disk full" in (audit.directory / "report.md").read_text()


@pytest.mark.parametrize("change", ["branch", "commit"])
def test_preflight_race_is_detected_before_git_integration(repo, mocker, change):
    engine = Engine(repo.root, repo.config, repo.resolver)
    prepare = engine._prepare_run
    changed_head = []

    def concurrent_change(*args):
        audit = prepare(*args)
        if change == "branch":
            repo.git.run("checkout", "main")
        else:
            repo.git.run("commit", "--allow-empty", "-m", "Concurrent user commit")
        changed_head.append(repo.git.text("rev-parse", "HEAD"))
        return audit

    mocker.patch.object(engine, "_prepare_run", side_effect=concurrent_change)
    audit = engine.run("main")
    assert audit.manifest["status"] == "failed"
    assert "Current branch changed before integration" in audit.manifest["error"]
    assert repo.resolver.calls == []
    assert not repo.git.operation_active()
    assert repo.git.text("rev-parse", "HEAD") == changed_head[0]
    assert not (audit.directory / "integration.log").exists()


def test_literal_target_oid_can_be_resolved_without_a_symbolic_target_ref(repo):
    audit = integrate(repo, target=repo.parent_oid)
    assert audit.manifest["status"] == "succeeded"
    assert audit.manifest["target_ref"] is None
    assert len(repo.resolver.calls) == 1
    repo.git.run("merge-base", "--is-ancestor", repo.parent_oid, "HEAD")
    assert repo.git.text("show", "HEAD:file.txt") == "combined"


def test_validation_switching_branch_at_the_same_commit_is_detected(repo):
    validation = (
        sys.executable,
        "-c",
        "import subprocess; subprocess.run(['git','checkout','-b','other'],check=True)",
    )
    audit = integrate(repo, validation=validation)
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"]
    assert "Checked-out branch changed" in audit.manifest["error"]
    assert repo.git.text("rev-parse", "HEAD") == audit.manifest["result_oid"]
    assert repo.git.text("branch", "--show-current") == "other"
    assert repo.git.branch_oid("feature") == audit.manifest["result_oid"]


def test_replacing_merge_result_with_target_does_not_lose_the_original_branch_silently(
    repo, mocker
):
    engine = Engine(repo.root, repo.config, repo.resolver)
    record = engine._record_result

    def concurrent_reset(audit):
        repo.git.run("reset", "--hard", "main")
        record(audit)

    mocker.patch.object(engine, "_record_result", side_effect=concurrent_reset)
    audit = engine.run("main", "merge")
    assert audit.manifest["status"] == "failed"
    assert "does not contain the original branch" in audit.manifest["error"]
    assert audit.manifest["result_oid"] == repo.parent_oid
    assert repo.git.branch_oid("feature") == repo.parent_oid
    assert (audit.directory / "report.md").is_file()


def test_final_capture_error_does_not_replace_the_original_resolver_failure(repo, mocker):
    repo.resolver = FakeResolver(error="model unavailable")
    mocker.patch("git_ez.audit.Audit.capture_worktree", side_effect=OSError("disk full"))
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["error"] == "model unavailable"
    assert audit.manifest["capture_error"] == "disk full"
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_modify_delete_conflicts_preserve_stage_meaning_and_apply_deletion(repo, operation):
    repo.git.run("rm", "file.txt")
    repo.git.run("commit", "--amend", "-m", "Delete on feature")
    before = repo.git.branch_oid("feature")
    repo.resolver = FakeResolver(action="delete")
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert len(repo.resolver.calls) == 1
    conflict = repo.resolver.calls[0]
    assert conflict.base == "base\n"
    assert (conflict.ours, conflict.theirs) == (
        ("parent\n", None) if operation == "rebase" else (None, "parent\n")
    )
    assert not repo.file.exists()
    assert not repo.git.operation_active()
    assert not repo.git.conflicts()
    repo.git.run("merge-base", "--is-ancestor", repo.parent_oid, "HEAD")
    if operation == "merge":
        repo.git.run("merge-base", "--is-ancestor", before, "HEAD")
    assert audit.manifest["conflicts"][0]["resolution"]["action"] == "delete"


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_rename_rename_conflict_can_keep_one_path_without_resurrecting_the_original(
    repo,
    mocker,
    operation,
):
    for branch, name in (("feature", "source.txt"), ("main", "target.txt")):
        repo.git.run("checkout", "-B", branch, repo.base_oid)
        repo.git.run("mv", "file.txt", name)
        repo.git.run("commit", "-m", f"Rename to {name}")
    repo.git.run("checkout", "feature")
    target = repo.git.branch_oid("main")
    before = repo.git.branch_oid("feature")

    def resolve(git, audit, round_number, paths, check):
        assert set(paths) == {"file.txt", "source.txt", "target.txt"}
        for name in ("file.txt", "source.txt"):
            (git.path / name).unlink(missing_ok=True)
        (git.path / "target.txt").write_text("base\n")
        return {
            "rationale": "Keep the target path without duplicating the file",
            "confidence": 0.95,
        }

    mocker.patch.object(repo.resolver, "run", side_effect=resolve)
    audit = integrate(repo, operation)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.text("ls-tree", "--name-only", "HEAD") == "target.txt"
    assert repo.git.text("show", "HEAD:target.txt") == "base"
    assert not repo.git.run("status", "--porcelain").stdout
    assert not repo.git.operation_active()
    repo.git.run("merge-base", "--is-ancestor", target, "HEAD")
    if operation == "merge":
        repo.git.run("merge-base", "--is-ancestor", before, "HEAD")
    original = next(c for c in audit.manifest["conflicts"] if c["path"] == "file.txt")
    assert "marked" not in original["artifacts"]
    assert original["resolution"]["action"] == "delete"


@pytest.mark.parametrize("change", ["index", "merge_head"])
def test_concurrent_control_change_after_resolution_blocks_git_continuation(repo, mocker, change):
    engine = Engine(repo.root, repo.config, repo.resolver)
    resolve = engine.resolve_conflicts

    def concurrent_change(git, *args):
        index = Path(git.text("rev-parse", "--path-format=absolute", "--git-path", "index"))
        original = index.read_bytes()
        resolve(git, *args)
        if change == "index":
            index.write_bytes(original)
        else:
            merge_head = Path(
                git.text(
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    "MERGE_HEAD",
                )
            )
            merge_head.write_text(repo.base_oid + "\n")

    mocker.patch.object(engine, "resolve_conflicts", side_effect=concurrent_change)
    audit = engine.run("main", "merge")
    assert audit.manifest["status"] == "failed"
    expected = "Unmerged index entries remain" if change == "index" else "Merge target changed"
    assert expected in audit.manifest["error"]
    assert not audit.manifest["integrated"]
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.git.operation_active()
    assert "git merge --continue" not in (audit.directory / "integration.log").read_text()


def test_branch_ref_change_during_final_verification_is_not_reported_as_success(repo, mocker):
    engine = Engine(repo.root, repo.config, repo.resolver)
    check = engine._check_repository
    record = engine._record_result
    recorded = []

    def record_result(audit):
        record(audit)
        recorded.append(audit.manifest["result_oid"])

    def concurrent_update():
        branch = check()
        if recorded:
            repo.git.run("update-ref", "refs/heads/feature", repo.source_oid)
        return branch

    mocker.patch.object(engine, "_record_result", side_effect=record_result)
    mocker.patch.object(engine, "_check_repository", side_effect=concurrent_update)
    audit = engine.run("main")
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"]
    assert "Current branch changed during integration" in audit.manifest["error"]
    assert audit.manifest["result_oid"] == recorded[0]
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert recorded[0] != repo.source_oid


@pytest.mark.parametrize("damage", ["oversized", "non_utf8"])
def test_unreadable_worktree_conflict_is_refused_before_sending_it_to_the_resolver(
    repo, mocker, damage
):
    engine = Engine(repo.root, repo.config, repo.resolver)
    snapshot = engine.snapshot

    def external_edit(git, audit, name, versions):
        path = git.path / name
        if damage == "oversized":
            with path.open("wb") as stream:
                stream.truncate(10_000_001)
        else:
            path.write_bytes(b"invalid \xff\n")
        return snapshot(git, audit, name, versions)

    mocker.patch.object(engine, "snapshot", side_effect=external_edit)
    audit = engine.run("main")
    assert audit.manifest["status"] == "failed"
    expected = "Marked file too large" if damage == "oversized" else "Non-UTF-8 working file"
    assert expected in audit.manifest["error"]
    assert repo.resolver.calls == []
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert "request" not in audit.manifest["conflicts"][0]["artifacts"]
    assert (audit.directory / "exit/files.json").is_file()


def test_binary_proposal_is_rejected_without_committing_it(repo):
    repo.resolver = FakeResolver(content="binary\0proposal")
    audit = integrate(repo)
    assert audit.manifest["status"] == "failed"
    assert "Binary content" in audit.manifest["error"]
    assert not audit.manifest["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    conflict = audit.manifest["conflicts"][0]
    assert conflict["status"] == "failed"
    assert (audit.directory / conflict["artifacts"]["proposed"]).read_bytes() == b"binary\0proposal"


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
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["merge_ff"] == policy
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
    assert "Requested fast-forward policy" in (audit.directory / "report.md").read_text()


@pytest.mark.parametrize("policy", [None, "ff", "no-ff", "ff-only"])
def test_merge_policy_leaves_up_to_date_branch_unchanged(repo, policy):
    audit = integrate(repo, "merge", target="HEAD", merge_ff=policy)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize("policy", ["ff", "no-ff"])
def test_merge_policy_survives_conflict_continuation(repo, policy):
    repo.git.run("config", "merge.ff", "only")
    audit = integrate(repo, "merge", merge_ff=policy)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.text("rev-parse", "HEAD^1") == repo.source_oid
    assert repo.git.text("rev-parse", "HEAD^2") == repo.parent_oid
    assert repo.file.read_text() == "combined\n"
    assert len(repo.resolver.calls) == 1
    assert not repo.git.operation_active()


def test_ff_only_refusal_preserves_state_and_audits_failure(repo):
    repo.git.run("config", "merge.ff", "false")
    refs = repo.git.text("show-ref")
    audit = integrate(repo, "merge", merge_ff="ff-only")
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"] is False
    assert repo.git.text("show-ref") == refs
    assert repo.file.read_text() == "source\n"
    assert repo.git.run("status", "--porcelain").stdout == b""
    assert not repo.git.operation_active()
    assert repo.resolver.calls == []
    assert audit.manifest["exit_state"] == "exit/files.json"
    assert "--ff-only" in (audit.directory / "integration.log").read_text()
    assert "--ff-only" in (audit.directory / "report.md").read_text()


@pytest.mark.parametrize("operation,policy", [("rebase", "ff"), ("merge", "invalid")])
def test_invalid_merge_policy_is_rejected_before_creating_audit(repo, operation, policy):
    with pytest.raises(EzError, match="[Ff]ast-forward"):
        integrate(repo, operation, merge_ff=policy)
    assert not (repo.root / ".git/ez").exists()
    assert repo.git.branch_oid("feature") == repo.source_oid


def test_rebase_onto_replays_only_selected_series(repo):
    repo.git.run("checkout", "-b", "old-base", repo.base_oid)
    (repo.root / "old.txt").write_text("old base only\n")
    repo.git.run("add", "old.txt")
    repo.git.run("commit", "-m", "old base")
    upstream = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "-b", "topic")
    (repo.root / "topic.txt").write_text("topic\n")
    repo.git.run("add", "topic.txt")
    repo.git.run("commit", "-m", "topic change")
    before = repo.git.text("rev-parse", "HEAD")
    audit = integrate(repo, target="old-base", onto="main")
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
    assert not (repo.root / "old.txt").exists()
    assert (repo.root / "topic.txt").read_text() == "topic\n"
    assert repo.git.branch_oid("old-base") == upstream
    assert repo.git.branch_oid("main") == repo.parent_oid
    assert repo.resolver.calls == []
    comparison = audit.manifest["range_diff"]
    assert comparison["before"] == f"{upstream}..{before}"
    assert comparison["after"] == f"{repo.parent_oid}..{audit.manifest['result_oid']}"
    assert " = " in (audit.directory / comparison["artifact"]).read_text()
    assert "New base: `main`" in (audit.directory / "report.md").read_text()


@pytest.mark.parametrize("target", ["HEAD", "HEAD~1"])
def test_rebase_onto_pins_head_relative_inputs_and_handles_empty_series(repo, target):
    audit = integrate(repo, target=target, onto="main")
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["target_ref"] is None
    assert audit.manifest["onto_oid"] == repo.parent_oid
    if target == "HEAD":
        assert repo.git.branch_oid("feature") == repo.parent_oid
        assert repo.resolver.calls == []
    else:
        assert repo.file.read_text() == "combined\n"
        assert len(repo.resolver.calls) == 1


@pytest.mark.parametrize("phase", ["preflight", "resolver", "validation"])
@pytest.mark.parametrize("changed_ref", ["main", "upstream"])
def test_rebase_onto_detects_changed_input_refs(repo, mocker, phase, changed_ref):
    repo.git.run("branch", "upstream", repo.base_oid)
    engine = Engine(repo.root, repo.config, repo.resolver)
    moved_oid = repo.source_oid
    if phase == "preflight":
        prepare = engine._prepare_run

        def prepare_and_move(*args):
            audit = prepare(*args)
            repo.git.run("update-ref", f"refs/heads/{changed_ref}", moved_oid)
            return audit

        mocker.patch.object(engine, "_prepare_run", side_effect=prepare_and_move)
    elif phase == "resolver":
        resolve = repo.resolver.run

        def resolve_and_move(*args):
            result = resolve(*args)
            repo.git.run("update-ref", f"refs/heads/{changed_ref}", moved_oid)
            return result

        mocker.patch.object(repo.resolver, "run", side_effect=resolve_and_move)
    command = (
        ("git", "update-ref", f"refs/heads/{changed_ref}", moved_oid)
        if phase == "validation"
        else ()
    )
    audit = engine.run("upstream", onto="main", validation=command)
    assert audit.manifest["status"] == "failed", audit.manifest
    assert "reference changed" in audit.manifest["error"] or "input refs" in audit.manifest["error"]
    assert audit.manifest["onto_oid"] == repo.parent_oid
    assert repo.git.branch_oid(changed_ref) == moved_oid
    if phase == "preflight":
        assert repo.resolver.calls == []
        assert repo.git.branch_oid("feature") == repo.source_oid
    elif phase == "resolver":
        assert repo.git.rebase_active()
    else:
        assert audit.manifest["integrated"]


@pytest.mark.parametrize("onto", ["missing", "--help", "main...feature"])
def test_invalid_onto_is_rejected_before_creating_audit(repo, onto):
    with pytest.raises(EzError):
        integrate(repo, onto=onto)
    assert not (repo.root / ".git/ez").exists()
    assert repo.git.branch_oid("feature") == repo.source_oid


def test_onto_is_rejected_for_merge_by_engine(repo):
    with pytest.raises(EzError, match="only supported for rebase"):
        integrate(repo, "merge", onto="main")
    assert not (repo.root / ".git/ez").exists()


def test_rebase_onto_records_a_fully_dropped_series(repo):
    repo.git.run("checkout", "main")
    repo.file.write_text("source\n")
    repo.git.run("commit", "-am", "equivalent feature change")
    new_base = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, target=repo.base_oid, onto="main")
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["result_oid"] == new_base
    assert " < " in (audit.directory / "range-diff.txt").read_text()
    assert repo.resolver.calls == []


def test_rebase_onto_rejects_a_result_missing_the_new_base(repo, mocker):
    engine = Engine(repo.root, repo.config, repo.resolver)
    record = engine._record_result

    def replace_result(audit):
        repo.git.run("reset", "--hard", repo.source_oid)
        record(audit)

    mocker.patch.object(engine, "_record_result", side_effect=replace_result)
    audit = engine.run(repo.base_oid, onto="main")
    assert audit.manifest["status"] == "failed"
    assert "does not contain" in audit.manifest["error"]
    assert repo.git.branch_oid("feature") == repo.source_oid


@pytest.mark.parametrize("policy", ["drop", "keep", "stop"])
@pytest.mark.parametrize("empty_message", [False, True])
def test_empty_policy_after_conflict_resolution(repo, policy, empty_message):
    if empty_message:
        repo.git.run("commit", "--amend", "--allow-empty-message", "-m", "")
    original_author = repo.git.text("show", "-s", "--format=%an <%ae>%n%aI", "HEAD")
    repo.resolver = FakeResolver(content="parent\n")
    audit = integrate(repo, rebase_options=RebaseOptions(empty=policy))
    assert len(repo.resolver.calls) == 1
    assert audit.manifest["rebase_options"]["empty"] == policy
    assert audit.manifest["empty_commits"][0]["policy"] == policy
    assert "Empty conflict resolutions" in (audit.directory / "report.md").read_text()
    if policy == "stop":
        assert audit.manifest["status"] == "failed"
        assert not audit.manifest["integrated"]
        assert repo.git.rebase_active()
        assert "resolution is empty" in audit.manifest["error"]
    else:
        assert audit.manifest["status"] == "succeeded", audit.manifest
        assert not repo.git.operation_active()
        if policy == "keep":
            assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
            assert repo.git.text("show", "-s", "--format=%B", "HEAD") == (
                "" if empty_message else "source change"
            )
            assert repo.git.text("show", "-s", "--format=%an <%ae>%n%aI", "HEAD") == original_author
            assert repo.git.run("diff", "HEAD^", "HEAD").stdout == b""
        else:
            assert repo.git.branch_oid("feature") == repo.parent_oid


@pytest.mark.parametrize("policy", ["drop", "keep", "stop"])
def test_reapply_cherry_picks_obeys_empty_policy_without_model(repo, policy):
    repo.git.run("checkout", "main")
    repo.file.write_text("source\n")
    repo.git.run("commit", "-am", "equivalent change")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, rebase_options=RebaseOptions(empty=policy, reapply_cherry_picks=True))
    assert repo.resolver.calls == []
    if policy == "stop":
        assert audit.manifest["status"] == "failed"
        assert repo.git.rebase_active()
        assert not audit.manifest["integrated"]
    else:
        assert audit.manifest["status"] == "succeeded", audit.manifest
        if policy == "keep":
            assert repo.git.text("rev-parse", "HEAD^") == target
            assert repo.git.run("diff", "HEAD^", "HEAD").stdout == b""
        else:
            assert repo.git.branch_oid("feature") == target


@pytest.mark.parametrize("keep", [None, True, False])
def test_rebase_controls_initially_empty_commits(repo, keep):
    repo.git.run("checkout", "-b", "empty", repo.base_oid)
    repo.git.run("commit", "--allow-empty", "-m", "intentional empty commit")
    audit = integrate(repo, rebase_options=RebaseOptions(keep_empty=keep))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.resolver.calls == []
    if keep is False:
        assert repo.git.branch_oid("empty") == repo.parent_oid
    else:
        assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
        assert repo.git.text("show", "-s", "--format=%s", "HEAD") == "intentional empty commit"


@pytest.mark.parametrize("force", [True, False])
def test_force_rebase_controls_replay_of_unchanged_series(repo, monkeypatch, force):
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+00:00")
    audit = integrate(repo, target=repo.base_oid, rebase_options=RebaseOptions(force_rebase=force))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert (repo.git.branch_oid("feature") != repo.source_oid) is force
    assert repo.resolver.calls == []


def test_empty_keep_honors_commit_hook_failure(repo):
    repo.resolver = FakeResolver(content="parent\n")
    hook = repo.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho empty-commit-rejected >&2\nexit 1\n")
    hook.chmod(0o755)
    audit = integrate(repo, rebase_options=RebaseOptions(empty="keep"))
    assert audit.manifest["status"] == "failed"
    assert "Cannot retain empty commit" in audit.manifest["error"]
    assert repo.git.rebase_active()
    assert "empty-commit-rejected" in (audit.directory / "integration.log").read_text()


def test_merge_rejects_rebase_options(repo):
    with pytest.raises(EzError, match="only supported for rebase"):
        integrate(repo, "merge", rebase_options=RebaseOptions(empty="keep"))
    assert not (repo.root / ".git/ez").exists()


@pytest.mark.parametrize("reapply", [True, False])
def test_reapply_cherry_picks_controls_equivalent_commit_replay(repo, reapply):
    repo.git.run("checkout", "-b", "equivalent", repo.base_oid)
    repo.file.write_text("source\n")
    repo.git.run("commit", "-am", "upstream equivalent")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(
        repo,
        target="equivalent",
        rebase_options=RebaseOptions(empty="keep", reapply_cherry_picks=reapply),
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.resolver.calls == []
    if reapply:
        assert repo.git.text("rev-parse", "HEAD^") == target
        assert repo.git.text("show", "-s", "--format=%s", "HEAD") == "source change"
    else:
        assert repo.git.branch_oid("feature") == target


@pytest.fixture
def ssh_signing(repo, tmp_path):
    key = tmp_path / "signing-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    allowed = tmp_path / "allowed-signers"
    allowed.write_text("test@example.invalid " + key.with_suffix(".pub").read_text())
    repo.git.run("config", "gpg.format", "ssh")
    repo.git.run("config", "user.signingkey", str(key))
    repo.git.run("config", "gpg.ssh.allowedSignersFile", str(allowed))
    return key


@pytest.mark.parametrize("conflict", [True, False])
def test_merge_message_and_signoff_survive_continuation(repo, conflict):
    if not conflict:
        repo.git.run("checkout", "-b", "clean", repo.base_oid)
        (repo.root / "independent.txt").write_text("new\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "independent")
    message = "Merge the feature\n\nKeep both intentions."
    audit = integrate(repo, "merge", commit_options=CommitOptions(message=message, signoff=True))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    actual = repo.git.text("show", "-s", "--format=%B", "HEAD")
    assert actual == message + "\n\nSigned-off-by: Test <test@example.invalid>"
    assert audit.manifest["commit_options"]["message"] == message
    assert len(repo.resolver.calls) == int(conflict)


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("explicit_key", [True, False])
def test_signing_options_create_verifiable_commits_after_conflict(
    repo, ssh_signing, operation, explicit_key
):
    if explicit_key:
        repo.git.run("config", "user.signingkey", "/nonexistent/default-signing-key")
    key = str(ssh_signing) if explicit_key else ""
    audit = integrate(repo, operation, commit_options=CommitOptions(gpg_sign=key, signoff=True))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    repo.git.run("verify-commit", "HEAD")
    assert "Signed-off-by: Test <test@example.invalid>" in repo.git.text(
        "show", "-s", "--format=%B", "HEAD"
    )
    assert len(repo.resolver.calls) == 1


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_explicit_no_sign_overrides_signing_configuration(repo, operation):
    repo.git.run("config", "commit.gpgsign", "true")
    repo.git.run("config", "gpg.program", "/nonexistent/signing-program")
    audit = integrate(repo, operation, commit_options=CommitOptions(gpg_sign=False, signoff=False))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert "gpgsig " not in repo.git.text("cat-file", "commit", "HEAD")
    assert "Signed-off-by:" not in repo.git.text("show", "-s", "--format=%B", "HEAD")


def test_retained_empty_commit_honors_signing_and_signoff(repo, ssh_signing):
    repo.resolver = FakeResolver(content="parent\n")
    audit = integrate(
        repo,
        rebase_options=RebaseOptions(empty="keep"),
        commit_options=CommitOptions(gpg_sign=str(ssh_signing), signoff=True),
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.run("diff", "HEAD^", "HEAD").stdout == b""
    repo.git.run("verify-commit", "HEAD")
    assert "Signed-off-by:" in repo.git.text("show", "-s", "--format=%B", "HEAD")


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_explicit_signing_failure_is_not_bypassed(repo, operation):
    repo.git.run("config", "gpg.program", "/nonexistent/explicit-signing-program")
    audit = integrate(repo, operation, commit_options=CommitOptions(gpg_sign=""))
    assert audit.manifest["status"] == "failed"
    assert not audit.manifest["integrated"]
    assert repo.git.operation_active()
    assert "explicit-signing-program" in (audit.directory / "integration.log").read_text()


def test_merge_verifies_signed_target(repo, ssh_signing):
    repo.git.run("checkout", "main")
    repo.git.run("commit", "--allow-empty", "-S", "-m", "signed target")
    target = repo.git.text("rev-parse", "HEAD")
    repo.git.run("checkout", "feature")
    audit = integrate(repo, "merge", commit_options=CommitOptions(verify_signatures=True))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.text("rev-parse", "HEAD^2") == target


def test_merge_signature_verification_refuses_unsigned_target_without_model(repo):
    audit = integrate(repo, "merge", commit_options=CommitOptions(verify_signatures=True))
    assert audit.manifest["status"] == "failed"
    assert not audit.manifest["integrated"]
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.resolver.calls == []
    assert not repo.git.operation_active()


def test_explicit_no_verify_signatures_overrides_configuration(repo):
    repo.git.run("config", "merge.verifySignatures", "true")
    audit = integrate(repo, "merge", commit_options=CommitOptions(verify_signatures=False))
    assert audit.manifest["status"] == "succeeded", audit.manifest


def test_merge_message_does_not_force_a_commit_on_fast_forward(repo):
    repo.git.run("checkout", "-b", "behind", repo.base_oid)
    audit = integrate(repo, "merge", commit_options=CommitOptions(message="unused", signoff=True))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.branch_oid("behind") == repo.parent_oid
    assert "Signed-off-by:" not in repo.git.text("show", "-s", "--format=%B", "HEAD")


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
    assert audit.manifest["status"] == "failed"
    assert not audit.manifest["integrated"]
    assert count.read_text().splitlines() == ["rejected"]
    assert repo.git.operation_active() is (operation == "merge")


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("autoupdate", [True, False])
def test_explicit_rerere_policy_overrides_configuration_without_changing_it(
    repo, operation, autoupdate
):
    repo.git.run("config", "rerere.enabled", "true")
    configured = "false" if autoupdate else "true"
    repo.git.run("config", "rerere.autoupdate", configured)
    first = integrate(repo, operation, rerere_autoupdate=True)
    assert first.manifest["status"] == "succeeded", first.manifest
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    second = integrate(repo, operation, rerere_autoupdate=autoupdate)
    assert second.manifest["status"] == "succeeded", second.manifest
    assert repo.git.text("config", "--get", "rerere.autoupdate") == configured
    assert second.manifest["rerere_autoupdate"] is autoupdate
    assert second.manifest["rerere_autoupdate_effective"] is autoupdate
    assert len(repo.resolver.calls) == (0 if autoupdate else 1)
    assert repo.file.read_text() == "combined\n"
    if autoupdate:
        assert second.manifest["native_resolutions"][0]["mechanism"] == "rerere"
    else:
        assert repo.resolver.calls[0].marked == "combined\n"
    assert "Effective rerere autoupdate" in (second.directory / "report.md").read_text()
    assert (
        f"rerere.autoupdate={str(autoupdate).lower()}"
        in (second.directory / "integration.log").read_text()
    )


def test_explicit_rerere_policy_does_not_retry_a_rejected_hook(repo):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "false")
    assert integrate(repo, "merge", rerere_autoupdate=True).manifest["status"] == "succeeded"
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    hook = repo.root / ".git/hooks/pre-commit"
    count = repo.root / ".git/hook-count"
    hook.write_text("#!/bin/sh\necho rejected >> .git/hook-count\nexit 1\n")
    hook.chmod(0o755)
    audit = integrate(
        repo,
        "merge",
        rerere_autoupdate=True,
        commit_options=CommitOptions(signoff=True),
    )
    assert audit.manifest["status"] == "failed"
    assert count.read_text().splitlines() == ["rejected"]
    assert repo.resolver.calls == []
    assert repo.git.operation_active()


def test_autoupdate_flag_does_not_enable_disabled_rerere(repo):
    repo.git.run("config", "rerere.enabled", "false")
    audit = integrate(repo, "merge", rerere_autoupdate=True)
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert len(repo.resolver.calls) == 1
    assert "native_resolutions" not in audit.manifest
    assert repo.git.text("config", "--get", "rerere.enabled") == "false"


def test_rerere_override_is_kept_across_multiple_rebase_steps(repo, mocker):
    repo.git.run("config", "rerere.enabled", "true")
    repo.git.run("config", "rerere.autoupdate", "false")
    repo.file.write_text("second source\n")
    repo.git.run("commit", "-am", "second source change")
    original = repo.git.text("rev-parse", "HEAD")
    resolve = repo.resolver.run

    def distinct_resolutions(git, audit, round_number, paths, check):
        repo.resolver.content = f"combined {round_number}\n"
        return resolve(git, audit, round_number, paths, check)

    mocker.patch.object(repo.resolver, "run", side_effect=distinct_resolutions)
    first = integrate(repo, rerere_autoupdate=True)
    assert first.manifest["status"] == "succeeded", first.manifest
    assert len(repo.resolver.calls) == 2
    repo.git.run("reset", "--hard", original)
    repo.resolver.calls.clear()
    second = integrate(repo, rerere_autoupdate=True)
    assert second.manifest["status"] == "succeeded", second.manifest
    assert repo.resolver.calls == []
    assert repo.git.text("config", "--get", "rerere.autoupdate") == "false"
    assert repo.file.read_text() == "combined 2\n"


def test_invalid_validation_argument_is_a_recorded_failure(repo):
    repo.git.run("checkout", "-b", "behind", repo.base_oid)
    audit = integrate(repo, "merge", validation=(sys.executable, "-c", "print('ok')\0"))
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"]
    assert "embedded null byte" in audit.manifest["error"]
    assert repo.git.branch_oid("behind") == repo.parent_oid
    assert not repo.git.operation_active()
    assert repo.resolver.calls == []
    assert audit.manifest["exit_state"] == "exit/files.json"
    saved = json.loads((audit.directory / "run.json").read_text())
    assert saved["status"] == "failed"
    assert "embedded null byte" in saved["error"]
    assert "embedded null byte" in (audit.directory / "report.md").read_text()


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
    assert audit.manifest["status"] == "succeeded", audit.manifest
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
    assert audit.manifest["rebase_sequence"]["status"] == "captured"
    plan = (audit.directory / "rebase-todo.txt").read_text()
    assert plan.index("feature A") < plan.index(f"{marker} feature A") < plan.index("unrelated")
    assert message in (audit.directory / "rebase-commits.log").read_text().replace("\n    ", "\n")
    report = (audit.directory / "report.md").read_text()
    assert "Native Git instructions" in report
    assert "Resulting commits and messages" in report
    assert not audit.manifest["result_commits"]["truncated"]
    assert not repo.git.operation_active()


@pytest.mark.parametrize("autosquash", [None, False])
def test_autosquash_requires_explicit_opt_in_even_with_native_configuration(repo, autosquash):
    autosquash_series(repo)
    repo.git.run("config", "rebase.autoSquash", "true")
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=autosquash))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.text("rev-list", "--count", f"{repo.parent_oid}..HEAD") == "3"
    assert "rebase_sequence" not in audit.manifest
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
    assert audit.manifest["status"] == "succeeded", audit.manifest
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
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert len(repo.resolver.calls) == 2
    assert repo.git.text("rev-list", "--count", f"{target}..HEAD") == "2"
    assert (repo.root / "a.txt").read_text() == "combined\n"
    assert not audit.manifest.get("empty_commits")
    assert not repo.git.operation_active()


def test_autosquash_resulting_commits_are_signed(repo, ssh_signing):
    autosquash_series(repo)
    audit = integrate(
        repo,
        rebase_options=RebaseOptions(autosquash=True),
        commit_options=CommitOptions(gpg_sign=str(ssh_signing), signoff=True),
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    for commit in repo.git.text("rev-list", f"{repo.parent_oid}..HEAD").splitlines():
        repo.git.run("verify-commit", commit)
        assert "Signed-off-by:" in repo.git.text("show", "-s", "--format=%B", commit)


def test_autosquash_capture_limit_failure_is_audited_before_replay(repo):
    from dataclasses import replace

    autosquash_series(repo)
    before = repo.git.text("rev-parse", "HEAD")
    repo.config = replace(repo.config, max_file_bytes=16)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.manifest["status"] == "failed"
    assert not audit.manifest["integrated"]
    assert audit.manifest["rebase_sequence"]["status"] == "unavailable"
    assert "capture limit" in (audit.directory / "integration.log").read_text()
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
    assert audit.manifest["status"] == "failed", audit.manifest
    assert audit.manifest["integrated"]
    assert "sequence capture is unavailable" in audit.manifest["error"]
    assert audit.manifest["rebase_sequence"]["status"] == "unavailable"
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
    assert audit.manifest["rebase_sequence"]["status"] == "captured"
    assert repo.resolver.calls == []
    if policy == "stop":
        assert audit.manifest["status"] == "failed"
        assert repo.git.rebase_active()
    else:
        assert audit.manifest["status"] == "succeeded", audit.manifest
        assert repo.git.text("rev-list", "--count", f"{target}..HEAD") == (
            "1" if policy == "keep" else "0"
        )


def test_autosquash_handles_an_empty_sequence(repo):
    before = repo.git.text("rev-parse", "HEAD")
    audit = integrate(repo, target="HEAD", rebase_options=RebaseOptions(autosquash=True))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["result_oid"] == before
    assert audit.manifest["rebase_sequence"]["status"] == "captured"
    assert (audit.directory / "rebase-commits.log").read_bytes() == b""
    assert repo.resolver.calls == []


def test_autosquash_leaves_unmatched_markers_as_picks(repo):
    autosquash_series(repo)
    repo.git.run("commit", "--amend", "-m", "fixup! missing target")
    audit = integrate(
        repo, onto="main", target=repo.base_oid, rebase_options=RebaseOptions(autosquash=True)
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert repo.git.text("rev-list", "--count", f"{repo.parent_oid}..HEAD") == "3"
    assert repo.git.text("show", "-s", "--format=%s", "HEAD") == "fixup! missing target"
    assert repo.resolver.calls == []


def test_autosquash_log_truncation_is_explicit(repo):
    from dataclasses import replace

    autosquash_series(repo, "squash!")
    repo.git.run("commit", "--amend", "-m", "squash! feature A\n\n" + "x" * 8000)
    repo.config = replace(repo.config, max_file_bytes=4096)
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert audit.manifest["result_commits"]["truncated"]
    assert (audit.directory / "rebase-commits.log").stat().st_size == 4096
    assert "Truncated: `True`" in (audit.directory / "report.md").read_text()


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
    assert audit.manifest["status"] == "failed"
    assert repo.git.rebase_active()
    assert repo.git.branch_oid("topic") == before
    assert audit.manifest["rebase_sequence"]["status"] == "captured"
    assert "fixup " in (audit.directory / "rebase-todo.txt").read_text()
    assert audit.manifest["conflicts"]
    assert "result_commits" not in audit.manifest


def test_autosquash_does_not_bypass_pre_rebase_hook(repo):
    autosquash_series(repo)
    hook = repo.root / ".git/hooks/pre-rebase"
    hook.write_text("#!/bin/sh\necho autosquash-rejected >&2\nexit 1\n")
    hook.chmod(0o755)
    before = repo.git.text("rev-parse", "HEAD")
    audit = integrate(repo, rebase_options=RebaseOptions(autosquash=True))
    assert audit.manifest["status"] == "failed"
    assert repo.git.branch_oid("topic") == before
    assert audit.manifest["rebase_sequence"]["status"] == "unavailable"
    assert "autosquash-rejected" in (audit.directory / "integration.log").read_text()
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
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"]
    assert audit.manifest["rebase_sequence"]["status"] == "unavailable"
    assert "sequence changed" in audit.manifest["rebase_sequence"]["error"]


def test_autosquash_sequence_is_checked_after_final_validation(repo):
    autosquash_series(repo)
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "[p.unlink() for p in Path('.git/ez/runs').glob('*/rebase-todo.txt')]",
    )
    audit = integrate(repo, validation=command, rebase_options=RebaseOptions(autosquash=True))
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["integrated"]
    assert audit.manifest["validation"]["returncode"] == 0
    assert audit.manifest["rebase_sequence"]["status"] == "unavailable"
    assert "sequence capture is unavailable" in audit.manifest["error"]
