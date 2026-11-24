# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Local integrations on real repositories, without a model or network."""

import json
import sys
from pathlib import Path

import pytest

from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.git import Git
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
