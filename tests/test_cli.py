# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The installed git-ez extension uses Git configuration and the real agent runtime."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from git_ez.cli import main, parser
from git_ez.git import Git


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.unit
def test_git_like_syntax_defaults_to_current_directory(operation):
    args = parser().parse_args([operation, "main"])
    assert args.command == operation
    assert args.target == "main"
    assert args.repo == Path(".")


@pytest.mark.parametrize(
    "args",
    [
        ["review", "/tmp/run"],
        ["run", "feature", "main"],
        ["rebase", "main", "--config", "file.toml"],
        ["rebase", "main", "--remote", "origin"],
    ],
)
@pytest.mark.unit
def test_only_supported_git_commands_are_accepted(args):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(args)
    assert error.value.code == 2


@pytest.mark.unit
def test_show_displays_report_without_modifying_audit(tmp_path, capsys):
    report = tmp_path / "report.md"
    content = "# Integration\nConflict: file.txt\nDecision: combine\nRationale: preserve intent\n"
    report.write_text(content)
    before = report.stat().st_mtime_ns
    assert main(["show", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == content.strip()
    assert report.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [report]


@pytest.mark.unit
def test_show_missing_report_returns_error(tmp_path, capsys):
    assert main(["show", str(tmp_path)]) == 2
    assert "git-ez:" in capsys.readouterr().err


@pytest.fixture
def cli_repo(tmp_path, model_server):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    git = Git(root)
    git.run("init", "-b", "main")
    git.run("config", "user.name", "Test")
    git.run("config", "user.email", "test@example.invalid")
    (root / "file.txt").write_text("base\n")
    git.run("add", ".")
    git.run("commit", "-m", "base")
    git.run("checkout", "-b", "feature")
    (root / "file.txt").write_text("source\n")
    git.run("commit", "-am", "source")
    git.run("checkout", "main")
    (root / "file.txt").write_text("parent\n")
    git.run("commit", "-am", "parent")
    git.run("checkout", "feature")
    git.run("config", "ez.model", "scripted-model")
    git.run("config", "ez.endpoint", model_server.endpoint)
    return git


@pytest.mark.parametrize("operation", ["rebase", "merge"])
@pytest.mark.parametrize("invocation", ["extension", "alias"])
@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.integration
def test_git_extension_resolves_through_real_http_and_agent(
    cli_repo,
    model_server,
    operation,
    invocation,
    subdirectory,
):
    git = cli_repo
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if invocation == "extension":
        command = ["git", "ez", operation]
    else:
        git.run("config", f"alias.ai-{operation}", f"ez {operation}")
        command = ["git", f"ai-{operation}"]
    cwd = git.path
    if subdirectory:
        cwd = cwd / "src"
        cwd.mkdir()
    result = subprocess.run(
        [*command, "main", "--check", sys.executable, "-c", "print('checked')"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Status: succeeded" in result.stdout
    assert (git.path / "file.txt").read_text() == "combined\n"
    assert git.text("remote") == ""
    assert len(model_server.calls) == 2
    audits = list((git.path / ".git/ez/runs").iterdir())
    assert len(audits) == 1
    assert "Preserve both intentions" in (audits[0] / "report.md").read_text()
    assert (audits[0] / "validation.stdout").read_text() == "checked\n"
    assert (audits[0] / "sessions/0001/checkpoints.sqlite").is_file()


@pytest.mark.integration
def test_missing_model_configuration_explains_setup(tmp_path, monkeypatch, capsys):
    git = Git(tmp_path)
    git.run("init", "-b", "main")
    monkeypatch.chdir(tmp_path)
    assert main(["rebase", "main"]) == 2
    assert "Configure ez.model" in capsys.readouterr().err
    assert not (tmp_path / ".git/ez").exists()


@pytest.mark.integration
def test_probe_uses_git_configuration(tmp_path, monkeypatch, mocker):
    Git(tmp_path).run("init", "-b", "main")
    Git(tmp_path).run("config", "ez.model", "configured-model")
    monkeypatch.chdir(tmp_path)
    probe = mocker.patch("git_ez.probe.probe", return_value={"status": "compatible"})
    output = tmp_path / "probe.json"
    assert main(["probe", "--output", str(output)]) == 0
    config, destination = probe.call_args.args
    assert config.model == "configured-model"
    assert destination == output


@pytest.mark.integration
def test_git_command_line_configuration_is_forwarded(cli_repo, model_server):
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        ["git", "-c", "ez.model=one-off-model", "ez", "merge", "main"],
        cwd=cli_repo.path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(call["model"] == "one-off-model" for call in model_server.calls)


@pytest.mark.integration
def test_version_is_available_without_a_repository_or_configuration(tmp_path):
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        ["git", "ez", "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "git-ez 0.1.0"


@pytest.mark.unit
def test_empty_check_is_rejected_before_loading_configuration(mocker, capsys):
    config = mocker.patch("git_ez.cli.Config.from_git")
    assert main(["rebase", "main", "--check"]) == 2
    assert "--check requires an executable" in capsys.readouterr().err
    config.assert_not_called()


@pytest.mark.integration
def test_module_entry_point_reports_version_without_configuration(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "git_ez", "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "git-ez 0.1.0"
