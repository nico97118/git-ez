# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local integrations on real repositories, without a model or network."""

import json
import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from git_ez.audit import Audit
from git_ez.cli import main
from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.git import Git
from git_ez.locking import checkout_lock
from tests.fakes import FakeResolver
from tests.helpers import integrate

pytestmark = pytest.mark.integration


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
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["integrated"]
    result = audit.view["result_oid"]
    assert repo.git.branch_oid("feature") == result
    assert repo.git.branch_oid("main") == repo.parent_oid
    assert repo.git.text("branch", "--show-current") == "feature"
    assert repo.file.read_text() == "combined\n"
    if operation == "merge":
        assert repo.git.text("rev-parse", "HEAD^1") == repo.source_oid
        assert repo.git.text("rev-parse", "HEAD^2") == repo.parent_oid
    else:
        assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
    assert repo.git.run("status", "--porcelain").stdout == b""
    assert repo.git.text("for-each-ref", "--format=%(refname)", "refs/heads/") == refs
    assert repo.git.run("worktree", "list", "--porcelain").stdout.count(b"worktree ") == 1
    assert audit.view["worktree"] == str(repo.root)
    assert "result_branch" not in audit.view
    assert "push" not in audit.view
    conflict = repo.resolver.calls[0]
    expected = ("parent\n", "source\n") if operation == "rebase" else ("source\n", "parent\n")
    assert (conflict.ours, conflict.theirs) == expected
    assert conflict.operation == operation
    record = audit.view["conflicts"][0]
    for artifact in record["artifacts"].values():
        assert (audit.directory / artifact).is_file()
    assert record["resolution"]["rationale"] == "Combine both intentions"
    report = (audit.directory / "report.md").read_text()
    assert "Combine both intentions" in report
    assert operation in report
    assert "Revue facultative" not in report
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
    assert audit.view["status"] == "succeeded", audit.view
    assert remote_git.branch_oid("feature") == repo.source_oid
    assert repo.git.branch_oid("feature") != repo.source_oid


def test_multiple_replayed_commits(repo):
    repo.file.write_text("source second\n")
    repo.git.run("commit", "-am", "second change")
    audit = integrate(repo)
    assert audit.view["status"] == "succeeded", audit.view
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
    assert audit.view["status"] == "failed", audit.view
    assert audit.view["integrated"]
    assert audit.view["validation"]["returncode"] == 7
    assert audit.view["validation"]["status"] == "failed"
    assert repo.git.branch_oid("feature") == audit.view["result_oid"]


def test_successful_validation(repo):
    audit = integrate(repo, validation=(sys.executable, "-c", "print('ok')"))
    assert audit.view["status"] == "succeeded", audit.view
    assert (audit.directory / "validation.stdout").read_text() == "ok\n"
    assert audit.view["validation"]["status"] == "passed"


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
    assert audit.view["status"] == "failed", audit.view


def test_target_changed_during_validation_is_detected(repo):
    script = (
        "import subprocess; "
        f"subprocess.run(['git','update-ref','refs/heads/main','{repo.source_oid}'],check=True)"
    )
    audit = integrate(repo, validation=(sys.executable, "-c", script))
    assert audit.view["status"] == "failed", audit.view
    assert "Target reference changed" in audit.view["error"]


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_low_confidence_keeps_active_operation_and_saved_proposal(repo, operation):
    repo.resolver.confidence = 0.2
    audit = integrate(repo, operation)
    assert audit.view["status"] == "failed", audit.view
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.file.read_text() == "combined\n"
    record = audit.view["conflicts"][0]
    assert record["status"] == "low_confidence"
    assert (audit.directory / record["artifacts"]["proposed"]).read_text() == "combined\n"
    assert "Combine both intentions" in (audit.directory / "report.md").read_text()


def test_invalid_edits_keep_proposal(repo):
    repo.resolver.content = "<<<<<<< bad\n"
    audit = integrate(repo)
    assert audit.view["status"] == "failed"
    assert "proposed" in audit.view["conflicts"][0]["artifacts"]


def test_provider_error_keeps_input(repo):
    repo.resolver = FakeResolver(error="offline")
    audit = integrate(repo)
    assert audit.view["status"] == "failed"
    assert "request" in audit.view["conflicts"][0]["artifacts"]


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_no_conflict_does_not_call_provider(repo, operation):
    repo.git.run("checkout", "-b", "clean", repo.base_oid)
    (repo.root / "new.txt").write_text("new\n")
    repo.git.run("add", "new.txt")
    repo.git.run("commit", "-m", "independent change")
    audit = integrate(repo, operation)
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.resolver.calls == []


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_already_up_to_date_is_a_local_success(repo, operation):
    audit = integrate(repo, operation, target="HEAD")
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["result_oid"] == repo.source_oid
    assert repo.resolver.calls == []


@pytest.mark.parametrize("target", ["origin/main", "release", "HEAD~1"])
def test_git_revisions_are_supported_without_remote_access(repo, target):
    repo.git.run("update-ref", "refs/remotes/origin/main", repo.parent_oid)
    repo.git.run("tag", "release", repo.parent_oid)
    audit = integrate(repo, target=target)
    assert audit.view["status"] == "succeeded", audit.view
    expected = repo.base_oid if target == "HEAD~1" else repo.parent_oid
    assert audit.view["target_oid"] == expected


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
    assert audit.view["status"] == "failed", audit.view
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
    assert audit.view["status"] == "succeeded", audit.view
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
    assert audit.view["status"] == "succeeded", audit.view
    assert (repo.root / "[file]*.txt").read_text() == "combined\n"
    assert repo.git.run("ls-files", "--", "file.txt").stdout == b""


def test_delete_decision(repo):
    repo.resolver = FakeResolver(action="delete")
    audit = integrate(repo)
    assert audit.view["status"] == "succeeded", audit.view
    assert not repo.file.exists()


def test_invocation_from_subdirectory_uses_repository_root(repo):
    subdirectory = repo.root / "src"
    subdirectory.mkdir()
    audit = Engine(subdirectory, repo.config, repo.resolver).run("main")
    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["worktree"] == str(repo.root)


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
    assert audit.view["status"] == "failed", audit.view
    assert "does not contain the pinned target" in audit.view["error"]


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_interruption_preserves_partial_edits_and_the_active_operation(repo, mocker, operation):
    def interrupt(*args):
        repo.file.write_text("partial resolution\n")
        raise KeyboardInterrupt

    mocker.patch.object(repo.resolver, "run", side_effect=interrupt)
    audit = integrate(repo, operation)
    assert audit.view["status"] == "interrupted"
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    conflict = audit.view["conflicts"][0]
    assert conflict["status"] == "interrupted"
    assert (
        audit.directory / conflict["artifacts"]["proposed"]
    ).read_text() == "partial resolution\n"


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
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert "Validation command timed out" in audit.view["error"]
    assert audit.view["validation"]["status"] == "timed_out"
    assert "validation started" in (audit.directory / "validation.stdout").read_text()
    assert "validation progress" in (audit.directory / "validation.stderr").read_text()
    assert repo.git.branch_oid("feature") == audit.view["result_oid"]
    assert not repo.git.operation_active()


def test_non_utf8_conflicts_remain_manual_with_a_saved_failure(repo):
    repo.file.write_bytes(b"source \xff\n")
    repo.git.run("commit", "--amend", "-am", "Non-UTF-8 input")
    audit = integrate(repo)
    assert audit.view["status"] == "failed"
    assert "Non-UTF-8 file requires manual resolution" in audit.view["error"]
    assert repo.resolver.calls == []
    assert repo.git.operation_active()
    assert (audit.directory / "report.md").is_file()


def test_conflict_round_limit_stops_before_calling_the_resolver_again(repo):
    from dataclasses import replace

    repo.file.write_text("second source change\n")
    repo.git.run("commit", "-am", "Second replayed change")
    repo.config = replace(repo.config, max_rounds=1)
    audit = integrate(repo)
    assert audit.view["status"] == "failed"
    assert "Maximum conflict rounds exceeded" in audit.view["error"]
    assert len(repo.resolver.calls) == 1
    assert repo.git.operation_active()
    assert repo.git.conflicts()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("side", ["main", "feature"])
@pytest.mark.parametrize("kind", ["binary", "large"])
@pytest.mark.parametrize("interactive", [False, True])
def test_git_integrated_resources_do_not_require_text_resolution(
    repo, mocker, operation, side, kind, interactive
):
    content = b"image\x00\xff" if kind == "binary" else b"x" * (repo.config.max_file_bytes + 1)
    repo.git.run("checkout", side)
    resource = repo.root / "resource.dat"
    resource.write_bytes(content)
    repo.git.run("add", "resource.dat")
    repo.git.run("commit", "--amend", "--no-edit")
    if side == "feature":
        repo.source_oid = repo.git.branch_oid("feature")
    repo.git.run("checkout", "feature")

    if interactive:
        mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, operation, interactive=interactive)

    assert audit.view["status"] == "succeeded", audit.view
    assert audit.view["integrated"]
    assert len(repo.resolver.calls) == 1
    assert resource.read_bytes() == content
    assert repo.git.run("show", "HEAD:resource.dat").stdout == content
    assert repo.git.text("status", "--porcelain") == ""
    assert audit.view["conflicts"][0]["status"] == "applied"


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("kind", ["binary", "large", "markers", "non_utf8"])
@pytest.mark.parametrize("existing", [False, True])
def test_agent_supporting_changes_still_require_valid_text(repo, mocker, operation, kind, existing):
    resource = repo.root / "resource.dat"
    if existing:
        repo.git.run("checkout", "feature")
        resource.write_bytes(b"Git supplied\x00resource")
        repo.git.run("add", "resource.dat")
        repo.git.run("commit", "--amend", "--no-edit")
        repo.source_oid = repo.git.branch_oid("feature")
    contents = {
        "binary": b"agent\x00proposal",
        "large": b"x" * (repo.config.max_file_bytes + 1),
        "markers": b"<<<<<<< agent\nleft\n=======\nright\n>>>>>>> other\n",
        "non_utf8": b"agent\xffproposal",
    }
    resolve = repo.resolver.run

    def propose(*args):
        decision = resolve(*args)
        resource.write_bytes(contents[kind])
        return decision

    mocker.patch.object(repo.resolver, "run", side_effect=propose)
    audit = integrate(repo, operation)

    assert audit.view["status"] == "failed"
    assert "resource.dat" in audit.view["error"]
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert resource.read_bytes() == contents[kind]
    assert audit.view["conflicts"][0]["status"] == "failed"


@pytest.mark.parametrize("change", ["restore_head", "mode"])
def test_agent_changes_to_git_resources_are_checked_even_outside_head_diff(repo, mocker, change):
    resource = repo.root / "resource.dat"
    resource.write_bytes(b"old\x00resource")
    repo.git.run("add", "resource.dat")
    repo.git.run("commit", "-m", "add shared resource")
    common = repo.git.text("rev-parse", "HEAD")
    repo.git.run("branch", "-f", "main", common)
    repo.file.write_text("feature text\n")
    repo.git.run("commit", "-am", "change feature text")
    repo.source_oid = repo.git.branch_oid("feature")
    repo.git.run("checkout", "main")
    repo.file.write_text("target text\n")
    resource.write_bytes(b"Git\x00resource")
    repo.git.run("commit", "-am", "change target text and resource")
    repo.git.run("checkout", "feature")
    resolve = repo.resolver.run

    def propose(*args):
        decision = resolve(*args)
        if change == "restore_head":
            resource.write_bytes(repo.git.run("show", "HEAD:resource.dat").stdout)
        else:
            resource.chmod(0o755)
        return decision

    mocker.patch.object(repo.resolver, "run", side_effect=propose)
    audit = integrate(repo, "merge")

    assert audit.view["status"] == "failed"
    assert "resource.dat" in audit.view["error"]
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid


def test_oversized_proposals_cannot_be_staged_or_committed(repo):
    from dataclasses import replace

    repo.config = replace(repo.config, max_file_bytes=4)
    audit = integrate(repo)
    assert audit.view["status"] == "failed"
    assert "exceeds size limit" in audit.view["error"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert "Proposal exceeds size limit" in audit.view["conflicts"][0]["capture_error"]


def test_non_utf8_proposal_is_a_recorded_failure_not_an_unfinished_run(repo, mocker):
    def propose(*args):
        repo.file.write_bytes(b"invalid \xff\n")
        return {"rationale": "Attempted resolution", "confidence": 0.95}

    mocker.patch.object(repo.resolver, "run", side_effect=propose)
    audit = integrate(repo)
    assert audit.view["status"] == "failed"
    assert "Non-UTF-8" in audit.view["error"]
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.file.read_bytes() == b"invalid \xff\n"
    assert json.loads((audit.directory / "run.json").read_text())["status"] == "failed"
    assert audit.view["conflicts"][0]["resolution"]["rationale"] == "Attempted resolution"


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
    assert audit.view["status"] == "failed"
    assert "Current branch changed before integration" in audit.view["error"]
    assert repo.resolver.calls == []
    assert not repo.git.operation_active()
    assert repo.git.text("rev-parse", "HEAD") == changed_head[0]


def test_literal_target_oid_can_be_resolved_without_a_symbolic_target_ref(repo):
    audit = integrate(repo, target=repo.parent_oid)
    assert audit.view["status"] == "succeeded"
    assert audit.view["target_ref"] is None
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
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert "Checked-out branch changed" in audit.view["error"]
    assert repo.git.text("rev-parse", "HEAD") == audit.view["result_oid"]
    assert repo.git.text("branch", "--show-current") == "other"
    assert repo.git.branch_oid("feature") == audit.view["result_oid"]


def test_replacing_merge_result_with_target_does_not_lose_the_original_branch_silently(
    repo, mocker
):
    engine = Engine(repo.root, repo.config, repo.resolver)
    verify = engine._verify_result

    def concurrent_reset(audit):
        repo.git.run("reset", "--hard", "main")
        verify(audit)

    mocker.patch.object(engine, "_verify_result", side_effect=concurrent_reset)
    audit = engine.run("main", "merge")
    assert audit.view["status"] == "failed"
    assert "Validation changed HEAD" in audit.view["error"]
    assert audit.view["result_oid"] != repo.parent_oid
    assert repo.git.branch_oid("feature") == repo.parent_oid
    assert (audit.directory / "report.md").is_file()


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_modify_delete_conflicts_preserve_stage_meaning_and_apply_deletion(repo, operation):
    repo.git.run("rm", "file.txt")
    repo.git.run("commit", "--amend", "-m", "Delete on feature")
    before = repo.git.branch_oid("feature")
    repo.resolver = FakeResolver(action="delete")
    audit = integrate(repo, operation)
    assert audit.view["status"] == "succeeded", audit.view
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
    assert audit.view["conflicts"][0]["resolution"]["action"] == "delete"


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
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("ls-tree", "--name-only", "HEAD") == "target.txt"
    assert repo.git.text("show", "HEAD:target.txt") == "base"
    assert not repo.git.run("status", "--porcelain").stdout
    assert not repo.git.operation_active()
    repo.git.run("merge-base", "--is-ancestor", target, "HEAD")
    if operation == "merge":
        repo.git.run("merge-base", "--is-ancestor", before, "HEAD")
    original = next(c for c in audit.view["conflicts"] if c["path"] == "file.txt")
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
    assert audit.view["status"] == "failed"
    expected = "Unmerged index entries remain" if change == "index" else "Merge target changed"
    assert expected in audit.view["error"]
    assert not audit.view["integrated"]
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert repo.git.operation_active()


def test_branch_ref_change_during_final_verification_is_not_reported_as_success(repo, mocker):
    engine = Engine(repo.root, repo.config, repo.resolver)
    branch_oid = engine.git.branch_oid
    verify = engine._verify_result
    recorded = []

    def record_result(audit):
        recorded.append(audit.view["result_oid"])
        verify(audit)

    def concurrent_update(branch):
        if recorded:
            repo.git.run("update-ref", "refs/heads/feature", repo.source_oid)
        return branch_oid(branch)

    mocker.patch.object(engine, "_verify_result", side_effect=record_result)
    mocker.patch.object(engine.git, "branch_oid", side_effect=concurrent_update)
    audit = engine.run("main")
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert "Current branch changed during integration" in audit.view["error"]
    assert audit.view["result_oid"] == recorded[0]
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert recorded[0] != repo.source_oid


@pytest.mark.parametrize("damage", ["oversized", "non_utf8"])
def test_unreadable_worktree_conflict_is_refused_before_sending_it_to_the_resolver(
    repo, mocker, damage
):
    engine = Engine(repo.root, repo.config, repo.resolver)
    snapshot = engine.snapshot

    def external_edit(git, audit, name, versions, stop_id=None):
        path = git.path / name
        if damage == "oversized":
            with path.open("wb") as stream:
                stream.truncate(10_000_001)
        else:
            path.write_bytes(b"invalid \xff\n")
        return snapshot(git, audit, name, versions, stop_id)

    mocker.patch.object(engine, "snapshot", side_effect=external_edit)
    audit = engine.run("main")
    assert audit.view["status"] == "failed"
    expected = "Marked file too large" if damage == "oversized" else "Non-UTF-8 working file"
    assert expected in audit.view["error"]
    assert repo.resolver.calls == []
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert "request" not in audit.view["conflicts"][0]["artifacts"]


def test_binary_proposal_is_rejected_without_committing_it(repo):
    repo.resolver = FakeResolver(content="binary\0proposal")
    audit = integrate(repo)
    assert audit.view["status"] == "failed"
    assert "Binary content" in audit.view["error"]
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source_oid
    conflict = audit.view["conflicts"][0]
    assert conflict["status"] == "failed"
    assert (audit.directory / conflict["artifacts"]["proposed"]).read_bytes() == b"binary\0proposal"


def test_invalid_validation_argument_is_a_recorded_failure(repo):
    repo.git.run("checkout", "-b", "behind", repo.base_oid)
    audit = integrate(repo, "merge", validation=(sys.executable, "-c", "print('ok')\0"))
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert audit.view["failure"]["type"] == "ValueError"
    assert repo.git.branch_oid("behind") == repo.parent_oid
    assert not repo.git.operation_active()
    assert repo.resolver.calls == []
    saved = json.loads((audit.directory / "run.json").read_text())
    assert saved["status"] == "failed"
    assert saved["failure"]["type"] == "ValueError"
    assert "ValueError" in (audit.directory / "report.md").read_text()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("choice", ["accept", "stop", "eof", "interrupt", "revise"])
def test_interactive_review_preserves_or_applies_proposals(repo, mocker, operation, choice):
    answers = iter(
        ["r", "add a test", "r", "keep both intentions", "a"]
        if choice == "revise"
        else ["a" if choice == "accept" else "s"]
    )
    observed = []
    original = repo.resolver.run

    def resolve(git, audit, number, paths, check):
        observed.append((repo.file.read_text(), audit.view["reviews"][-1]["feedback"]))
        return original(git, audit, number, paths, check)

    repo.resolver.run = resolve

    def read(prompt):
        assert repo.git.run("ls-files", "-u").stdout
        if choice == "eof":
            raise EOFError
        if choice == "interrupt":
            raise KeyboardInterrupt
        return next(answers)

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", operation, interactive=True)
    success = choice in ("accept", "revise")
    assert (audit.view["status"] == "succeeded") == success, audit.view
    assert bool(repo.git.run("ls-files", "-u").stdout) != success
    assert repo.file.read_text() == ("combined\n" if success else observed[0][0])
    reviews = audit.view["reviews"]
    assert len(reviews) == (3 if choice == "revise" else 1)
    for proposal in reviews:
        assert (audit.directory / proposal["artifacts"] / "files.json").is_file()
    if choice == "revise":
        assert observed[1:] == [
            (observed[0][0], "add a test"),
            (observed[0][0], "keep both intentions"),
        ]


def test_interactive_review_rechecks_state(repo, mocker):
    def read(prompt):
        repo.git.run("update-ref", "refs/heads/main", repo.base_oid)
        return "a"

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "failed"
    assert repo.git.run("ls-files", "-u").stdout


@pytest.mark.parametrize("mutation", ["file", "index"])
def test_interactive_review_rejects_concurrent_edits(repo, mocker, mutation):
    def read(prompt):
        if mutation == "file":
            repo.file.write_text("changed during review\n")
        else:
            repo.git.run("add", "file.txt")
        return "a"

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "failed"
    assert "changed during review" in audit.view["error"]
    assert repo.git.rebase_active()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_interactive_without_conflict_never_prompts(repo, mocker, operation):
    prompt = mocker.patch("builtins.input", side_effect=AssertionError("Unexpected prompt"))
    audit = Engine(repo.root, repo.config, repo.resolver).run("HEAD", operation, interactive=True)
    assert audit.view["status"] == "succeeded"
    assert not repo.resolver.calls
    prompt.assert_not_called()


def test_interactive_acceptance_does_not_bypass_confidence(repo, mocker):
    repo.resolver.confidence = 0.01
    mocker.patch("builtins.input", return_value="a")
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "failed"
    assert repo.git.run("ls-files", "-u").stdout
    assert audit.view["reviews"][0]["decision"] == "accept"


def test_interactive_multiple_rebase_stops(repo, mocker):
    repo.file.write_text("source second\n")
    repo.git.run("commit", "-am", "second change")
    mocker.patch("builtins.input", side_effect=["r", "revise first", "a", "a"])
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    assert [r["round"] for r in audit.view["reviews"]] == [1, 1, 2]


@pytest.mark.parametrize(
    ("operation", "previous_stop"), [("merge", False), ("rebase", False), ("rebase", True)]
)
@pytest.mark.parametrize("ending", ["stop", "eof", "interrupt", "resolver_error"])
def test_interactive_failed_revision_links_latest_session(
    repo, mocker, operation, ending, previous_stop
):
    if previous_stop:
        repo.file.write_text("source second\n")
        repo.git.run("commit", "-am", "second change")
    original = repo.resolver.run
    indices = []
    heads = []
    failing_round = 2 if previous_stop else 1

    def resolve(git, audit, number, paths, check):
        review = audit.view["reviews"][-1]
        directory = audit.directory / "sessions" / f"{number:04d}"
        audit.create_directory(directory)
        audit.add_record(
            "sessions",
            {
                "round": number,
                "artifacts": f"sessions/{number:04d}",
                "tool_calls": 0,
                "runtime": "deterministic test resolver",
                "status": "failed",
                "rationale": f"attempt {number}",
                "confidence": 0.95,
            },
        )
        audit.write_json(f"sessions/{number:04d}/messages.json", [f"attempt {number}"])
        if review["round"] == failing_round:
            indices.append(git.run("ls-files", "--stage", "-z").stdout)
            heads.append(git.text("rev-parse", "HEAD"))
        result = original(git, audit, number, paths, check)
        if review["feedback"] and ending == "resolver_error":
            raise EzError("Revision resolver failed")
        return result

    repo.resolver.run = resolve
    answers = iter((["a"] if previous_stop else []) + ["r", "revise this proposal", "s"])

    def read(prompt):
        if len(indices) == 2:
            if ending == "eof":
                raise EOFError
            if ending == "interrupt":
                raise KeyboardInterrupt
        return next(answers)

    mocker.patch("builtins.input", side_effect=read)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", operation, interactive=True)
    assert audit.view["status"] == ("interrupted" if ending == "interrupt" else "failed")
    latest = 3 if previous_stop else 2
    record = audit.view["conflicts"][-1]
    assert record["artifacts"]["session"] == f"sessions/{latest:04d}/messages.json"
    assert record["resolution"]["rationale"] == f"attempt {latest}"
    assert record["status"] == ("interrupted" if ending == "interrupt" else "failed")
    if previous_stop:
        assert audit.view["conflicts"][0]["artifacts"]["session"] == "sessions/0001/messages.json"
    assert len(audit.view["reviews"]) == latest
    for review in audit.view["reviews"]:
        assert (audit.directory / review["artifacts"] / "files.json").is_file()
        assert (audit.directory / review["artifacts"] / "worktree.diff").is_file()
    assert indices[0] == indices[1] == repo.git.run("ls-files", "--stage", "-z").stdout
    assert heads[0] == heads[1] == repo.git.text("rev-parse", "HEAD")
    assert repo.git.run("ls-files", "-u").stdout
    if operation == "rebase":
        assert repo.git.rebase_active()
    else:
        assert repo.git.text("rev-parse", "MERGE_HEAD") == repo.parent_oid
    assert "<<<<<<< HEAD" in repo.file.read_text()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_interactive_rerere_autoupdate_never_prompts(repo, mocker, operation):
    repo.git.run("config", "rerere.enabled", "true")
    first = integrate(repo, operation, rerere_autoupdate=True)
    assert first.view["status"] == "succeeded"
    repo.git.run("reset", "--hard", repo.source_oid)
    repo.resolver.calls.clear()
    mocker.patch("builtins.input", side_effect=AssertionError("Unexpected prompt"))
    second = Engine(repo.root, repo.config, repo.resolver).run(
        "main", operation, rerere_autoupdate=True, interactive=True
    )
    assert second.view["status"] == "succeeded", second.view
    assert not repo.resolver.calls


def test_requested_validation_remains_not_run_when_resolution_fails(repo):
    repo.resolver = FakeResolver(error="original resolver failure")
    audit = integrate(repo, validation=("unused-validation",))
    assert audit.view["error"] == "original resolver failure"
    assert audit.view["validation"] == {"command": ["unused-validation"], "status": "not_run"}
    assert repo.git.operation_active()


def test_missing_validation_executable_records_failed_validation_and_unavailable_output(repo):
    audit = integrate(repo, validation=("git-ez-nonexistent-test-validator",))
    assert audit.view["status"] == "failed"
    assert audit.view["integrated"]
    assert audit.view["validation"]["status"] == "failed"
    assert "returncode" not in audit.view["validation"]
    assert audit.view["failure"]["type"] == "FileNotFoundError"
    assert not (audit.directory / "validation.stdout").exists()
    assert not (audit.directory / "validation.stderr").exists()
    assert not repo.git.operation_active()


def test_validation_not_started_if_its_initial_audit_save_fails(repo, mocker):
    save = Audit.save

    def failing_save(self):
        if self.view.get("validation", {}).get("status") == "running":
            raise OSError("validation metadata unavailable")
        save(self)

    mocker.patch.object(Audit, "save", failing_save)
    marker = repo.root / "validation-started"
    audit = integrate(
        repo,
        validation=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('validation-started').touch()",
        ),
    )
    assert audit.view["status"] == "failed"
    assert audit.view["failure"]["type"] == "OSError"
    assert audit.view["failure"]["phase"] == "validation"
    assert audit.view["validation"]["status"] == "not_run"
    assert not marker.exists()
    assert audit.view["integrated"]
    assert (
        json.loads((audit.directory / "run.json").read_text())["validation"]["status"] == "not_run"
    )


# A real owner process pauses at observable lifecycle boundaries. The second run
# must fail before it can consume the first run's conflicts or create an audit.
_LOCK_OWNER = """
import sys
from pathlib import Path
from unittest.mock import patch
from git_ez.audit import Audit
from git_ez.config import Config
from git_ez.engine import Engine
from tests.fakes import FakeResolver

root, operation, phase = sys.argv[1:]
paused = False
def pause():
    global paused
    if not paused:
        paused = True
        print('ready', flush=True)
        assert sys.stdin.readline() == 'continue\\n'

class Resolver(FakeResolver):
    def run(self, *args):
        if phase == 'resolver':
            pause()
        return super().run(*args)

preflight = Engine._check_repository
validate = Engine.validate
finalize = Audit.finalize
def checked(self, **kwargs):
    if phase == 'preflight':
        pause()
    return preflight(self, **kwargs)
def validated(self, *args):
    pause()
    return validate(self, *args)
def finalized(self):
    if phase == 'finalization':
        pause()
    return finalize(self)
def reviewed(*args, **kwargs):
    pause()
    return 'accept', None

with patch.object(Engine, '_check_repository', checked), \\
     patch.object(Engine, 'validate', validated), \\
     patch.object(Audit, 'finalize', finalized), \\
     patch('git_ez.engine.review_proposal', reviewed):
    audit = Engine(Path(root), Config(model='test'), Resolver()).run(
        'main', operation,
        validation=(sys.executable, '-c', 'pass') if phase == 'validation' else (),
        interactive=phase == 'review',
    )
assert audit.view['status'] == 'succeeded', audit.view
"""


@pytest.fixture
def lock_owner(repo):
    repo.git.run("config", "ez.model", "test")

    @contextmanager
    def start(operation, phase):
        source = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(source), str(source / "src"), environment.get("PYTHONPATH", "")]
        )
        process = subprocess.Popen(
            [sys.executable, "-c", _LOCK_OWNER, str(repo.root), operation, phase],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            assert select.select([process.stdout], [], [], 30)[0], "Owner did not reach barrier"
            assert process.stdout.readline() == "ready\n"
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=30)

    return start


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("phase", ["preflight", "resolver", "review", "validation", "finalization"])
def test_checkout_lock_rejects_another_process_throughout_run(
    repo, lock_owner, operation, phase, capsys
):
    subdirectory = repo.root / "subdirectory"
    subdirectory.mkdir()
    with lock_owner(operation, phase) as owner:
        state = repo.git.run("status", "--porcelain=v1").stdout
        index = repo.git.run("ls-files", "--stage").stdout
        head = repo.git.text("rev-parse", "HEAD")
        audits = {p: p.read_bytes() for p in repo.root.rglob("run.json")}
        contender = Engine(subdirectory, repo.config, repo.resolver)
        with pytest.raises(EzError, match="Another git-ez integration is running"):
            contender.run("main", "merge" if operation == "rebase" else "rebase")
        assert main([operation, "main", "--repo", str(subdirectory)]) == 2
        assert "Another git-ez integration is running" in capsys.readouterr().err
        assert repo.resolver.calls == []
        assert repo.git.run("status", "--porcelain=v1").stdout == state
        assert repo.git.run("ls-files", "--stage").stdout == index
        assert repo.git.text("rev-parse", "HEAD") == head
        assert {p: p.read_bytes() for p in repo.root.rglob("run.json")} == audits
        stdout, stderr = owner.communicate("continue\n", timeout=30)
        assert owner.returncode == 0, (stdout, stderr)
    # The persistent file is reusable after the owner finishes its audit.
    lock = Path(repo.git.text("rev-parse", "--absolute-git-dir")) / "ez-execution.lock"
    assert lock.is_file()
    with checkout_lock(repo.git):
        pass
    assert repo.git.run("status", "--porcelain").stdout == b""


def test_checkout_lock_is_released_after_killing_owner_without_rollback(repo, lock_owner):
    with lock_owner("merge", "resolver") as owner:
        assert repo.git.conflicts()
        owner.kill()
        owner.communicate(timeout=30)
        with checkout_lock(repo.git):
            pass
        # Process death releases the lock, but preserves native Git recovery state.
        assert repo.git.operation_active()
        with pytest.raises(EzError, match="operation"):
            integrate(repo, "merge")
        repo.git.run("merge", "--abort")
        assert integrate(repo, "merge").view["status"] == "succeeded"


def test_checkout_lock_allows_independent_linked_worktree(repo, tmp_path, lock_owner):
    linked = tmp_path / "linked"
    repo.git.run("worktree", "add", "-b", "other", str(linked), "feature")
    other = Git(linked)
    assert repo.git.text("rev-parse", "--path-format=absolute", "--git-common-dir") == other.text(
        "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    with lock_owner("merge", "resolver") as owner:
        audit = Engine(linked, repo.config, FakeResolver()).run("main", "merge")
        assert audit.view["status"] == "succeeded"
        assert repo.git.branch_oid("feature") == repo.source_oid
        with pytest.raises(EzError, match="Another git-ez integration"):
            integrate(repo, "merge")
        stdout, stderr = owner.communicate("continue\n", timeout=30)
        assert owner.returncode == 0, (stdout, stderr)
    assert integrate(repo, "merge").view["status"] == "succeeded"


@pytest.mark.parametrize(
    "error", [EzError("stopped"), KeyboardInterrupt(), RuntimeError(), SystemExit()]
)
def test_checkout_lock_is_released_after_failed_or_interrupted_run(repo, mocker, error):
    mocker.patch.object(repo.resolver, "run", side_effect=error)
    if isinstance(error, (EzError, KeyboardInterrupt)):
        audit = integrate(repo, "merge")
        assert audit.view["status"] == (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        )
    else:
        with pytest.raises(type(error)):
            integrate(repo, "merge")
    with checkout_lock(repo.git):
        pass
    assert repo.git.operation_active()


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_checkout_lock_rejects_unsafe_lock_file_before_changes(repo, kind):
    lock = Path(repo.git.text("rev-parse", "--absolute-git-dir")) / "ez-execution.lock"
    if kind == "symlink":
        lock.symlink_to(repo.file)
    elif kind == "directory":
        lock.mkdir()
    else:
        os.mkfifo(lock)
    with pytest.raises(EzError, match="execution lock"):
        integrate(repo, "merge")
    assert repo.file.read_text() == "source\n"
    assert not (repo.root / ".git" / "ez").exists()
    assert repo.resolver.calls == []
    assert repo.git.run("status", "--porcelain").stdout == b""


def test_checkout_lock_is_released_when_audit_finalization_raises(repo, mocker):
    mocker.patch.object(Audit, "finalize", side_effect=OSError("disk failure"))
    with pytest.raises(OSError, match="disk failure"):
        integrate(repo, "merge")
    with checkout_lock(repo.git):
        pass
    assert repo.git.text("rev-parse", "HEAD^2") == repo.parent_oid


def test_checkout_lock_reports_acquisition_errors_and_allows_retry(repo):
    error = PermissionError("locking denied")
    with (
        patch("git_ez.locking.fcntl.flock", side_effect=error),
        pytest.raises(EzError, match="Cannot acquire checkout execution lock") as raised,
    ):
        integrate(repo, "merge")
    assert raised.value.__cause__ is error
    assert not (repo.root / ".git" / "ez").exists()
    assert integrate(repo, "merge").view["status"] == "succeeded"


def test_executed_child_does_not_keep_checkout_locked(repo):
    # Even explicitly preserving inheritable descriptors cannot retain this lock.
    with checkout_lock(repo.git):
        child = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); input()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=False,
        )
        try:
            assert select.select([child.stdout], [], [], 30)[0]
            assert child.stdout.readline() == "ready\n"
        except BaseException:
            child.kill()
            child.communicate(timeout=30)
            raise
    try:
        with checkout_lock(repo.git):
            assert child.poll() is None
    finally:
        child.communicate("exit\n", timeout=30)


def conflict_fingerprint(repo):
    """Observe native state without treating an object ID alone as restoration proof."""
    metadata = Path(repo.git.text("rev-parse", "--absolute-git-dir"))
    control = {}
    for name in ("rebase-merge", "MERGE_HEAD", "MERGE_MSG", "MERGE_AUTOSTASH", "REBASE_HEAD"):
        path = metadata / name
        if path.is_dir():
            control[name] = {
                str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()
            }
        elif path.exists():
            control[name] = path.read_bytes()
    paths = set(repo.git.run("ls-files", "-z").stdout.decode().rstrip("\0").split("\0"))
    return (
        repo.git.run("status", "--porcelain=v2", "--untracked-files=no").stdout,
        repo.git.run("ls-files", "--stage", "-z").stdout,
        {
            name: ((repo.root / name).read_bytes(), (repo.root / name).stat().st_mode)
            for name in paths
            if (repo.root / name).is_file()
        },
        control,
    )


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("kind", ["normal", "delete", "rename", "multiple"])
def test_interactive_quit_restores_native_conflict_and_can_continue(repo, mocker, operation, kind):
    options = {}
    target = "main"
    if kind == "delete":
        repo.git.run("checkout", "main")
        repo.git.run("rm", "file.txt")
        repo.git.run("commit", "-m", "remove parent file")
        repo.git.run("checkout", "feature")
    elif kind == "rename":
        repo.git.run("mv", "file.txt", "renamed.txt")
        if operation == "merge":
            (repo.root / "renamed.txt").write_text("base\n")
        repo.git.run("commit", "-am", "rename source file")
        repo.git.run("checkout", "main")
        repo.git.run("rm", "file.txt")
        repo.git.run("commit", "-m", "delete renamed file on parent")
        repo.git.run("checkout", "feature")
        if operation == "rebase":
            target = repo.source_oid
            options["onto"] = "main"
    elif kind == "multiple":
        (repo.root / "second.txt").write_text("source second\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "source second file")
        repo.git.run("checkout", "main")
        (repo.root / "second.txt").write_text("parent second\n")
        repo.git.run("add", ".")
        repo.git.run("commit", "-m", "parent second file")
        repo.git.run("checkout", "feature")
    baseline = []
    original = repo.resolver.run

    def resolve(git, audit, number, paths, check):
        baseline.append(conflict_fingerprint(repo))
        result = original(git, audit, number, paths, check)
        (repo.root / "temporary").mkdir(exist_ok=True)
        (repo.root / "temporary" / "artifact").write_text("generated")
        return result

    repo.resolver.run = resolve
    mocker.patch("builtins.input", return_value="q")
    audit = integrate(repo, operation, target=target, interactive=True, **options)
    assert audit.view["status"] == "failed", audit.view
    assert conflict_fingerprint(repo) == baseline[0]
    assert (repo.root / "temporary/artifact").read_text() == "generated"
    if kind == "rename":
        assert "renamed.txt" in repo.git.conflicts()
    assert audit.view["reviews"][-1]["restoration"] == "original_conflict_restored"
    assert audit.view["conflicts"][-1]["application"] == "unmerged"
    for name in repo.git.conflicts():
        (repo.root / name).write_text("manual resolution\n")
        repo.git.run("add", "--", name)
    if operation == "rebase":
        # A later rename/add commit can conflict too: resolve it natively as needed.
        result = repo.git.run("rebase", "--continue", check=False)
        while result.returncode and repo.git.conflicts():
            for name in repo.git.conflicts():
                (repo.root / name).write_text("manual resolution\n")
                repo.git.run("add", "--", name)
            result = repo.git.run("rebase", "--continue", check=False)
        assert result.returncode == 0, result.stderr
    else:
        repo.git.run("commit", "--no-edit")
    assert not repo.git.operation_active()


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("eventual_success", [True, False])
def test_interactive_check_correction_and_fresh_feedback_budget(
    repo, mocker, capsys, operation, eventual_success
):
    from dataclasses import replace

    repo.config = replace(repo.config, validation_attempts=2, api_key="synthetic-validation-secret")
    original = repo.resolver.run
    baselines = []
    feedback = []

    def resolve(git, audit, number, paths, check):
        baselines.append(conflict_fingerprint(repo))
        feedback.append(audit.view["reviews"][-1]["feedback"])
        result = original(git, audit, number, paths, check)
        repo.file.write_text("good\n" if eventual_success and number == 2 else "bad\n")
        return result

    repo.resolver.run = resolve
    # The check creates, deletes and modifies files outside the conflict set.
    (repo.root / ".git" / "info" / "exclude").write_text("ignored*\n")
    (repo.root / "ignored-existing").write_text("keep me")
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "Path('ignored-existing').unlink(missing_ok=True); "
        "Path('ignored-new').write_text('generated'); "
        "print('validation diagnostic synthetic-validation-secret'); "
        "raise SystemExit(0 if Path('file.txt').read_text() == 'good\\n' else 1)",
    )
    answers = iter(["a"] if eventual_success else ["a", "r", "preserve compatibility", "q"])
    prompt_count = []

    def read(prompt):
        prompt_count.append(prompt)
        return next(answers)

    mocker.patch("builtins.input", side_effect=read)
    audit = integrate(repo, operation, interactive=True, validation=command)
    output = capsys.readouterr().out
    assert all(baseline == baselines[0] for baseline in baselines)
    assert "synthetic-validation-secret" not in str(feedback)
    assert "synthetic-validation-secret" not in output
    assert not (repo.root / "ignored-existing").exists()
    assert (repo.root / "ignored-new").read_text() == "generated"
    if eventual_success:
        assert audit.view["status"] == "succeeded", audit.view
        assert audit.view["reviews"][-1]["decision"] == "accept"
        assert len(baselines) == 2
        assert "ignored-new" not in output
    else:
        assert audit.view["status"] == "failed"
        assert len(baselines) == 4
        assert "preserve compatibility" in feedback
        assert "cannot be accepted" in output
        assert "validation diagnostic" in output
        assert conflict_fingerprint(repo) == baselines[0]
        assert len(prompt_count) == 4


@pytest.mark.parametrize("phase", ["resolver", "check"])
def test_interactive_sigint_waits_for_mutating_child_and_restores(repo, mocker, phase):
    from git_ez.processes import run_process

    original = repo.resolver.run
    baseline = []
    command = (
        sys.executable,
        "-c",
        "import os, signal, time; from pathlib import Path; "
        "Path('file.txt').write_text('child mutation'); "
        "os.kill(os.getppid(), signal.SIGINT); time.sleep(1); "
        "Path('late-artifact').write_text('unsafe')",
    )

    def resolve(git, audit, number, paths, check):
        baseline.append(conflict_fingerprint(repo))
        result = original(git, audit, number, paths, check)
        if phase == "resolver":
            run_process(command, cwd=repo.root, env=dict(os.environ), timeout=5)
        return result

    repo.resolver.run = resolve
    prompt = mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, interactive=True, validation=command if phase == "check" else ())
    assert audit.view["status"] == "interrupted", audit.view
    assert conflict_fingerprint(repo) == baseline[0]
    prompt.assert_not_called()


def test_interactive_restoration_failure_is_reported_and_backup_retained(repo, mocker):
    import shutil

    from git_ez.conflict_state import ConflictState

    original = ConflictState.restore
    backup = []

    def restore(state):
        if backup:
            mocker.patch("git_ez.conflict_state.reconcile", side_effect=OSError("disk failure"))
        backup.append(state.directory)
        return original(state)

    mocker.patch.object(ConflictState, "restore", restore)
    mocker.patch("builtins.input", return_value="q")
    audit = integrate(repo, interactive=True)
    assert audit.view["status"] == "failed"
    assert "Conflict restoration failed" in audit.view["error"]
    assert (backup[0] / "index").exists()
    shutil.rmtree(backup[0])


def test_interactive_global_merge_approval_covers_all_files(repo, mocker, capsys):
    (repo.root / "second.txt").write_text("source addition\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "source addition")
    repo.git.run("checkout", "main")
    (repo.root / "second.txt").write_text("parent addition\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "parent addition")
    repo.git.run("checkout", "feature")
    prompt = mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, "merge", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    assert len(audit.view["reviews"]) == 1
    assert len(audit.view["reviews"][0]["conflict_ids"]) == 2
    prompt.assert_called_once()
    output = capsys.readouterr().out
    assert "file.txt" in output and "second.txt" in output


def test_interactive_quit_restores_modes_links_and_ignores_repeated_sigint(repo, mocker):
    import signal

    from git_ez.conflict_state import reconcile

    outside = repo.root.parent / "outside"
    outside.write_text("unrelated")
    link = repo.root / "tracked-link"
    repo.git.run("checkout", "main")
    link.symlink_to(outside)
    repo.git.run("add", "tracked-link")
    repo.git.run("commit", "-m", "add tracked link")
    repo.git.run("checkout", "feature")
    original = repo.resolver.run
    initial = []

    def resolve(git, audit, number, paths, check):
        initial.append(repo.file.read_bytes())
        result = original(git, audit, number, paths, check)
        repo.file.chmod(0o755)
        link.unlink()
        link.write_text("replaced link")
        return result

    def restore(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGINT)
        return reconcile(*args, **kwargs)

    repo.resolver.run = resolve
    mocker.patch("git_ez.conflict_state.reconcile", side_effect=restore)
    mocker.patch("builtins.input", return_value="q")
    audit = integrate(repo, interactive=True)
    assert audit.view["status"] == "failed", audit.view
    assert repo.file.read_bytes() == initial[0]
    assert repo.file.stat().st_mode & 0o777 == 0o644
    assert link.is_symlink()
    assert outside.read_text() == "unrelated"


def test_interactive_rejects_check_that_changes_git_operation_metadata(repo, mocker):
    prompt = mocker.patch("builtins.input", return_value="a")
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "Path('.git/rebase-merge/git-rebase-todo').write_text('invalid command')",
    )
    audit = integrate(repo, interactive=True, validation=command)
    assert audit.view["status"] == "failed"
    assert "Git operation metadata changed" in audit.view["error"]
    assert "invalid command" not in (repo.root / ".git/rebase-merge/git-rebase-todo").read_text()
    assert repo.git.conflicts()
    prompt.assert_not_called()


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_interactive_quit_restores_check_side_effects_on_clean_tracked_files(
    repo, mocker, operation
):
    repo.git.run("checkout", "main")
    (repo.root / "support.txt").write_text("original support\n")
    (repo.root / "removed.txt").write_text("original removed\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "add supporting files")
    repo.git.run("checkout", "feature")
    baseline = []
    original = repo.resolver.run

    def resolve(git, audit, number, paths, check):
        baseline.append(conflict_fingerprint(repo))
        return original(git, audit, number, paths, check)

    repo.resolver.run = resolve
    mocker.patch("builtins.input", return_value="q")
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "Path('support.txt').write_text('modified'); Path('removed.txt').unlink(); "
        "Path('created.txt').write_text('created')",
    )
    audit = integrate(repo, operation, interactive=True, validation=command)
    assert audit.view["status"] == "failed", audit.view
    assert conflict_fingerprint(repo) == baseline[0]
    assert (repo.root / "created.txt").read_text() == "created"


@pytest.mark.parametrize("interactive", [False, True])
def test_cli_interruption_exit_status_preserves_noninteractive_policy(repo, mocker, interactive):
    repo.git.run("config", "ez.model", "test")
    engine = Engine(repo.root, repo.config, repo.resolver)
    mocker.patch("git_ez.cli.Engine", return_value=engine)
    mocker.patch.object(repo.resolver, "run", side_effect=KeyboardInterrupt)
    arguments = ["rebase", "main", "--repo", str(repo.root)]
    if interactive:
        arguments.append("--interactive")
    assert main(arguments) == (130 if interactive else 1)
    assert repo.git.conflicts()
    assert repo.git.rebase_active()


def test_interactive_sigint_during_child_startup_waits_before_restoration(repo, mocker):
    import signal

    from git_ez.processes import run_process

    native_popen = subprocess.Popen
    original = repo.resolver.run
    baseline = []
    children = []

    def spawn(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        os.kill(os.getpid(), signal.SIGINT)
        return child

    def resolve(git, audit, number, paths, check):
        baseline.append(conflict_fingerprint(repo))
        result = original(git, audit, number, paths, check)
        with patch("git_ez.processes.subprocess.Popen", side_effect=spawn):
            run_process(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                cwd=repo.root,
                env=dict(os.environ),
                timeout=30,
            )
        return result

    repo.resolver.run = resolve
    audit = integrate(repo, interactive=True)
    assert audit.view["status"] == "interrupted", audit.view
    assert children[0].poll() is not None
    assert conflict_fingerprint(repo) == baseline[0]


def test_interactive_uncertain_child_cleanup_retains_backup_without_false_restoration(repo):
    import shutil

    from git_ez.processes import ChildCleanupError

    original = repo.resolver.run

    def resolve(git, audit, number, paths, check):
        original(git, audit, number, paths, check)
        raise ChildCleanupError("Cannot confirm child process group termination")

    repo.resolver.run = resolve
    audit = integrate(repo, interactive=True)
    assert audit.view["status"] == "failed"
    error = audit.view["error"]
    assert "conflict was not restored" in error
    backup = Path(error.split("Backup retained at ", 1)[1].split(":", 1)[0])
    try:
        assert (backup / "index").exists()
        assert "<<<<<<< HEAD" in (backup / "worktree/file.txt").read_text()
        assert repo.file.read_text() == "combined\n"
        assert repo.git.conflicts()
        assert "restoration" not in audit.view["reviews"][-1]
    finally:
        shutil.rmtree(backup)


def test_interactive_quit_after_multiple_rejections_restores_one_original_baseline(repo, mocker):
    original = repo.resolver.run
    baselines = []

    def resolve(git, audit, number, paths, check):
        baselines.append(conflict_fingerprint(repo))
        result = original(git, audit, number, paths, check)
        (repo.root / f"attempt-{number}").write_text("provisional artifact")
        return result

    repo.resolver.run = resolve
    mocker.patch("builtins.input", side_effect=["r", "first feedback", "r", "second feedback", "q"])
    audit = integrate(repo, interactive=True)
    assert audit.view["status"] == "failed"
    assert len(baselines) == len(audit.view["reviews"]) == 3
    assert all((repo.root / f"attempt-{number}").exists() for number in (1, 2, 3))
    assert all(before == baselines[0] for before in baselines)
    assert conflict_fingerprint(repo) == baselines[0]
    assert [item["decision"] for item in audit.view["reviews"]] == ["revise", "revise", "stop"]
    assert all(
        item["restoration"] == "original_conflict_restored" for item in audit.view["reviews"]
    )


@pytest.mark.parametrize("dirty", [False, True])
def test_conflict_snapshot_reuses_git_blobs_and_never_copies_nontracked_trees(repo, dirty):
    from git_ez.conflict_state import ConflictState

    support = repo.root / "support.bin"
    original = b"\0tracked binary\n" * 65536
    repo.git.run("checkout", "main")
    support.write_bytes(original)
    repo.git.run("add", "support.bin")
    repo.git.run("commit", "-m", "add binary support")
    repo.git.run("checkout", "feature")
    assert repo.git.run("rebase", "main", check=False).returncode
    if dirty:
        original = b"working contents differ from index\n"
        support.write_bytes(original)
    ignored = repo.root / ".venv"
    ignored.mkdir()
    (ignored / ".git").mkdir()
    os.mkfifo(ignored / "fifo")
    (repo.root / ".git/info/exclude").write_text(".venv/\n")
    notes = repo.root / "notes"
    notes.write_text("before")
    generated = ignored / "generated"
    generated.write_text("before")
    index = Path(repo.git.text("rev-parse", "--path-format=absolute", "--git-path", "index"))
    original_index = index.read_bytes()
    marked = repo.file.read_bytes()
    state = ConflictState(repo.git)
    try:
        assert not (state.directory / "worktree/.venv").exists()
        assert not (state.directory / "worktree/notes").exists()
        assert (state.directory / "worktree/support.bin").exists() == dirty
        assert (state.directory / "worktree/file.txt").read_bytes() == marked
        support.write_bytes(b"changed")
        repo.file.write_text("proposal\n")
        notes.write_text("after")
        generated.write_text("after")
        (repo.root / "new-nontracked").write_text("created")
        state.restore()
        assert index.read_bytes() == original_index
        assert support.read_bytes() == original
        assert repo.file.read_bytes() == marked
        assert notes.read_text() == generated.read_text() == "after"
        assert (repo.root / "new-nontracked").read_text() == "created"
        outside = repo.root.parent / "outside"
        outside.write_text("external contents")
        support.unlink()
        support.symlink_to(outside)
        state.restore()
        assert not support.is_symlink()
        assert support.read_bytes() == original
        assert outside.read_text() == "external contents"
    finally:
        state.close()


def test_conflict_restore_preserves_nontracked_data_blocking_a_tracked_path(repo):
    from git_ez.conflict_state import ConflictState

    assert repo.git.run("rebase", "main", check=False).returncode
    state = ConflictState(repo.git)
    try:
        repo.file.unlink()
        repo.file.mkdir()
        notes = repo.file / "nontracked-notes"
        notes.write_text("keep me")
        with pytest.raises(EzError, match="Conflict restoration failed"):
            state.restore()
        assert state.retained
        assert notes.read_text() == "keep me"
        assert (state.directory / "index").exists()
    finally:
        state.close()


def test_interactive_check_cleanup_failure_never_retries_or_restores_with_live_child(repo, mocker):
    import shutil

    from git_ez.conflict_state import ConflictState
    from git_ez.processes import ChildCleanupError

    children = []
    restored_while_alive = []
    restore = ConflictState.restore

    def runner(command, **kwargs):
        child = subprocess.Popen(
            (sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read(1)"),
            cwd=repo.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        children.append(child)
        assert child.stdout.readline() == b"ready\n"
        raise ChildCleanupError("Child group remains active")

    def observe_restore(state):
        if children and children[0].poll() is None:
            restored_while_alive.append(True)
        return restore(state)

    resolver = mocker.spy(repo.resolver, "run")
    check = mocker.patch("git_ez.engine.run_process", side_effect=runner)
    prompt = mocker.patch("builtins.input", return_value="a")
    mocker.patch.object(ConflictState, "restore", observe_restore)
    backup = None
    try:
        audit = integrate(repo, interactive=True, validation=("check",))
        assert audit.view["status"] == "failed", audit.view
        assert "conflict was not restored" in audit.view["error"]
        backup = Path(audit.view["error"].split("Backup retained at ", 1)[1].split(":", 1)[0])
        assert (backup / "index").exists()
        assert "restoration" not in audit.view["reviews"][-1]
        assert audit.view["validation"]["status"] == "failed"
        assert not restored_while_alive
        assert len(children) == 1 and children[0].poll() is None
        assert repo.file.read_text() == "combined\n"
        assert repo.git.conflicts()
        resolver.assert_called_once()
        check.assert_called_once()
        prompt.assert_not_called()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        if backup is not None:
            shutil.rmtree(backup)


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree", "both"])
@pytest.mark.parametrize("action", ["write", "delete"])
def test_interactive_stages_every_approved_path_despite_preexisting_index_hints(
    repo, mocker, operation, flag, action
):
    support = repo.root / "support.txt"
    for branch in ("main", "feature"):
        repo.git.run("checkout", branch)
        support.write_text("original\n")
        repo.git.run("add", "support.txt")
        repo.git.run("commit", "-m", f"add support on {branch}")
    for hint in ("assume-unchanged", "skip-worktree") if flag == "both" else (flag,):
        repo.git.run("update-index", f"--{hint}", "support.txt")
    original = repo.resolver.run
    initial_flags = []

    def resolve(git, audit, number, paths, check):
        initial_flags.append(git.text("ls-files", "-v", "--", "support.txt"))
        result = original(git, audit, number, paths, check)
        if action == "write":
            support.write_text("approved supporting resolution\n")
        else:
            support.unlink()
        return result

    repo.resolver.run = resolve
    hints_at_continuation = []
    run = Git.run

    def observe(git, *args, **kwargs):
        if args == (operation, "--continue"):
            hints_at_continuation.append(git.text("ls-files", "-v", "--", "support.txt"))
        return run(git, *args, **kwargs)

    mocker.patch.object(Git, "run", observe)
    prompt = mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, operation, interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    expected_flag = {"assume-unchanged": "h", "skip-worktree": "S", "both": "s"}[flag]
    assert initial_flags == [f"{expected_flag} support.txt"]
    assert hints_at_continuation == (
        [f"{expected_flag} support.txt"] if action == "write" else [""]
    )
    prompt.assert_called_once()
    if action == "write":
        assert repo.git.text("show", "HEAD:support.txt") == "approved supporting resolution"
        assert support.read_text() == "approved supporting resolution\n"
    else:
        assert not repo.git.run("ls-tree", "HEAD", "--", "support.txt").stdout
        assert not support.exists()


def test_interactive_detects_incomplete_staging_before_git_continues(repo, mocker):
    original = repo.resolver.run

    def resolve(git, audit, number, paths, check):
        result = original(git, audit, number, paths, check)
        (repo.root / "support.txt").write_text("approved support\n")
        return result

    repo.resolver.run = resolve
    run = Git.run
    continued = []

    def omit_support(git, *args, **kwargs):
        if args[:4] == ("add", "-A", "--", "file.txt"):
            return run(git, "add", "-A", "--", "file.txt", **kwargs)
        if args == ("rebase", "--continue"):
            continued.append(True)
        return run(git, *args, **kwargs)

    mocker.patch.object(Git, "run", omit_support)
    mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, interactive=True)
    assert audit.view["status"] == "failed", audit.view
    assert "Approved content was not staged: support.txt" in audit.view["error"]
    assert not continued
    assert repo.git.conflicts()
    assert "<<<<<<< HEAD" in repo.file.read_text()
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert audit.view["reviews"][-1]["restoration"] == "original_conflict_restored"


def test_interactive_staging_keeps_native_clean_filter_semantics(repo, mocker):
    repo.git.run("config", "filter.uppercase.clean", "tr a-z A-Z")
    for branch in ("main", "feature"):
        repo.git.run("checkout", branch)
        (repo.root / ".gitattributes").write_text("file.txt filter=uppercase\n")
        repo.git.run("add", ".gitattributes", "file.txt")
        repo.git.run("commit", "-m", f"normalize content on {branch}")
    mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, "merge", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    assert repo.git.text("show", "HEAD:file.txt") == "COMBINED"
    assert repo.file.read_text() == "combined\n"


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("kind", ["unstaged", "staged", "untracked"])
def test_autostash_configuration_never_bypasses_clean_checkout_precondition(
    repo, mocker, operation, interactive, kind
):
    repo.git.run("config", f"{operation}.autoStash", "true")
    if kind == "untracked":
        (repo.root / "user-notes").write_text("private notes\n")
    else:
        repo.file.write_text("local changes\n")
        if kind == "staged":
            repo.git.run("add", "file.txt")
    before = repo.git.run("status", "--porcelain=v2", "--untracked-files=all").stdout
    index = repo.git.run("ls-files", "--stage", "-z").stdout
    prompt = mocker.patch("builtins.input")
    with pytest.raises(EzError, match="Repository must be clean"):
        integrate(repo, operation, interactive=interactive)
    assert repo.git.run("status", "--porcelain=v2", "--untracked-files=all").stdout == before
    assert repo.git.run("ls-files", "--stage", "-z").stdout == index
    assert repo.git.branch_oid("feature") == repo.source_oid
    assert not repo.git.operation_active()
    assert not repo.git.run("stash", "list").stdout
    assert not (repo.root / ".git/ez/runs").exists()
    assert repo.resolver.calls == []
    prompt.assert_not_called()
    if kind == "untracked":
        assert (repo.root / "user-notes").read_text() == "private notes\n"
    else:
        assert repo.file.read_text() == "local changes\n"


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("action", ["accept", "quit", "automatic"])
def test_native_git_autostash_is_explicitly_disabled_without_changing_configuration(
    repo, mocker, operation, action
):
    repo.git.run("config", f"{operation}.autoStash", "true")
    run = Git.run
    initial = []

    def observe(git, *args, **kwargs):
        if args[0] == operation and "--continue" not in args:
            initial.append(args)
        return run(git, *args, **kwargs)

    mocker.patch.object(Git, "run", observe)
    mocker.patch("builtins.input", return_value="q" if action == "quit" else "a")
    audit = integrate(repo, operation, interactive=action != "automatic")
    assert audit.view["status"] == ("failed" if action == "quit" else "succeeded"), audit.view
    assert len(initial) == 1 and "--no-autostash" in initial[0]
    assert repo.git.text("config", "--bool", "--get", f"{operation}.autoStash") == "true"
    assert not repo.git.run("stash", "list").stdout
    marker = "rebase-merge/autostash" if operation == "rebase" else "MERGE_AUTOSTASH"
    assert not Path(
        repo.git.text("rev-parse", "--path-format=absolute", "--git-path", marker)
    ).exists()


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("interactive", [False, True])
def test_autostash_configuration_cannot_hide_final_validation_side_effects(
    repo, mocker, operation, interactive
):
    repo.git.run("config", f"{operation}.autoStash", "true")
    (repo.root / ".git/info/exclude").write_text("check-counter\n")
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('check-counter'); "
        "n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); "
        "Path('file.txt').write_text('checked-'+str(n)+'\\n')",
    )
    prompt = mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, operation, interactive=interactive, validation=command)
    assert audit.view["status"] == "failed", audit.view
    assert audit.view["integrated"]
    assert "Repository must be clean" in audit.view["error"]
    assert repo.git.text("show", "HEAD:file.txt") == ("checked-1" if interactive else "combined")
    assert repo.file.read_text() == ("checked-2\n" if interactive else "checked-1\n")
    assert not repo.git.operation_active()
    if interactive:
        prompt.assert_called_once()
    else:
        prompt.assert_not_called()


def test_split_index_snapshot_survives_native_shared_index_replacement(repo):
    from git_ez.conflict_state import ConflictState

    repo.git.run("checkout", "main")
    support = repo.root / "support.txt"
    support.write_text("support\n")
    repo.git.run("add", "support.txt")
    repo.git.run("commit", "-m", "add support")
    repo.git.run("checkout", "feature")
    repo.git.run("config", "core.splitIndex", "true")
    repo.git.run("config", "splitIndex.sharedIndexExpire", "now")
    repo.git.run("config", "splitIndex.maxPercentChange", "0")
    assert repo.git.run("rebase", "main", check=False).returncode
    repo.git.run("update-index", "--split-index")
    initial_shared = repo.git.text("rev-parse", "--shared-index-path")
    baseline = conflict_fingerprint(repo)
    initial_stages = repo.git.run("ls-files", "--stage", "-z").stdout
    state = ConflictState(repo.git)
    try:
        repo.file.write_text("proposed\n")
        info = support.stat()
        os.utime(support, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
        repo.git.run("update-index", "--refresh", "--", "support.txt", check=False)
        repo.git.run("update-index", "--split-index")
        current_shared = repo.git.text("rev-parse", "--shared-index-path")
        original_path = Path(initial_shared)
        if not original_path.is_absolute():
            original_path = repo.root / original_path
        assert current_shared != initial_shared
        assert not original_path.exists()
        assert repo.git.run("ls-files", "--stage", "-z").stdout == initial_stages
        state.restore()
        restored = repo.git.run("ls-files", "--stage", "-z", check=False)
        assert restored.returncode == 0
        assert restored.stdout == initial_stages
        assert conflict_fingerprint(repo) == baseline
    finally:
        state.close()


def test_quit_keeps_native_split_index_readable_after_check_refresh(repo, mocker):
    support = repo.root / "support.txt"
    repo.git.run("checkout", "main")
    support.write_text("support\n")
    repo.git.run("add", "support.txt")
    repo.git.run("commit", "-m", "add support")
    repo.git.run("checkout", "feature")
    repo.git.run("config", "core.splitIndex", "true")
    repo.git.run("config", "splitIndex.sharedIndexExpire", "now")
    repo.git.run("config", "splitIndex.maxPercentChange", "0")
    baselines = []
    original = repo.resolver.run

    def resolve(git, audit, number, paths, check):
        baselines.append(conflict_fingerprint(repo))
        return original(git, audit, number, paths, check)

    repo.resolver.run = resolve
    command = (
        sys.executable,
        "-c",
        "import os, subprocess; from pathlib import Path; p=Path('support.txt'); "
        "s=p.stat(); os.utime(p, ns=(s.st_atime_ns,s.st_mtime_ns+5000000000)); "
        "subprocess.run(['git','update-index','--refresh','--','support.txt'], "
        "capture_output=True); "
        "subprocess.run(['git','update-index','--split-index'],check=True)",
    )
    mocker.patch("builtins.input", return_value="q")
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", interactive=True, validation=command
    )
    result = repo.git.run("status", "--porcelain=v2", check=False)
    assert result.returncode == 0
    assert audit.view["reviews"][-1]["restoration"] == "original_conflict_restored"
    assert conflict_fingerprint(repo) == baselines[0]
    repo.file.write_text("manual resolution\n")
    repo.git.run("add", "file.txt")
    repo.git.run("rebase", "--continue")
    assert not (repo.root / ".git/rebase-merge").exists()


@pytest.mark.parametrize("decision", ["interrupt", "q"])
def test_repeated_sigint_at_restore_entry_preserves_the_conflict(repo, mocker, decision):
    import signal

    from git_ez.conflict_state import ConflictState

    previous_handler = signal.getsignal(signal.SIGINT)
    restore = ConflictState.restore
    states = []
    count = 0
    baselines = []

    def interrupted_restore(state):
        nonlocal count
        count += 1
        states.append(state)
        if count == 1:
            baselines.append(conflict_fingerprint(repo))
        if count == 2:
            # Deliver the repeated SIGINT before restore installs its temporary mask.
            os.kill(os.getpid(), signal.SIGINT)
        return restore(state)

    def interrupt_review(prompt):
        if decision == "interrupt":
            os.kill(os.getpid(), signal.SIGINT)
        return "q"

    mocker.patch.object(ConflictState, "restore", interrupted_restore)
    mocker.patch("builtins.input", side_effect=interrupt_review)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", interactive=True)
    assert audit.view["status"] == ("interrupted" if decision == "interrupt" else "failed")
    assert "<<<<<<< HEAD" in repo.file.read_text()
    assert not states[-1].directory.exists()
    assert repo.git.conflicts()

    assert conflict_fingerprint(repo) == baselines[0]
    assert signal.getsignal(signal.SIGINT) == previous_handler


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("file_mode", [False, True])
def test_interactive_checks_approved_executable_mode_before_continuation(
    repo, mocker, operation, file_mode
):
    repo.git.run("config", "core.fileMode", str(file_mode).lower())
    original = repo.resolver.run
    baselines = []

    def resolve(git, audit, number, paths, check):
        baselines.append(conflict_fingerprint(repo))
        result = original(git, audit, number, paths, check)
        repo.file.chmod(0o755)
        return result

    repo.resolver.run = resolve
    prompt = mocker.patch("builtins.input", return_value="a")
    audit = integrate(repo, operation, interactive=True)
    prompt.assert_called_once()
    assert repo.git.text("config", "--bool", "core.fileMode") == str(file_mode).lower()
    if file_mode:
        assert audit.view["status"] == "succeeded", audit.view
        assert repo.git.text("ls-tree", "HEAD", "--", "file.txt").startswith("100755 ")
        assert repo.git.text("show", "HEAD:file.txt") == "combined"
        assert not repo.git.operation_active()
    else:
        assert audit.view["status"] == "failed", audit.view
        assert "Approved executable mode was not staged: file.txt" in audit.view["error"]
        assert "core.fileMode" in audit.view["error"]
        assert repo.git.branch_oid("feature") == repo.source_oid
        assert repo.git.operation_active()
        assert conflict_fingerprint(repo) == baselines[0]
        assert audit.view["reviews"][-1]["restoration"] == "original_conflict_restored"
