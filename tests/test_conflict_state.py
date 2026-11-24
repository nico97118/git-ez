# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Native conflict snapshots, proposal restoration, and child cleanup."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from git_ez.engine import Engine
from git_ez.errors import EzError
from git_ez.git import Git
from tests.helpers import integrate

pytestmark = pytest.mark.integration


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
