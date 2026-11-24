# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Regression cases for the agreed audit scope and review findings."""

import sys

import pytest

from git_ez.audit import Audit
from git_ez.engine import Engine
from git_ez.errors import AuditError

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("ending", ["error", "interrupted", "low_confidence"])
def test_failed_attempt_retains_supporting_changes(repo, monkeypatch, interactive, ending):
    monkeypatch.setattr("builtins.input", lambda prompt: "s")
    from git_ez.errors import EzError

    def fail(*args):
        repo.file.write_text("partial conflict resolution\n")
        (repo.root / "support.txt").write_text("SUPPORTING-CHANGE-REVIEW\n")
        if ending == "interrupted":
            raise KeyboardInterrupt
        if ending == "low_confidence":
            return {"rationale": "Needs manual review", "confidence": 0.1}
        raise EzError("resolver failed")

    repo.resolver.run = fail
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "merge", interactive=interactive
    )
    assert audit.view["status"] == ("interrupted" if ending == "interrupted" else "failed")
    files = list(audit.directory.rglob("*"))
    assert any(p.is_file() and b"SUPPORTING-CHANGE-REVIEW" in p.read_bytes() for p in files)


def test_resolution_diff_without_final_newline_is_readable(repo):
    repo.file.write_text("source")
    repo.git.run("commit", "--amend", "--no-edit", "-a")
    repo.resolver.content = "combined"
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    assert audit.view["status"] == "succeeded", audit.view
    record = audit.view["conflicts"][0]
    diff = (audit.directory / record["artifacts"]["diff"]).read_text()
    assert "\n+combined" in diff, repr(diff)


@pytest.mark.parametrize(
    "pager", ["/definitely-missing-git-ez-pager --flag", "/definitely-missing-git-ez-pager"]
)
def test_missing_pager_with_options_falls_back_to_stdout(monkeypatch, capsys, pager):
    from git_ez.audit_cli import display

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("GIT_PAGER_IN_USE", raising=False)
    monkeypatch.setenv("GIT_PAGER", pager)
    display("REPORT-MUST-REMAIN-VISIBLE\n")
    assert "REPORT-MUST-REMAIN-VISIBLE" in capsys.readouterr().out


@pytest.mark.parametrize("exit_code", [0, 7])
def test_command_output_does_not_fake_timeout(tmp_path, exit_code):
    import json
    import os
    import shlex

    from git_ez.agent_runtime import JournalShell, SessionLog

    audit = Audit(tmp_path / "journal", {"operation": "merge", "branch": "test", "conflicts": []})
    audit.initialize()
    log = SessionLog(audit.directory / "sessions/0001", audit)
    shell = JournalShell(
        log, 1024, root_dir=tmp_path, virtual_mode=True, timeout=2, env=dict(os.environ)
    )
    result = shell.execute(
        shlex.join(
            [
                sys.executable,
                "-c",
                "import sys; print('Error: Command timed out in the nested application'); "
                f"sys.exit({exit_code})",
            ]
        )
    )
    assert result.exit_code == exit_code
    events = [
        json.loads(line) for line in (log.directory / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["outcome"] == "returned"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_missing_validation_stdout_is_diagnostic_not_run_failure(
    repo, monkeypatch, stream, exit_code
):
    original = Audit.write

    def fail(self, artifact, content):
        if artifact == f"validation.{stream}":
            raise AuditError("Cannot export validation stdout")
        return original(self, artifact, content)

    monkeypatch.setattr(Audit, "write", fail)
    audit = Engine(repo.root, repo.config, repo.resolver).run(
        "main", "merge", validation=(sys.executable, "-c", f"raise SystemExit({exit_code})")
    )
    assert audit.view["integrated"]
    assert audit.view["validation"]["status"] == ("passed" if exit_code == 0 else "failed")
    assert audit.view["status"] == ("succeeded" if exit_code == 0 else "failed")
    assert audit.published
    assert any(error["phase"] == f"validation {stream}" for error in audit.audit_errors)
    if exit_code:
        assert audit.view["error"] == "Validation failed with code 7"


@pytest.mark.parametrize(
    "before,after", [("old", "new"), ("old\n", "new"), ("old", "new\n"), ("", "new"), ("old", "")]
)
def test_no_newline_diffs_can_be_applied_by_git(repo, before, after):
    from git_ez.conflicts import text_diff

    repo.file.write_text(before)
    patch = repo.root / "resolution.patch"
    patch.write_text(text_diff(before, after, "a/file.txt", "b/file.txt"))
    repo.git.run("apply", "--check", str(patch))
    repo.git.run("apply", str(patch))
    assert repo.file.read_text() == after


def test_quitting_the_pager_does_not_replay_the_report(monkeypatch, capsys):
    from git_ez.audit_cli import display

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("GIT_PAGER_IN_USE", raising=False)
    monkeypatch.setenv("GIT_PAGER", "sh -c 'exit 0'")
    display("REPORT-MUST-NOT-BE-REPLAYED\n")
    assert "REPORT-MUST-NOT-BE-REPLAYED" not in capsys.readouterr().out


def test_later_baseline_failure_preserves_previous_applied_attempt(repo, monkeypatch):
    from git_ez.audit import load_report
    from git_ez.checks import WorktreeChecks

    repo.file.write_text("source second\n")
    repo.git.run("commit", "-am", "second conflicting change")
    second_oid = repo.git.text("rev-parse", "HEAD")
    original = WorktreeChecks.baseline

    def baseline(self, git):
        head = git.run("rev-parse", "--verify", "REBASE_HEAD", check=False).stdout.decode().strip()
        if head == second_oid:
            raise OSError("Cannot inspect the later conflict")
        return original(self, git)

    monkeypatch.setattr(WorktreeChecks, "baseline", baseline)
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "rebase")
    assert audit.view["status"] == "failed"
    assert len(repo.resolver.calls) == 1
    assert len(audit.view["reviews"]) == 1
    first = audit.view["reviews"][0]
    assert first["status"] == "applied"
    assert first["application"] == "staged"
    assert first["resolution"] == {"rationale": "Combine both intentions", "confidence": 0.95}
    assert "Confidence: 0.95" in load_report(audit.directory)
    assert repo.git.rebase_active()


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n", "\u2028", "\x85"])
def test_text_diff_preserves_non_lf_characters(repo, separator):
    from git_ez.audit_report import artifact_lines
    from git_ez.conflicts import text_diff

    before = f"left{separator}old"
    after = f"left{separator}new"
    repo.file.write_bytes(before.encode())
    patch = repo.root / "resolution.patch"
    patch.write_bytes(text_diff(before, after, "a/file.txt", "b/file.txt").encode())
    repo.git.run("apply", "--check", str(patch))
    repo.git.run("apply", str(patch))
    assert repo.file.read_bytes() == after.encode()
    if separator not in ("\n", "\r\n"):
        escaped = f"\\u{ord(separator):04x}"
        display = artifact_lines(repo.root, patch.name)
        assert f"-left{escaped}old" in display
        assert f"+left{escaped}new" in display
