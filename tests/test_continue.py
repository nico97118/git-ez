# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Manual continuation and transfer of conflict ownership using real Git."""

import pytest

from git_ez.cli import main, parser
from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.git import Git
from tests.test_engine import repo as integration_repo

repo = integration_repo

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def continuation_editor(monkeypatch):
    monkeypatch.setenv("GIT_EDITOR", "true")


def start(repo, operation="rebase", backend="merge", second=False):
    if second:
        repo.file.write_text("second\n")
        repo.git.run("commit", "-am", "second change")
    args = ("rebase", f"--{backend}", "main") if operation == "rebase" else ("merge", "main")
    assert repo.git.run(*args, check=False).returncode != 0
    assert repo.git.conflicts()


def resume(repo, operation="rebase", **kwargs):
    return Engine(repo.root, repo.config, repo.resolver).continue_operation(operation, **kwargs)


def manual(repo):
    repo.file.write_text("manual\n")
    repo.git.run("add", "file.txt")


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("edit", [False, True])
def test_current_manual_conflict_is_never_assisted_or_staged(repo, operation, edit, capfd):
    start(repo, operation)
    if edit:
        repo.file.write_text("unfinished manual resolution\n")
    before = repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes()
    result = resume(repo, operation)
    assert isinstance(result, int) and result != 0
    assert repo.git.operation_active()
    assert repo.git.conflicts()
    assert before == (repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes())
    assert repo.resolver.calls == []
    output = capfd.readouterr()
    assert "unmerged" in output.err or "resolve" in output.out


@pytest.mark.parametrize("backend", ["merge", "apply"])
@pytest.mark.parametrize("second", [False, True])
def test_native_rebase_continuation_transfers_only_new_conflict(repo, backend, second):
    start(repo, backend=backend, second=second)
    original = repo.git.commit_oid("REBASE_HEAD")
    manual(repo)
    result = resume(repo)
    if second:
        assert not isinstance(result, int)
        assert result.view["status"] == "succeeded", result.view
        assert result.view["integrated"]
        assert len(repo.resolver.calls) == 1
        assert repo.resolver.calls[0].theirs == "second\n"
        assert repo.git.text("show", "HEAD^:file.txt") == "manual"
        assert repo.git.text("rev-parse", "HEAD^^") == repo.parent_oid
        assert repo.file.read_text() == "combined\n"
        assert result.view["conflicts"][0]["status"] == "applied"
    else:
        assert result == 0
        assert repo.resolver.calls == []
        assert repo.file.read_text() == "manual\n"
        assert repo.git.text("rev-parse", "HEAD^") == repo.parent_oid
    assert repo.git.commit_oid("HEAD") != original
    assert repo.git.text("branch", "--show-current") == "feature"
    assert not repo.git.operation_active()
    assert not repo.git.conflicts()
    assert repo.git.run("status", "--porcelain").stdout == b""


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_native_merge_or_rebase_reports_editor_failure_without_assistance(
    repo, operation, monkeypatch, capfd
):
    start(repo, operation)
    manual(repo)
    before = repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes()
    monkeypatch.setenv("GIT_EDITOR", "false")
    result = resume(repo, operation)
    assert isinstance(result, int) and result != 0
    assert "editor" in capfd.readouterr().err
    assert repo.resolver.calls == []
    assert repo.git.operation_active()
    assert before == (repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes())


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_continue_requires_matching_active_operation(repo, operation):
    with pytest.raises(EzError, match=f"No {operation} in progress"):
        resume(repo, operation)
    assert not repo.resolver.calls


def test_merge_continue_keeps_native_parents(repo):
    start(repo, "merge")
    manual(repo)
    assert resume(repo, "merge") == 0
    assert repo.git.text("rev-parse", "HEAD^1") == repo.source_oid
    assert repo.git.text("rev-parse", "HEAD^2") == repo.parent_oid
    assert not repo.git.operation_active()
    assert not repo.resolver.calls


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_continue_in_linked_worktree(repo, operation, tmp_path):
    root = tmp_path / "linked"
    repo.git.run("worktree", "add", "-b", "linked", str(root), "feature")
    repo.root, repo.git, repo.file = root, Git(root), root / "file.txt"
    assert (root / ".git").is_file()
    start(repo, operation)
    manual(repo)
    assert resume(repo, operation) == 0
    assert repo.git.text("branch", "--show-current") == "linked"
    assert not repo.git.operation_active()


@pytest.mark.parametrize("interactive", [False, True])
def test_new_conflict_uses_current_invocation_review_mode(repo, interactive, mocker):
    start(repo, second=True)
    manual(repo)
    review = mocker.patch("git_ez.engine.review_proposal", return_value=("accept", None))
    result = resume(repo, interactive=interactive)
    assert result.view["status"] == "succeeded", result.view
    assert result.view["interactive"] is interactive
    assert review.call_count == int(interactive)
    assert len(repo.resolver.calls) == 1


def test_git_ez_started_operation_can_be_continued(repo):
    repo.resolver.error = "Stopped for manual work"
    stopped = Engine(repo.root, repo.config, repo.resolver).run("main")
    assert stopped.view["status"] == "failed"
    manual(repo)
    assert resume(repo) == 0
    assert repo.file.read_text() == "manual\n"
    assert not repo.resolver.calls


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_cli_preserves_native_failure_status_and_diagnostics(repo, operation, capfd):
    start(repo, operation)
    native = repo.git.run(operation, "--continue", check=False)
    assert main([operation, "--continue", "--repo", str(repo.root)]) == native.returncode
    output = capfd.readouterr()
    assert native.stderr.decode().strip() in output.err
    assert repo.git.conflicts()


@pytest.mark.parametrize(
    "arguments",
    [
        ["rebase", "--continue", "main"],
        ["merge", "--continue", "main"],
        ["rebase", "--continue", "--onto", "main"],
        ["rebase", "--continue", "--no-autosquash"],
        ["rebase", "--continue", "--no-keep-base"],
        ["merge", "--continue", "--no-ff"],
        ["merge", "--continue", "-m", "message"],
    ],
)
def test_continue_rejects_initial_integration_options(arguments, capsys):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(arguments)
    assert exc.value.code == 2
    assert "--continue cannot be combined" in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_interruption_suggests_current_options_only_if_operation_active(
    repo, operation, mocker, capsys
):
    repo.git.run("config", "ez.model", "test")
    mocker.patch("git_ez.engine.AgentResolver", return_value=repo.resolver)
    mocker.patch.object(repo.resolver, "run", side_effect=KeyboardInterrupt)
    result = main([operation, "main", "--repo", str(repo.root), "--interactive"])
    assert result == 130
    output = capsys.readouterr().err
    assert f"git ez {operation} --continue --interactive --repo {repo.root}" in output
    assert f"git {operation} --continue" in output
    assert repo.git.operation_active()


def test_missing_staging_preserves_manual_edits(repo):
    start(repo)
    manual(repo)
    repo.file.write_text("additional unstaged manual edit\n")
    before = repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes()
    result = resume(repo)
    assert isinstance(result, int) and result != 0
    assert before == (repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes())
    assert not repo.resolver.calls


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_cli_native_completion_needs_no_model_configuration(repo, operation):
    start(repo, operation)
    manual(repo)
    assert main([operation, "--continue", "--repo", str(repo.root)]) == 0
    assert not repo.git.operation_active()
    assert not repo.git.conflicts()
    assert repo.file.read_text() == "manual\n"


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_native_hook_failure_is_not_retried(repo, operation, capfd):
    start(repo, operation)
    manual(repo)
    hook = repo.root / ".git" / "hooks" / "prepare-commit-msg"
    hook.write_text("#!/bin/sh\necho 'manual continuation hook refused' >&2\nexit 1\n")
    hook.chmod(0o755)
    before = repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes()
    assert resume(repo, operation) != 0
    assert "manual continuation hook refused" in capfd.readouterr().err
    assert repo.git.operation_active()
    assert not repo.resolver.calls
    assert before == (repo.git.run("ls-files", "--stage", "-z").stdout, repo.file.read_bytes())


def test_ambiguous_initial_position_leaves_real_new_conflict_manual(repo, mocker):
    start(repo, second=True)
    original = repo.git.commit_oid("REBASE_HEAD")
    manual(repo)
    engine = Engine(repo.root, repo.config, repo.resolver)
    position = engine.git.rebase_position
    mocker.patch.object(engine.git, "rebase_position", side_effect=[None, position()])
    assert engine.continue_operation("rebase") != 0
    assert repo.git.rebase_active()
    assert repo.git.conflicts()
    assert repo.git.commit_oid("REBASE_HEAD") != original
    assert repo.git.text("show", "HEAD:file.txt") == "manual"
    assert not repo.resolver.calls


def test_unrelated_exec_failure_after_progress_does_not_trigger_assistance(repo, tmp_path, capfd):
    # Rebase can advance yet stop for an exec error rather than a new conflict.
    editor = tmp_path / "sequence.py"
    editor.write_text(
        "import pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "p.write_text(p.read_text() + 'exec echo unrelated-exec-failure >&2; false\\n')\n"
    )
    import shlex
    import sys

    result = repo.git.run(
        "rebase",
        "--interactive",
        "main",
        check=False,
        sequence_editor=shlex.join([sys.executable, str(editor)]),
    )
    assert result.returncode != 0
    manual(repo)
    assert resume(repo) != 0
    assert "unrelated-exec-failure" in capfd.readouterr().err
    assert not repo.resolver.calls
    assert repo.git.rebase_active()
    assert not repo.git.conflicts()
    assert repo.file.read_text() == "manual\n"


def test_cli_new_conflict_uses_real_agent_and_records_only_new_conflict(repo, model_server, capfd):
    repo.git.run("config", "ez.model", "scripted-model")
    repo.git.run("config", "ez.endpoint", model_server.endpoint)
    start(repo, second=True)
    manual(repo)
    assert main(["rebase", "--continue", "--repo", str(repo.root)]) == 0
    assert model_server.calls
    assert "Status: succeeded" in capfd.readouterr().out
    assert repo.git.text("show", "HEAD^:file.txt") == "manual"
    assert repo.file.read_text() == "combined\n"
    assert not repo.git.operation_active()


def test_interruption_after_completed_git_does_not_suggest_continue(repo, mocker, capsys):
    import sys

    repo.git.run("config", "ez.model", "test")
    mocker.patch("git_ez.engine.AgentResolver", return_value=repo.resolver)
    mocker.patch.object(Engine, "validate", side_effect=KeyboardInterrupt)
    assert (
        main(
            [
                "rebase",
                "main",
                "--repo",
                str(repo.root),
                "--check",
                sys.executable,
                "-c",
                "pass",
            ]
        )
        == 1
    )
    assert not repo.git.operation_active()
    assert "git ez rebase --continue" not in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("returncode", [0, 1])
def test_native_completion_runs_requested_final_validation(repo, operation, returncode, capfd):
    import sys

    repo.git.run("config", "ez.model", "test")
    start(repo, operation)
    manual(repo)
    result = main(
        [
            operation,
            "--continue",
            "--repo",
            str(repo.root),
            "--check",
            sys.executable,
            "-c",
            f"raise SystemExit({returncode})",
        ]
    )
    assert result == returncode
    assert not repo.git.operation_active()
    output = capfd.readouterr().out
    assert "Integrated: True" in output
    assert f"Status: {'succeeded' if returncode == 0 else 'failed'}" in output
    assert repo.file.read_text() == "manual\n"


def test_completed_native_integration_is_retained_when_journal_initialization_fails(
    repo, mocker, capfd
):
    import sys

    from git_ez.audit import Audit
    from git_ez.errors import AuditError

    repo.git.run("config", "ez.model", "test")
    start(repo)
    manual(repo)
    mocker.patch.object(Audit, "initialize", side_effect=AuditError("cannot create journal"))
    assert (
        main(
            [
                "rebase",
                "--continue",
                "--repo",
                str(repo.root),
                "--check",
                sys.executable,
                "-c",
                "pass",
            ]
        )
        == 1
    )
    assert "Integrated: True" in capfd.readouterr().out
    assert not repo.git.operation_active()
