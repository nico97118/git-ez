# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Checkout execution locks across processes and lifecycle boundaries."""

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
