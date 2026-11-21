# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local integrations on real repositories, without a model or network."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from git_ez.config import Config
from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.git import Git
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
