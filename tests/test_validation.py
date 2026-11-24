# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Requested validation outcomes, side effects, and retained integration results."""

import json
import sys

import pytest

from git_ez.audit import Audit
from tests.fakes import FakeResolver
from tests.helpers import integrate

pytestmark = pytest.mark.integration


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
