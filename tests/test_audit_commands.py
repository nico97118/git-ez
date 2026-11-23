# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Resolution attempts and saved reports remain understandable without native history."""

import json
import os
import pty
import select
import subprocess
import sys
from pathlib import Path

import pytest

from git_ez.audit import load_report
from git_ez.audit_cli import consult
from git_ez.cli import main
from git_ez.engine import Engine
from git_ez.errors import EzError
from tests.test_engine import repo as repo

pytestmark = pytest.mark.integration


def test_reprompt_keeps_both_proposals_and_common_reference(repo, monkeypatch):
    number = 0

    def resolve(git, audit, attempt, paths, check):
        nonlocal number
        number += 1
        repo.file.write_text(f"proposal {number}\n")
        return {"rationale": f"Explanation {number}", "confidence": 0.9 + number / 100}

    repo.resolver.run = resolve
    answers = iter(["r", "Keep error handling", "a"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    attempts = json.loads((audit.directory / "run.json").read_text())["reviews"]
    assert len(attempts) == 2
    assert attempts[0]["decision"] == "revise"
    assert attempts[0]["application"] == "not_staged"
    assert attempts[1]["application"] == "staged"
    assert attempts[1]["feedback"] == "Keep error handling"
    for index, attempt in enumerate(attempts, 1):
        record = attempt["conflicts"][0]
        assert record["resolution"]["content"] == f"proposal {index}\n"
        assert record["resolution"]["rationale"] == f"Explanation {index}"
        diff = (audit.directory / record["artifacts"]["diff"]).read_text()
        assert "-source" in diff
        assert f"+proposal {index}" in diff
    report = load_report(audit.directory)
    assert "Attempt 1: revise" in report
    assert "Attempt 2: accept" in report
    assert "+proposal 1" in report and "+proposal 2" in report
    assert repo.git.text("show", "HEAD:file.txt") == "proposal 2"


@pytest.mark.parametrize("ending", ["error", "stop"])
def test_reprompt_failure_does_not_apply_previous_attempt(repo, monkeypatch, ending):
    number = 0

    def resolve(*args):
        nonlocal number
        number += 1
        repo.file.write_text(f"proposal {number}\n")
        if number == 2 and ending == "error":
            raise EzError("Resolver refused the revision")
        return {"rationale": f"Explanation {number}", "confidence": 0.95}

    repo.resolver.run = resolve
    answers = iter(["r", "Change this", "s"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge", interactive=True)
    assert audit.view["status"] == "failed"
    first, second = audit.view["reviews"]
    assert first["decision"] == "revise"
    assert first["conflicts"][0]["resolution"]["content"] == "proposal 1\n"
    assert second["conflicts"][0]["resolution"]["content"] == "proposal 2\n"
    assert all(attempt["application"] == "not_staged" for attempt in (first, second))
    if ending == "error":
        assert "rationale" not in second["conflicts"][0]["resolution"]
    assert repo.git.conflicts()
    assert repo.git.branch_oid("feature") == repo.source_oid


def test_discovery_prefix_and_copied_journal(repo, monkeypatch, tmp_path, capsys):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    monkeypatch.chdir(repo.root)
    assert main(["audit", "list"]) == 0
    assert audit.directory.name in capsys.readouterr().out
    assert main(["audit", "show"]) == 0
    assert "+combined" in capsys.readouterr().out
    assert main(["audit", "show", audit.directory.name[:8]]) == 0
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    assert main(["audit", "show", "--path", str(audit.directory)]) == 0
    assert "Confidence: 0.95" in capsys.readouterr().out
    assert main(["audit", "show", "id", "--path", str(audit.directory)]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_discovery_rejects_ambiguity_and_uses_recorded_date(repo, monkeypatch):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    other = audit.directory.parent / (audit.directory.name[:8] + "older")
    other.mkdir()
    data = audit.view
    data.update(id=other.name, created_at="2000-01-01T00:00:00+00:00")
    (other / "run.json").write_text(json.dumps(data))
    monkeypatch.chdir(repo.root)
    with pytest.raises(EzError, match="Ambiguous"):
        consult("show", audit.directory.name[:8], None, False)
    assert f"Run: {audit.directory.name}" in consult("show", None, None, False)
    assert main(["audit", "show", "nonexistent"]) == 2


def test_no_runs_and_audit_help(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    assert main(["audit", "list"]) == 0
    assert "No audit runs" in capsys.readouterr().out
    assert main(["audit", "show"]) == 2
    with pytest.raises(SystemExit) as exit:
        main(["audit"])
    assert exit.value.code == 0


def test_pager_native_overrides_and_redirection(repo, tmp_path):
    audit = Engine(repo.root, repo.config, repo.resolver).run("main", "merge")
    marker = tmp_path / "pager-used"
    pager = tmp_path / "pager.py"
    pager.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('used')\n"
        "sys.stdout.write(sys.stdin.read())\n"
    )
    repo.git.run("config", "core.pager", f"{sys.executable} {pager}")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env.pop("GIT_PAGER", None)
    env.pop("PAGER", None)
    for options, paged in [([], True), (["--no-pager"], False), (["--paginate"], True)]:
        marker.unlink(missing_ok=True)
        master, slave = pty.openpty()
        try:
            with subprocess.Popen(
                ["git", *options, "ez", "audit", "show", audit.directory.name],
                cwd=repo.root,
                env=env,
                stdout=slave,
                stderr=slave,
            ) as process:
                os.close(slave)
                slave = -1
                output = b""
                while process.poll() is None:
                    if select.select([master], [], [], 10)[0]:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                process.wait(timeout=10)
                assert process.returncode == 0, output.decode(errors="replace")
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)
        assert marker.exists() is paged
    marker.unlink(missing_ok=True)
    result = subprocess.run(
        ["git", "ez", "audit", "show"],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "+combined" in result.stdout
    assert not marker.exists()
