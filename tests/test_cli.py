# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The installed git-ez extension uses Git configuration and the real agent runtime."""

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from git_ez.cli import main, parser
from git_ez.git import Git


@pytest.mark.parametrize("operation", ["rebase", "merge", "cherry-pick"])
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
def test_show_rejects_report_without_authoritative_manifest(tmp_path, capsys):
    report = tmp_path / "report.md"
    content = "# Integration\nConflict: file.txt\nDecision: combine\nRationale: preserve intent\n"
    report.write_text(content)
    before = report.stat().st_mtime_ns
    assert main(["audit", "show", "--path", str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "Cannot read resolution journal" in output.err
    assert report.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [report]


@pytest.mark.unit
def test_show_missing_report_returns_error(tmp_path, capsys):
    assert main(["audit", "show", "--path", str(tmp_path)]) == 2
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


@pytest.mark.parametrize(
    "operation,invocation,subdirectory",
    [
        ("rebase", "extension", False),
        ("merge", "extension", False),
        ("cherry-pick", "extension", True),
        ("rebase", "extension", True),
        ("merge", "alias", True),
    ],
)
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
    assert not git.operation_active()
    assert not git.run("status", "--porcelain").stdout
    if operation != "cherry-pick":
        git.run("merge-base", "--is-ancestor", "main", "HEAD")
    audits = list((git.path / ".git/ez/runs").iterdir())
    assert len(audits) == 1
    assert "Preserve both intentions" in (audits[0] / "report.md").read_text()
    assert (audits[0] / "validation.stdout").read_text() == "checked\n"
    assert (audits[0] / "sessions/0001/messages.json").is_file()


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
    assert main(["probe"]) == 0
    (config,) = probe.call_args.args
    assert config.model == "configured-model"


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
    assert result.stdout.strip() == f"git-ez {version('git-ez-ai')}"


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
    assert result.stdout.strip() == f"git-ez {version('git-ez-ai')}"


@pytest.mark.integration
def test_failed_cli_run_returns_one_and_a_readable_audit(cli_repo, monkeypatch, capsys):
    monkeypatch.chdir(cli_repo.path)
    before = cli_repo.branch_oid("feature")
    assert main(["merge", "main", "--check", sys.executable, "-c", "raise SystemExit(7)"]) == 1
    captured = capsys.readouterr()
    assert "Status: failed" in captured.out
    assert "Validation failed with code 7" in captured.err
    assert not cli_repo.operation_active()
    assert cli_repo.branch_oid("feature") != before
    audit = next((cli_repo.path / ".git/ez/runs").iterdir())
    assert "Validation failed with code 7" in (audit / "report.md").read_text()
    assert main(["audit", "show", "--path", str(audit)]) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["merge", "main"], None),
        (["merge", "--ff", "main"], "ff"),
        (["merge", "main", "--no-ff"], "no-ff"),
        (["merge", "main", "--ff-only"], "ff-only"),
        (["merge", "--ff-only", "main", "--no-ff", "--ff"], "ff"),
    ],
)
def test_merge_fast_forward_policy_syntax(arguments, expected):
    assert parser().parse_args(arguments).merge_ff == expected


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["--ff-only"])
def test_merge_fast_forward_policies_are_not_rebase_options(flag):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["rebase", "main", flag])
    assert error.value.code == 2


@pytest.mark.integration
def test_cli_ff_only_refuses_divergence_without_model_call(
    cli_repo, model_server, monkeypatch, capsys
):
    monkeypatch.chdir(cli_repo.path)
    before = cli_repo.branch_oid("feature")
    assert main(["merge", "main", "--ff-only"]) == 1
    output = capsys.readouterr()
    assert "Status: failed" in output.out
    assert "Integrated: False" in output.out
    assert cli_repo.branch_oid("feature") == before
    assert not cli_repo.operation_active()
    assert model_server.calls == []
    audit = next((cli_repo.path / ".git/ez/runs").iterdir())
    assert "Not possible to fast-forward" in (audit / "report.md").read_text()


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments", [["rebase", "--onto", "main", "old"], ["rebase", "old", "--onto", "main"]]
)
def test_rebase_onto_syntax(arguments):
    args = parser().parse_args(arguments)
    assert args.target == "old"
    assert args.onto == "main"


@pytest.mark.unit
def test_merge_rejects_onto():
    with pytest.raises(SystemExit):
        parser().parse_args(["merge", "main", "--onto", "old"])


@pytest.mark.integration
def test_cli_rebase_onto_reaches_engine(cli_repo, monkeypatch, capsys):
    monkeypatch.chdir(cli_repo.path)
    assert main(["rebase", "HEAD", "--onto", "main"]) == 0
    assert "Status: succeeded" in capsys.readouterr().out
    assert cli_repo.branch_oid("feature") == cli_repo.branch_oid("main")


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["--force-rebase", "-f", "--no-ff"])
def test_rebase_force_aliases_and_reset(flag):
    assert parser().parse_args(["rebase", "main", flag]).force_rebase is True
    assert parser().parse_args(["rebase", "main", flag, "--ff"]).force_rebase is False


@pytest.mark.integration
def test_cli_rebase_empty_options_reach_engine(cli_repo, monkeypatch, capsys):
    monkeypatch.chdir(cli_repo.path)
    cli_repo.run("commit", "--allow-empty", "-m", "empty")
    assert (
        main(["rebase", "main", "--empty=keep", "--no-keep-empty", "--reapply-cherry-picks"]) == 0
    )
    assert "Status: succeeded" in capsys.readouterr().out
    assert cli_repo.text("show", "-s", "--format=%s", "HEAD") != "empty"


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments", [["rebase", "main", "--empty=unknown"], ["merge", "main", "--empty=keep"]]
)
def test_invalid_rebase_policies_are_rejected(arguments):
    with pytest.raises(SystemExit):
        parser().parse_args(arguments)


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["rebase", "-S", "main"], ""),
        (["merge", "--gpg-sign", "main"], ""),
        (["merge", "main", "--gpg-sign=key"], "key"),
        (["rebase", "-Skey", "main"], "key"),
        (["merge", "main", "-S", "--no-gpg-sign"], False),
        (["merge", "main", "--no-gpg-sign", "-Skey"], "key"),
    ],
)
def test_signing_syntax_does_not_consume_target(arguments, expected):
    args = parser().parse_args(arguments)
    assert args.target == "main"
    assert args.gpg_sign == expected


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["rebase", "merge", "cherry-pick"])
@pytest.mark.parametrize("check_option", ["--check", "--chec", "--ch", "--c"])
@pytest.mark.parametrize("attached", [False, True])
def test_signing_arguments_inside_check_are_untouched(operation, check_option, attached):
    arguments = [f"{check_option}=command"] if attached else [check_option, "command"]
    command_arguments = ["-S", "--gpg-sign", "--no-gpg-sign", "--ff", "--", "--check"]
    args = parser().parse_args([operation, "main", *arguments, *command_arguments])
    assert args.check == ["command", *command_arguments]
    assert args.gpg_sign is None


@pytest.mark.unit
def test_signing_before_attached_check_is_still_an_integration_option():
    args = parser().parse_args(["merge", "-Skey", "main", "--check=command", "-S"])
    assert args.gpg_sign == "key"
    assert args.check == ["command", "-S"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments",
    [
        ["audit", "show", "run", "--check", "command"],
        ["probe", "--check=command"],
    ],
)
def test_check_is_rejected_for_non_integration_commands(arguments, capsys):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(arguments)
    assert error.value.code == 2
    assert "--check is only supported for rebase, merge and cherry-pick" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.parametrize("check_option", ["--check", "--chec", "--check=", "--chec="])
def test_cli_validation_arguments_stay_literal(cli_repo, monkeypatch, capsys, check_option):
    monkeypatch.chdir(cli_repo.path)
    # A no-op integration isolates argument handling from the resolver.
    validation = [sys.executable, "-S", "-c", "print('literal validation')"]
    arguments = (
        [check_option + validation[0], *validation[1:]]
        if check_option.endswith("=")
        else [check_option, *validation]
    )
    assert main(["merge", "HEAD", *arguments]) == 0
    assert "Status: succeeded" in capsys.readouterr().out
    directory = next((cli_repo.path / ".git/ez/runs").iterdir())
    assert (directory / "validation.stdout").read_text().strip() == "literal validation"


@pytest.mark.integration
@pytest.mark.parametrize("from_file", [True, False])
def test_cli_merge_message_and_signoff(cli_repo, monkeypatch, capsys, tmp_path, from_file):
    monkeypatch.chdir(tmp_path)
    if from_file:
        (tmp_path / "message.txt").write_text("Custom merge\n\nDetails\n")
        arguments = ["-F", "message.txt"]
    else:
        arguments = ["-m", "Custom merge", "-m", "Details"]
    assert main(["merge", "main", "--repo", str(cli_repo.path), *arguments, "--signoff"]) == 0
    assert "Status: succeeded" in capsys.readouterr().out
    assert cli_repo.text("show", "-s", "--format=%B", "HEAD") == (
        "Custom merge\n\nDetails\n\nSigned-off-by: Test <test@example.invalid>"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ["-", "/nonexistent/git-ez-message"])
def test_bad_message_file_is_rejected_before_configuration(path, mocker, capsys):
    config = mocker.patch("git_ez.cli.Config.from_git")
    assert main(["merge", "main", "-F", path]) == 2
    assert "git-ez:" in capsys.readouterr().err
    config.assert_not_called()


@pytest.mark.unit
def test_message_and_file_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parser().parse_args(["merge", "main", "-m", "message", "-F", "file"])


@pytest.mark.integration
def test_message_file_content_is_pinned_before_integration(cli_repo, monkeypatch, tmp_path, mocker):
    from git_ez.engine import Engine

    message = tmp_path / "message.txt"
    message.write_text("original message\n")
    monkeypatch.chdir(cli_repo.path)
    prepare = Engine._prepare_run

    def change_message_file(self, *args):
        audit = prepare(self, *args)
        message.write_text("changed message\n")
        return audit

    mocker.patch.object(Engine, "_prepare_run", change_message_file)
    assert main(["merge", "main", "-F", str(message)]) == 0
    assert cli_repo.text("show", "-s", "--format=%B", "HEAD") == "original message"


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["rebase", "merge", "cherry-pick"])
def test_explicit_rerere_policy_syntax(operation):
    args = parser().parse_args([operation, "main", "--rerere-autoupdate", "--no-rerere-autoupdate"])
    assert args.rerere_autoupdate is False
    assert parser().parse_args([operation, "main"]).rerere_autoupdate is None


@pytest.mark.integration
def test_cli_rerere_policy_reaches_engine(cli_repo, monkeypatch):
    import json

    monkeypatch.chdir(cli_repo.path)
    assert main(["merge", "main", "--no-rerere-autoupdate"]) == 0
    directory = next((cli_repo.path / ".git/ez/runs").iterdir())
    manifest = json.loads((directory / "run.json").read_text())
    assert manifest["rerere_autoupdate"] is False
    assert manifest["rerere_autoupdate_effective"] is False


@pytest.mark.unit
@pytest.mark.parametrize("source", ["message", "file"])
def test_nul_message_is_rejected_before_configuration(tmp_path, mocker, capsys, source):
    config = mocker.patch("git_ez.cli.Config.from_git")
    engine = mocker.patch("git_ez.cli.Engine")
    message = "title\0body"
    if source == "file":
        path = tmp_path / "message.txt"
        path.write_text(message)
        arguments = ["-F", str(path)]
    else:
        arguments = ["-m", message]
    assert main(["merge", "main", *arguments]) == 2
    assert "Commit message must not contain NUL characters" in capsys.readouterr().err
    config.assert_not_called()
    engine.assert_not_called()


@pytest.mark.integration
def test_nul_message_file_leaves_repository_unchanged(
    cli_repo, model_server, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(cli_repo.path)
    message = tmp_path / "message.txt"
    message.write_bytes(b"title\0body")
    before = cli_repo.text("show-ref")
    assert main(["merge", "main", "-F", str(message)]) == 2
    assert "NUL characters" in capsys.readouterr().err
    assert cli_repo.text("show-ref") == before
    assert cli_repo.run("status", "--porcelain").stdout == b""
    assert not cli_repo.operation_active()
    assert not (cli_repo.path / ".git/ez").exists()
    assert model_server.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [True, False])
def test_autosquash_cli_policy_is_explicit_and_last_value_wins(enabled):
    flags = ["--autosquash", "--no-autosquash"]
    if enabled:
        flags.reverse()
    assert parser().parse_args(["rebase", "main", *flags]).autosquash is enabled
    assert parser().parse_args(["rebase", "main"]).autosquash is None
    with pytest.raises(SystemExit):
        parser().parse_args(["merge", "main", "--autosquash"])


@pytest.mark.integration
def test_cli_autosquash_uses_native_grouping_without_a_model(cli_repo, model_server, monkeypatch):
    monkeypatch.chdir(cli_repo.path)
    (cli_repo.path / "file.txt").write_text("fixed source\n")
    cli_repo.run("commit", "-am", "fixup! source")
    target = cli_repo.text("rev-parse", "main^")
    assert main(["rebase", target, "--autosquash"]) == 0
    assert cli_repo.text("rev-list", "--count", f"{target}..HEAD") == "1"
    assert cli_repo.text("show", "-s", "--format=%s", "HEAD") == "source"
    assert model_server.calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "flag", ["--keep-base", "--no-keep-base", "--fork-point", "--no-fork-point"]
)
def test_rebase_selection_flags_are_not_merge_options(flag):
    with pytest.raises(SystemExit):
        parser().parse_args(["merge", "main", flag])


@pytest.mark.integration
@pytest.mark.parametrize("keep", [True, False])
def test_cli_rebase_selection_options_reach_the_engine(cli_repo, model_server, monkeypatch, keep):
    import json

    monkeypatch.chdir(cli_repo.path)
    target = "main" if keep else "HEAD"
    flags = ["--keep-base", "--no-fork-point"] if keep else ["--fork-point"]
    before = cli_repo.text("rev-parse", "HEAD")
    assert main(["rebase", target, *flags]) == 0
    assert cli_repo.text("rev-parse", "HEAD") == before
    assert model_server.calls == []
    directory = next((cli_repo.path / ".git/ez/runs").iterdir())
    manifest = json.loads((directory / "run.json").read_text())
    assert manifest["rebase_options"]["keep_base"] is keep
    assert manifest["rebase_plan"]["fork_point"]["enabled"] is (not keep)


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["--log", "main"], 20),
        (["--log=2", "main"], 2),
        (["main", "--log=0"], 0),
        (["main", "--log", "--no-log"], 0),
        (["main", "--no-log", "--log=3"], 3),
    ],
)
@pytest.mark.unit
def test_merge_log_parsing_preserves_target(arguments, expected):
    args = parser().parse_args(["merge", *arguments])
    assert args.target == "main"
    assert args.log == expected


@pytest.mark.unit
def test_merge_log_does_not_change_validation_arguments():
    args = parser().parse_args(["merge", "main", "--check", "tool", "--log"])
    assert args.log is None
    assert args.check == ["tool", "--log"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["rebase", "main", "--log"],
        ["rebase", "main", "--cleanup=strip"],
        ["merge", "main", "--cleanup=invalid"],
        ["merge", "main", "--log=invalid"],
    ],
)
@pytest.mark.unit
def test_invalid_merge_message_options(arguments):
    with pytest.raises(SystemExit):
        parser().parse_args(arguments)


@pytest.mark.integration
@pytest.mark.parametrize("from_file", [False, True])
def test_cli_merge_log_cleanup_reaches_commit(cli_repo, tmp_path, capsys, from_file):
    if from_file:
        path = tmp_path / "merge-message.txt"
        path.write_text("Custom merge\n\n# remove me\n")
        message = ["-F", str(path)]
    else:
        message = ["-m", "Custom merge\n\n# remove me\n"]
    assert (
        main(
            ["merge", "main", "--repo", str(cli_repo.path), "--log=1", "--cleanup=strip", *message]
        )
        == 0
    )
    result = cli_repo.text("show", "-s", "--format=%B", "HEAD")
    assert "Custom merge" in result
    assert "remove me" not in result
    assert "  parent" in result
    assert "Status: succeeded" in capsys.readouterr().out


@pytest.mark.unit
def test_negative_merge_log_is_rejected_before_configuration(mocker, capsys):
    config = mocker.patch("git_ez.cli.Config.from_git")
    assert main(["merge", "main", "--log=-1"]) == 2
    config.assert_not_called()
    assert "nonnegative" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.parametrize("outcome", ["compatible", "provider_error", "partial_failure"])
def test_probe_json_destination(tmp_path, monkeypatch, mocker, capsys, outcome):
    import json

    from git_ez.config import Config
    from git_ez.errors import EzError

    monkeypatch.chdir(tmp_path)
    mocker.patch("git_ez.cli.Config.from_git", return_value=Config(model="test"))
    compatible = outcome == "compatible"
    if outcome != "provider_error":
        from langchain_core.messages import AIMessage

        def respond(messages):
            import re

            marker = re.search(r"ez-[0-9a-f]{32}", str(messages)).group()
            if len(messages) == 1 and "Call compatibility_echo" in messages[0].content:
                if outcome == "partial_failure":
                    return AIMessage(content="no tool call")
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "compatibility_echo", "args": {"value": marker}, "id": "call"}
                    ],
                )
            return AIMessage(content=marker)

        model = mocker.Mock()
        model.invoke.side_effect = respond
        model.bind_tools.return_value = model
        mocker.patch("git_ez.probe.create_chat_model", return_value=model)
    else:
        mocker.patch("git_ez.probe.create_chat_model", side_effect=EzError("Invalid provider"))
    assert main(["probe"]) == (0 if compatible else 1)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == ("compatible" if compatible else "failed")
    assert f"Compatibility: {report['status']}" in captured.err
    assert list(tmp_path.iterdir()) == []
    if outcome == "provider_error":
        assert report["error"]["message"] == "Invalid provider"
    elif outcome == "partial_failure":
        assert [check["ok"] for check in report["checks"]] == [True, False]
        assert report["error"]["type"] == "EzError"


@pytest.mark.unit
def test_probe_rejects_removed_output_option(tmp_path, mocker, capsys):
    probe = mocker.patch("git_ez.probe.probe")
    output = tmp_path / "report.json"
    with pytest.raises(SystemExit, match="2"):
        main(["probe", "--output", str(output)])
    captured = capsys.readouterr()
    assert "unrecognized arguments: --output" in captured.err
    assert captured.out == ""
    assert not output.exists()
    probe.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("on_flush", [False, True])
def test_probe_stdout_write_failure(mocker, capsys, on_flush):
    import io

    from git_ez.config import Config

    mocker.patch("git_ez.cli.Config.from_git", return_value=Config(model="test"))
    mocker.patch("git_ez.probe.probe", return_value={"status": "compatible"})

    class FailedOutput(io.StringIO):
        def write(self, value):
            if not on_flush:
                raise BrokenPipeError("consumer closed")
            return super().write(value)

        def flush(self):
            raise OSError("storage full")

    with mocker.patch("git_ez.cli.sys.stdout", FailedOutput()):
        assert main(["probe"]) != 0
    captured = capsys.readouterr()
    assert "git-ez:" in captured.err
    assert "Compatibility: compatible" not in captured.err


@pytest.mark.unit
def test_probe_configuration_error(mocker, capsys):
    from git_ez.errors import EzError

    mocker.patch("git_ez.cli.Config.from_git", side_effect=EzError("Configure ez.model"))
    probe = mocker.patch("git_ez.probe.probe")
    assert main(["probe"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Configure ez.model" in captured.err
    probe.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize("destination", ["file", "pipe"])
def test_probe_redirected_json(tmp_path, model_server, destination):
    import json

    git = Git(tmp_path)
    git.run("init", "-b", "main")
    git.run("config", "ez.model", "test")
    git.run("config", "ez.endpoint", model_server.endpoint)
    before = git.run("status", "--porcelain").stdout
    command = [sys.executable, "-m", "git_ez.cli", "probe"]
    if destination == "file":
        output = tmp_path / "report.json"
        with output.open("w") as stream:
            result = subprocess.run(command, cwd=tmp_path, stdout=stream, stderr=subprocess.PIPE)
        report = json.loads(output.read_text())
    else:
        with subprocess.Popen(
            command, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as producer:
            consumer = subprocess.run(
                [sys.executable, "-c", "import json,sys; print(json.load(sys.stdin)['status'])"],
                stdin=producer.stdout,
                capture_output=True,
                text=True,
            )
            producer.stdout.close()
            _, diagnostics = producer.communicate()
        assert producer.returncode == 1
        assert b"Compatibility: failed" in diagnostics
        assert consumer.returncode == 0
        assert consumer.stdout == "failed\n"
        assert git.run("status", "--porcelain").stdout == before
        return
    assert result.returncode == 1
    assert b"Compatibility: failed" in result.stderr
    assert report["status"] == "failed"
    assert report["checks"][0]["ok"] is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments",
    [
        ["main", "feature"],
        ["main", "--no-commit"],
        ["main", "--mainline=1"],
        ["--continue", "main"],
    ],
)
def test_cherry_pick_rejects_unsupported_arguments(arguments):
    with pytest.raises(SystemExit):
        parser().parse_args(["cherry-pick", *arguments])
