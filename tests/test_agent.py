# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Real Deep Agents graph/backend/checkpoint integration with a scripted chat model."""

import shlex
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from git_ez.config import Config
from git_ez.engine import Engine
from git_ez.git import Git


def tool(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": ""}


def command(script: str) -> dict[str, Any]:
    return tool("execute", command=shlex.join([sys.executable, "-B", "-c", script]))


class ScriptedModel(BaseChatModel):
    model_name: str = "test-qwen"
    provider_name: str = "openai"
    turns: list[list[dict[str, Any]]] = Field(default_factory=list)
    seen: list[Any] = Field(default_factory=list)
    exposed_tools: list[str] = Field(default_factory=list)
    position: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-model"

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": self.provider_name, "ls_model_name": self.model_name}

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        self.exposed_tools = [t.name if hasattr(t, "name") else t.get("name", "") for t in tools]
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.seen.append(list(messages))
        calls = self.turns[self.position] if self.position < len(self.turns) else []
        self.position += 1
        calls = [call | {"id": f"call-{self.position}-{i}"} for i, call in enumerate(calls)]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))]
        )


@pytest.fixture
def repo(tmp_path: Path, mocker):
    root = tmp_path / "working"
    root.mkdir()
    repo = SimpleNamespace(root=root.resolve())
    repo.mocker = mocker
    Git(repo.root).run("init", "-b", "main")
    repo.git = Git(repo.root)
    repo.git.run("config", "user.name", "Test")
    repo.git.run("config", "user.email", "test@example.invalid")
    (repo.root / "file.txt").write_text("base\n")
    (repo.root / "rules.txt").write_text("The intended result is combined.\n")
    repo.git.run("add", ".")
    repo.git.run("commit", "-m", "base")
    repo.git.run("checkout", "-b", "feature")
    (repo.root / "file.txt").write_text("source\n")
    repo.git.run("commit", "-am", "source")
    repo.source = repo.git.branch_oid("feature")
    repo.git.run("checkout", "main")
    (repo.root / "file.txt").write_text("parent\n")
    repo.git.run("commit", "-am", "parent")
    repo.parent = repo.git.branch_oid("main")
    repo.config = Config(
        provider="openai",
        model="test-qwen",
        endpoint="http://localhost:8000/v1/chat/completions",
    )
    repo.check = (
        sys.executable,
        "-B",
        "-c",
        "from pathlib import Path; assert Path('file.txt').read_text() == 'combined\\n'",
    )
    repo.git.run("checkout", "feature")
    return repo


def run_agent(
    repo,
    turns: list[list[dict[str, Any]]],
    operation: str = "rebase",
    config: Config | None = None,
    check: tuple[str, ...] = (),
):
    mocker = repo.mocker
    selected = config or repo.config
    model = ScriptedModel(
        turns=turns,
        provider_name=selected.provider,
        profile={"max_input_tokens": 32768, "tool_calling": True},
    )
    mocker.patch("git_ez.agent_runtime.create_chat_model", return_value=model)
    audit = Engine(repo.root, selected).run("main", operation, validation=check)
    return (audit, model)


@pytest.mark.parametrize("operation", ["rebase", "merge"])
def test_real_graph_reads_context_repairs_tests_and_persists_integration(repo, operation) -> None:
    audit, model = run_agent(
        repo,
        [
            [tool("ls", path="/"), tool("read_file", file_path="/rules.txt")],
            [tool("execute", command=shlex.join(repo.check))],
            [
                command("from pathlib import Path; Path('file.txt').write_text('combined\\n')"),
                tool("write_file", file_path="/notes.txt", content="Supporting change.\n"),
            ],
            [tool("execute", command=shlex.join(repo.check))],
            [
                tool(
                    "finish",
                    rationale="Read rules, repaired the failing check and retested.",
                    confidence=0.95,
                )
            ],
        ],
        operation=operation,
        check=repo.check,
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    result = Git(Path(audit.manifest["worktree"]))
    assert result.text("show", "HEAD:notes.txt") == "Supporting change."
    assert repo.git.branch_oid("feature") == audit.manifest["result_oid"]
    assert "task" not in model.exposed_tools
    assert "read_file" in model.exposed_tools
    session = audit.directory / "sessions/0001"
    events = (session / "events.jsonl").read_text()
    assert "rules.txt" in events
    assert "AssertionError" in events
    assert "notes.txt" in (session / "staged.diff").read_text()
    with closing(sqlite3.connect(session / "checkpoints.sqlite")) as db:
        assert db.execute("select count(*) from checkpoints").fetchone()[0] > 0
    assert not (result.path / ".ez-agent").exists()


def test_finish_with_markers_can_recover(repo) -> None:
    audit, model = run_agent(
        repo,
        [
            [tool("finish", rationale="Premature", confidence=0.9)],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Repaired after feedback", confidence=0.9)],
        ],
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert "conflict markers" in str(model.seen[1])


def test_low_confidence_preserves_edits_and_checkpoint(repo) -> None:
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('uncertain\\n')")],
            [tool("finish", rationale="Unclear business intent", confidence=0.1)],
        ],
    )
    assert audit.manifest["status"] == "failed"
    assert audit.manifest["sessions"][0]["status"] == "low_confidence"
    assert (Path(audit.manifest["worktree"]) / "file.txt").read_text() == "uncertain\n"
    assert (audit.directory / "sessions/0001/checkpoints.sqlite").is_file()
    assert "Unclear business intent" in (audit.directory / "report.md").read_text()
    assert (audit.directory / "sessions/0001/decision.json").is_file()


def test_limits_stop_unfinished_agent(repo) -> None:
    audit, _ = run_agent(
        repo, [[tool("ls", path="/")]] * 5, config=replace(repo.config, agent_max_turns=2)
    )
    assert audit.manifest["status"] == "failed"
    assert (audit.directory / "sessions/0001/events.jsonl").is_file()


def test_all_conflicts_are_in_one_session(repo) -> None:
    for branch, text in (("feature", "source second"), ("main", "parent second")):
        repo.git.run("checkout", branch)
        (repo.root / "second.txt").write_text(text)
        repo.git.run("add", "second.txt")
        repo.git.run("commit", "--amend", "-m", "add conflicting file")
    repo.git.run("checkout", "feature")
    audit, _ = run_agent(
        repo,
        [
            [
                command(
                    "from pathlib import Path; Path('file.txt').write_text('combined\\n'); "
                    "Path('second.txt').write_text('also combined\\n')"
                )
            ],
            [tool("finish", rationale="Both conflicts resolved together", confidence=0.9)],
        ],
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert len(audit.manifest["sessions"]) == 1
    assert len(audit.manifest["conflicts"]) == 2


def test_git_control_state_changes_are_detected(repo) -> None:
    audit, _ = run_agent(repo, [[tool("execute", command="git rebase --abort")]])
    assert audit.manifest["status"] == "failed"
    assert "Git control state" in audit.manifest["error"]
    assert repo.git.branch_oid("feature") == repo.source


def test_plain_answer_without_finish_is_not_success(repo) -> None:
    audit, _ = run_agent(repo, [[]])
    assert audit.manifest["status"] == "failed"
    assert "without a verified finish" in audit.manifest["error"]


def test_later_rebase_stop_gets_an_independent_checkpoint(repo) -> None:
    repo.git.run("checkout", "feature")
    (repo.root / "file.txt").write_text("source second\n")
    repo.git.run("commit", "-am", "second source change")
    audit, model = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="First replay resolved", confidence=0.9)],
            [command("from pathlib import Path; Path('file.txt').write_text('combined again\\n')")],
            [tool("finish", rationale="Second replay resolved", confidence=0.9)],
        ],
    )
    assert audit.manifest["status"] == "succeeded", audit.manifest
    assert len(audit.manifest["sessions"]) == 2
    for number in ("0001", "0002"):
        assert (audit.directory / "sessions" / number / "checkpoints.sqlite").is_file()
    assert model.position == 4


def test_changed_merge_head_is_rejected(repo):
    base = repo.git.text("rev-parse", f"{repo.source}^")
    script = (
        "from pathlib import Path; import subprocess; "
        "path=subprocess.check_output(['git','rev-parse','--git-path','MERGE_HEAD'],"
        "text=True).strip(); "
        f"Path(path).write_text({base!r}+'\\n'); "
        "Path('file.txt').write_text('combined\\n')"
    )
    audit, _ = run_agent(
        repo,
        [
            [command(script)],
            [tool("finish", rationale="Combined", confidence=0.95)],
        ],
        operation="merge",
    )
    assert audit.manifest["status"] == "failed"
    assert "Git control state" in audit.manifest["error"]
    assert repo.git.branch_oid("feature") == repo.source
    assert audit.manifest["conflicts"][0]["status"] == "failed"


@pytest.mark.parametrize("confidence", [0.1, None])
def test_failed_session_archives_proposals_and_new_files(repo, confidence):
    turns = [
        [
            command(
                "from pathlib import Path; Path('file.txt').write_text('uncertain\\n'); "
                "Path('support.bin').write_bytes(b'new\\x00supporting file')"
            )
        ]
    ]
    if confidence is not None:
        turns.append([tool("finish", rationale="Unclear business intent", confidence=confidence)])
    audit, _ = run_agent(repo, turns)
    assert audit.manifest["status"] == "failed"
    session = audit.directory / "sessions/0001"
    import json

    snapshot = json.loads((session / "files.json").read_text())
    records = {record["path"]: record for record in snapshot["files"]}
    assert (session / records["support.bin"]["artifact"]).read_bytes() == b"new\0supporting file"
    assert (session / records["file.txt"]["artifact"]).read_text() == "uncertain\n"
    conflict = audit.manifest["conflicts"][0]
    assert conflict["status"] == ("low_confidence" if confidence is not None else "failed")
    assert (audit.directory / conflict["artifacts"]["proposed"]).read_text() == "uncertain\n"
    exit_dir = audit.directory / "exit"
    final = json.loads((exit_dir / "files.json").read_text())
    assert any(record["path"] == "support.bin" for record in final["files"])
    if confidence is not None:
        assert conflict["resolution"]["rationale"] == "Unclear business intent"


def test_failed_session_snapshot_explicitly_marks_truncated_files(repo):
    import json

    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('new.txt').write_text('x'*64)")],
            [tool("finish", rationale="Uncertain", confidence=0.1)],
        ],
        config=replace(repo.config, max_file_bytes=16),
    )
    session = audit.directory / "sessions/0001"
    snapshot = json.loads((session / "files.json").read_text())
    new_file = next(record for record in snapshot["files"] if record["path"] == "new.txt")
    assert new_file["truncated"]
    assert new_file["size"] == 64
    assert new_file["recorded_bytes"] == 16
    assert (session / new_file["artifact"]).read_bytes() == b"x" * 16


@pytest.mark.parametrize("transport", ["parameters", "count"])
def test_agent_environment_redacts_transient_git_tokens_without_losing_other_settings(
    repo,
    monkeypatch,
    transport,
):
    from git_ez.agent_runtime import agent_environment

    if transport == "parameters":
        entries = ["ez.apiKey=fake-token", "user.name=O'Reilly"]
        parameters = " ".join("'" + entry.replace("'", "'\\''") + "'" for entry in entries)
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", parameters)
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "ez.apiKey")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "fake-token")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.name")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "O'Reilly")
    monkeypatch.setenv(repo.config.api_key_env, "fake-env-token")
    env = agent_environment(repo.config)
    assert "fake-token" not in str(env)
    assert "fake-env-token" not in str(env)
    import subprocess

    result = subprocess.run(
        ["git", "config", "--get", "user.name"],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "O'Reilly"
