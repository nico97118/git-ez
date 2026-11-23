# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Real Deep Agents graph/backend/checkpoint integration with a scripted chat model."""

import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from git_ez.audit_state import snapshot_view
from git_ez.config import Config
from git_ez.engine import Engine
from git_ez.git import Git

pytestmark = pytest.mark.integration


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
    assert audit.view["status"] == "succeeded", audit.view
    result = Git(Path(audit.view["worktree"]))
    assert result.text("show", "HEAD:notes.txt") == "Supporting change."
    assert repo.git.branch_oid("feature") == audit.view["result_oid"]
    assert "task" not in model.exposed_tools
    assert "read_file" in model.exposed_tools
    session = audit.directory / "sessions/0001"
    events = (session / "events.jsonl").read_text()
    assert "rules.txt" in events
    assert "AssertionError" in events
    assert "notes.txt" in (session / "staged.diff").read_text()
    assert not (session / "checkpoints.sqlite").exists()
    assert json.loads((session / "runtime.json").read_text())["checkpoints"] == {
        "storage": "memory",
        "resumable": False,
    }
    assert "checkpoints.sqlite" not in (audit.directory / "report.md").read_text()
    assert not (result.path / ".ez-agent").exists()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
@pytest.mark.parametrize("kind", ["binary", "large"])
def test_finish_accepts_untouched_git_resources(repo, operation, kind):
    content = b"image\x00\xff" if kind == "binary" else b"x" * (repo.config.max_file_bytes + 1)
    repo.git.run("checkout", "feature")
    (repo.root / "resource.dat").write_bytes(content)
    repo.git.run("add", "resource.dat")
    repo.git.run("commit", "--amend", "--no-edit")
    repo.source = repo.git.branch_oid("feature")

    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved only the text conflict.", confidence=0.95)],
        ],
        operation=operation,
    )

    assert audit.view["status"] == "succeeded", audit.view
    assert (repo.root / "resource.dat").read_bytes() == content
    assert repo.git.run("show", "HEAD:resource.dat").stdout == content
    assert audit.view["sessions"][0]["status"] == "resolved"


def test_finish_rejects_agent_changes_to_git_integrated_binary_and_allows_repair(repo):
    repo.git.run("checkout", "feature")
    (repo.root / "resource.dat").write_bytes(b"Git\x00resource")
    repo.git.run("add", "resource.dat")
    repo.git.run("commit", "--amend", "--no-edit")
    repo.source = repo.git.branch_oid("feature")

    audit, model = run_agent(
        repo,
        [
            [
                command(
                    "from pathlib import Path; Path('file.txt').write_text('combined\\n'); "
                    "Path('resource.dat').write_bytes(b'bad\\x00resource')"
                )
            ],
            [tool("finish", rationale="Changed the resource too.", confidence=0.95)],
            [
                command(
                    "from pathlib import Path; "
                    "Path('resource.dat').write_bytes(b'Git\\x00resource')"
                )
            ],
            [tool("finish", rationale="Restored the untouched Git resource.", confidence=0.95)],
        ],
    )

    assert audit.view["status"] == "succeeded", audit.view
    assert "Binary content" in str(model.seen[2])
    assert (repo.root / "resource.dat").read_bytes() == b"Git\x00resource"


def test_finish_with_markers_can_recover(repo) -> None:
    audit, model = run_agent(
        repo,
        [
            [tool("finish", rationale="Premature", confidence=0.9)],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Repaired after feedback", confidence=0.9)],
        ],
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert "conflict markers" in str(model.seen[1])
    events = [
        json.loads(line)
        for line in (audit.directory / "sessions/0001/events.jsonl").read_text().splitlines()
    ]
    feedback = [event for event in events if event.get("origin") == "middleware"]
    assert len(feedback) == 1  # The SDK already recorded the attempted finish request.
    assert feedback[0]["event"] == "tool_result"
    assert "conflict markers" in feedback[0]["result"]["content"]


def test_low_confidence_preserves_edits_and_audit(repo) -> None:
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('uncertain\\n')")],
            [tool("finish", rationale="Unclear business intent", confidence=0.1)],
        ],
    )
    assert audit.view["status"] == "failed"
    assert audit.view["sessions"][0]["status"] == "low_confidence"
    assert (Path(audit.view["worktree"]) / "file.txt").read_text() == "uncertain\n"
    assert (audit.directory / "sessions/0001/messages.json").is_file()
    assert "Unclear business intent" in (audit.directory / "report.md").read_text()
    assert (audit.directory / "sessions/0001/decision.json").is_file()


@pytest.mark.parametrize(
    "limit,error_name",
    [
        ("agent_max_turns", "ModelCallLimitExceededError"),
        ("agent_max_tool_calls", "ToolCallLimitExceededError"),
    ],
)
def test_limits_stop_unfinished_agent_for_the_expected_reason(repo, limit, error_name) -> None:
    audit, model = run_agent(
        repo, [[tool("ls", path="/")]] * 5, config=replace(repo.config, **{limit: 2})
    )
    assert audit.view["status"] == "failed"
    assert error_name in audit.view["error"]
    session = audit.view["sessions"][0]
    assert session["tool_calls"] == 2
    if limit == "agent_max_turns":
        assert len(model.seen) == 2
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source
    assert error_name in (audit.directory / "sessions/0001/events.jsonl").read_text()


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
    assert audit.view["status"] == "succeeded", audit.view
    assert len(audit.view["sessions"]) == 1
    assert len(audit.view["conflicts"]) == 2
    assert repo.git.text("show", "HEAD:file.txt") == "combined"
    assert repo.git.text("show", "HEAD:second.txt") == "also combined"
    assert not repo.git.conflicts()
    assert not repo.git.operation_active()


def test_git_control_state_changes_are_detected(repo) -> None:
    audit, _ = run_agent(repo, [[tool("execute", command="git rebase --abort")]])
    assert audit.view["status"] == "failed"
    assert "Git control state" in audit.view["error"]
    assert repo.git.branch_oid("feature") == repo.source


def test_plain_answer_without_finish_is_not_success(repo) -> None:
    audit, _ = run_agent(repo, [[]])
    assert audit.view["status"] == "failed"
    assert "without a verified finish" in audit.view["error"]


def test_later_rebase_stop_gets_an_independent_session(repo) -> None:
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
    assert audit.view["status"] == "succeeded", audit.view
    assert len(audit.view["sessions"]) == 2
    for number in ("0001", "0002"):
        assert (audit.directory / "sessions" / number / "messages.json").is_file()
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
    assert audit.view["status"] == "failed"
    assert "Git control state" in audit.view["error"]
    assert repo.git.branch_oid("feature") == repo.source
    assert audit.view["conflicts"][0]["status"] == "failed"


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
    assert audit.view["status"] == "failed"
    session = audit.directory / "sessions/0001"
    import json

    snapshot = json.loads((session / "files.json").read_text())
    records = {record["path"]: record for record in snapshot["files"]}
    assert (session / records["support.bin"]["artifact"]).read_bytes() == b"new\0supporting file"
    assert (session / records["file.txt"]["artifact"]).read_text() == "uncertain\n"
    conflict = audit.view["conflicts"][0]
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
    assert audit.view["status"] == "failed"
    assert audit.view["sessions"][0]["status"] == "low_confidence"
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source
    assert new_file["truncated"]
    assert audit.view["evidence"]["sessions/0001/files.json"]["status"] == "partial"
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


def test_finish_in_a_tool_batch_is_rejected_until_it_is_called_alone(repo):
    audit, model = run_agent(
        repo,
        [
            [tool("finish", rationale="Premature batch", confidence=0.95), tool("ls", path="/")],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Verified alone", confidence=0.95)],
        ],
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert "finish must be called alone" in str(model.seen[1])
    assert audit.view["sessions"][0]["rationale"] == "Verified alone"
    events = [
        json.loads(line)
        for line in (audit.directory / "sessions/0001/events.jsonl").read_text().splitlines()
    ]
    rejected = [event for event in events if event.get("tool_call_id") == "call-1-0"]
    assert [event["event"] for event in rejected] == ["tool_requested", "tool_result"]
    assert rejected[-1]["result"]["content"] == "finish must be called alone"


def test_agent_cannot_override_the_configured_command_timeout(repo):
    unsafe = tool("execute", command="touch forbidden.txt", timeout=repo.config.timeout + 1)
    audit, model = run_agent(
        repo,
        [
            [unsafe],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved within limits", confidence=0.95)],
        ],
    )
    assert audit.view["status"] == "succeeded", audit.view
    assert "Command timeout exceeds configured limit" in str(model.seen[1])
    assert not (repo.root / "forbidden.txt").exists()
    events = [
        json.loads(line)
        for line in (audit.directory / "sessions/0001/events.jsonl").read_text().splitlines()
    ]
    rejected = [event for event in events if event.get("tool_call_id") == "call-1-0"]
    assert [event["event"] for event in rejected] == ["tool_requested", "tool_result"]
    assert rejected[-1]["result"]["content"] == "Command timeout exceeds configured limit"
    assert audit.view["sessions"][0]["tool_calls"] == 3


@pytest.mark.parametrize("event_name", ["tool_requested", "tool_result"])
def test_middleware_feedback_capture_failure_stops_before_next_model_call(repo, event_name):
    from git_ez.agent_runtime import SessionLog

    event = SessionLog.event

    def fail_feedback(self, name, **data):
        if name == event_name and data.get("origin") == "middleware":
            raise OSError("Synthetic feedback capture failure")
        event(self, name, **data)

    repo.mocker.patch.object(SessionLog, "event", fail_feedback)
    audit, model = run_agent(
        repo,
        [
            [tool("execute", command="touch forbidden.txt", timeout=repo.config.timeout + 1)],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Should not be reached", confidence=0.95)],
        ],
    )
    assert len(model.seen) == 1
    assert audit.view["status"] == "failed"
    assert audit.view["failure"]["domain"] == "audit"
    assert audit.view["audit_health"] == "incomplete"
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source
    assert not (repo.root / "forbidden.txt").exists()


@pytest.mark.parametrize("bad_call", [tool("missing_tool"), tool("execute")])
def test_sdk_normalized_tool_feedback_is_recorded_once(repo, bad_call):
    audit, _ = run_agent(
        repo,
        [
            [bad_call],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved after feedback", confidence=0.95)],
        ],
    )
    assert audit.view["status"] == "succeeded", audit.view.get("error")
    events = [
        json.loads(line)
        for line in (audit.directory / "sessions/0001/events.jsonl").read_text().splitlines()
    ]
    requests = [event for event in events if event["event"] == "tool_requested"]
    results = [event for event in events if event["event"] == "tool_result"]
    assert len(requests) == len(results) == 3
    feedback = next(
        event["result"] for event in results if event["result"]["tool_call_id"] == "call-1-0"
    )
    assert feedback["status"] == "error"
    assert audit.view["sessions"][0]["tool_calls"] == 3


def test_context_byte_limit_stops_further_model_calls_and_preserves_evidence(repo):
    audit, model = run_agent(
        repo,
        [[command("print('x' * 10000)")]],
        config=replace(repo.config, agent_max_context_bytes=8192),
    )
    assert audit.view["status"] == "failed"
    assert "Agent context byte budget exceeded" in audit.view["error"]
    assert len(model.seen) == 1
    assert audit.view["sessions"][0]["tool_calls"] == 1
    assert repo.git.operation_active()
    assert (audit.directory / "sessions/0001/events.jsonl").is_file()
    assert (audit.directory / "exit/files.json").is_file()


def test_session_audit_capture_failure_prevents_git_continuation(repo):
    from git_ez.audit import Audit

    original = Audit.capture_worktree

    def capture(audit, git, directory, *args, **kwargs):
        if directory.parent.name == "sessions":
            raise OSError("session snapshot unavailable")
        return original(audit, git, directory, *args, **kwargs)

    repo.mocker.patch.object(Audit, "capture_worktree", capture)
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved", confidence=0.95)],
        ],
    )
    assert audit.view["status"] == "failed"
    assert not audit.view["integrated"]
    assert audit.view["sessions"][0]["status"] == "resolved"
    assert audit.view["sessions"][0]["evidence_health"] == "incomplete"
    assert audit.view["sessions"][0]["capture_error"] == "Session worktree capture failed"
    assert repo.git.branch_oid("feature") == repo.source
    assert repo.git.operation_active()
    assert (audit.directory / "exit/files.json").is_file()
    assert "Session worktree capture failed" in (audit.directory / "report.md").read_text()


def test_secondary_session_capture_failure_preserves_the_low_confidence_reason(repo):
    from git_ez.audit import Audit

    original = Audit.capture_worktree

    def capture(audit, git, directory, *args, **kwargs):
        if directory.parent.name == "sessions":
            raise OSError("secondary snapshot failure")
        return original(audit, git, directory, *args, **kwargs)

    repo.mocker.patch.object(Audit, "capture_worktree", capture)
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('uncertain\\n')")],
            [tool("finish", rationale="Uncertain", confidence=0.1)],
        ],
    )
    assert audit.view["status"] == "failed"
    assert "confidence below threshold" in audit.view["error"]
    session = audit.view["sessions"][0]
    assert session["status"] == "low_confidence"
    assert session["capture_error"] == "Session worktree capture failed"
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source


@pytest.mark.parametrize("ref", ["main", "feature"])
def test_agent_cannot_move_input_refs_while_head_is_detached(repo, ref):
    base = repo.git.text("rev-parse", f"{repo.source}^")
    audit, model = run_agent(
        repo,
        [
            [
                tool(
                    "execute",
                    command=f"git update-ref refs/heads/{ref} {base}",
                )
            ]
        ],
    )
    assert audit.view["status"] == "failed"
    assert "Git control state" in audit.view["error"]
    assert not audit.view["integrated"]
    assert len(model.seen) == 1
    assert repo.git.branch_oid(ref) == base
    assert audit.view["before_oid"] == repo.source
    assert audit.view["target_oid"] == repo.parent
    assert (audit.directory / "exit/files.json").is_file()


def test_provider_failure_is_audited_without_persisting_exception_secrets(repo):
    secret = "provider-echoed-private-token"
    repo.mocker.patch.object(ScriptedModel, "_generate", side_effect=RuntimeError(secret))
    audit, _ = run_agent(repo, [])
    assert audit.view["status"] == "failed"
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source
    event_path = audit.directory / "sessions/0001/events.jsonl"
    events = event_path.read_text() if event_path.exists() else ""
    assert '"event": "model_error"' in events
    assert '"type": "RuntimeError"' in events
    for artifact in audit.directory.rglob("*"):
        if artifact.is_file():
            assert secret.encode() not in artifact.read_bytes(), artifact.relative_to(
                audit.directory
            )


def test_resolver_mission_distinguishes_upstream_from_result_base(repo):
    import json

    boundary = repo.git.text("rev-parse", "main^")
    model = ScriptedModel(
        turns=[
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved on the requested new base", confidence=0.95)],
        ],
        profile={"max_input_tokens": 32768, "tool_calling": True},
    )
    repo.mocker.patch("git_ez.agent_runtime.create_chat_model", return_value=model)
    audit = Engine(repo.root, repo.config).run(boundary, onto="main")
    assert audit.view["status"] == "succeeded", audit.view
    mission = json.loads((audit.directory / "sessions/0001/mission.json").read_text())
    assert mission["target_sha"] == boundary
    assert mission["rebase_base_sha"] == repo.parent
    assert mission["replay_boundary_sha"] == boundary


def test_interactive_revisions_reach_runtime_and_keep_separate_sessions(repo, mocker):
    import json

    model = ScriptedModel(
        turns=[
            [command("from pathlib import Path; Path('file.txt').write_text('first\\n')")],
            [tool("finish", rationale="First proposal; no tests run", confidence=0.95)],
            [
                command(
                    "from pathlib import Path; assert Path('file.txt').read_text() == 'first\\n'; "
                    "Path('file.txt').write_text('combined\\n')"
                )
            ],
            [tool("finish", rationale="Revised current proposal; no tests run", confidence=0.95)],
        ],
        profile={"max_input_tokens": 32768, "tool_calling": True},
    )
    mocker.patch("git_ez.agent_runtime.create_chat_model", return_value=model)
    mocker.patch("builtins.input", side_effect=["r", "preserve both intentions", "a"])
    audit = Engine(repo.root, repo.config).run("main", interactive=True)
    assert audit.view["status"] == "succeeded", audit.view
    assert len(audit.view["sessions"]) == 2
    for number in (1, 2):
        assert (audit.directory / "sessions" / f"{number:04d}" / "messages.json").is_file()
    mission = json.loads((audit.directory / "sessions/0002/mission.json").read_text())
    assert mission["user_feedback"] == "preserve both intentions"
    assert mission["review_round"] == 1
    assert (audit.directory / "proposals/0001/files/0001.bin").read_text() == "first\n"


@pytest.mark.parametrize("outcome", ["success", "low_confidence", "interrupted"])
def test_agent_files_survive_success_failure_and_interruption(repo, outcome):
    if outcome == "interrupted":
        generate = ScriptedModel._generate

        def interrupt_after_edits(self, *args, **kwargs):
            if self.position == 3:
                files = list(
                    (repo.root / ".git/ez/runs").glob("*/sessions/0001/agent-files/proof.txt")
                )
                assert len(files) == 1
                assert files[0].read_text() == "Retained evidence\n"
                raise KeyboardInterrupt
            return generate(self, *args, **kwargs)

        repo.mocker.patch.object(ScriptedModel, "_generate", interrupt_after_edits)
    audit, model = run_agent(
        repo,
        [
            [tool("write_file", file_path="/.ez-agent/proof.txt", content="Retained evidence\n")],
            [tool("read_file", file_path="/.ez-agent/proof.txt")],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [
                tool(
                    "finish",
                    rationale="Checked retained evidence",
                    confidence=0.1 if outcome == "low_confidence" else 0.95,
                )
            ],
        ],
    )
    assert "Retained evidence" in str(model.seen[2])
    session = audit.directory / "sessions/0001"
    assert (session / "agent-files/proof.txt").read_text() == "Retained evidence\n"
    assert (session / "messages.json").is_file()
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "tool_result" for event in events)
    assert not (session / "checkpoints.sqlite").exists()
    assert not (repo.root / ".ez-agent").exists()
    if outcome == "success":
        assert audit.view["status"] == "succeeded"
        assert repo.git.text("show", "HEAD:file.txt") == "combined"
        assert repo.git.run("ls-files", "--", "*proof*").stdout == b""
    else:
        assert audit.view["status"] == ("interrupted" if outcome == "interrupted" else "failed")
        assert repo.git.operation_active()
        assert repo.git.branch_oid("feature") == repo.source
        assert (repo.root / "file.txt").read_text() == "combined\n"


def test_large_tool_output_is_offloaded_to_persistent_audit_files(repo):
    audit, model = run_agent(
        repo,
        [
            [command("print('0123456789' * 15000)")],
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved after large output", confidence=0.95)],
        ],
        config=replace(repo.config, tool_output_bytes=200000, agent_max_context_bytes=1000000),
    )
    assert audit.view["status"] == "succeeded", audit.view
    files = list((audit.directory / "sessions/0001/agent-files").rglob("*"))
    assert any(path.is_file() and "0123456789" * 15000 in path.read_text() for path in files)
    assert "/.ez-agent/" in str(model.seen[1])
    assert not (repo.root / ".ez-agent").exists()


@pytest.mark.parametrize("artifact", ["messages", "events", "metadata"])
@pytest.mark.parametrize("primary", ["none", "model", "low_confidence", "interrupted"])
def test_finalization_errors_preserve_primary_failure_and_capture_other_evidence(
    repo, artifact, primary
):
    from git_ez.agent_runtime import SessionLog
    from git_ez.audit import Audit
    from git_ez.audit_storage import AuditStore

    if artifact == "messages":
        write = AuditStore.write

        def failing_write(self, path, content, **kwargs):
            if path.endswith("/messages.json"):
                raise OSError("messages destination unavailable")
            return write(self, path, content, **kwargs)

        repo.mocker.patch.object(AuditStore, "write", failing_write)
    elif artifact == "events":
        event = SessionLog.event

        def failing_event(self, event_name, **data):
            if event_name == "session_finished":
                raise OSError("events destination unavailable")
            return event(self, event_name, **data)

        repo.mocker.patch.object(SessionLog, "event", failing_event)
    else:
        save = Audit.save

        def failing_save(self):
            sessions = self.view.get("sessions", [])
            if (
                self.view["lifecycle"] != "finished"
                and sessions
                and sessions[-1]["status"] != "running"
            ):
                raise OSError("metadata destination unavailable")
            return save(self)

        repo.mocker.patch.object(Audit, "save", failing_save)
    if primary in ("model", "interrupted"):
        repo.mocker.patch.object(
            ScriptedModel,
            "_generate",
            side_effect=RuntimeError("private-provider-token")
            if primary == "model"
            else KeyboardInterrupt(),
        )
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [
                tool(
                    "finish",
                    rationale="Uncertain intent" if primary == "low_confidence" else "Resolved",
                    confidence=0.1 if primary == "low_confidence" else 0.95,
                )
            ],
        ],
    )
    optional_only = artifact == "messages" and primary == "none"
    assert audit.view["status"] == (
        "succeeded" if optional_only else "interrupted" if primary == "interrupted" else "failed"
    )
    assert audit.view["integrated"] is optional_only
    if primary == "none":
        assert audit.view["sessions"][0]["status"] == "resolved"
        if not optional_only:
            assert "Agent session audit capture failed" in audit.view["error"]
    elif primary == "model":
        assert "Model request failed" in audit.view["error"]
    elif primary == "low_confidence":
        assert "confidence below threshold" in audit.view["error"]
        assert audit.view["sessions"][0]["status"] == "low_confidence"
    else:
        assert audit.view["error"] == "Interrupted by user"
        assert audit.view["sessions"][0]["status"] == "interrupted"
    assert any(issue["phase"] == f"session {artifact}" for issue in audit.audit_errors)
    saved = snapshot_view(json.loads((audit.directory / "run.json").read_text()))
    assert saved["status"] == audit.view["status"]
    assert saved["validation"]["status"] == "not_requested"
    assert (audit.directory / "exit/files.json").is_file()
    if artifact != "messages":
        assert (audit.directory / "sessions/0001/messages.json").is_file()
    for path in audit.directory.rglob("*"):
        if path.is_file():
            assert b"private-provider-token" not in path.read_bytes()


@pytest.mark.parametrize(
    "primary,event_name",
    [
        ("model", "model_error"),
        ("model", "model_request_failed"),
        ("low_confidence", "tool_error"),
        ("agent", "agent_error"),
    ],
)
def test_error_event_failure_keeps_primary_error_and_partial_log(repo, primary, event_name):
    from git_ez.agent_runtime import SessionLog

    event = SessionLog.event

    def failing_event(self, logged_event, **data):
        if logged_event == event_name:
            raise OSError("error event unavailable")
        event(self, logged_event, **data)

    repo.mocker.patch.object(SessionLog, "event", failing_event)
    if primary == "model":
        repo.mocker.patch.object(
            ScriptedModel, "_generate", side_effect=RuntimeError("private-provider-token")
        )
    elif primary == "agent":
        repo.mocker.patch(
            "git_ez.agent_runtime.create_deep_agent",
            side_effect=RuntimeError("private-provider-token"),
        )
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Uncertain intent", confidence=0.1)],
        ],
    )
    assert audit.view["status"] == "failed"
    if primary == "model":
        assert "Model request failed (RuntimeError)" in audit.view["error"]
    elif primary == "agent":
        assert "Deep Agents failed (RuntimeError)" in audit.view["error"]
    else:
        assert "confidence below threshold" in audit.view["error"]
    assert any(item["phase"] == f"session {event_name}" for item in audit.audit_errors)
    assert audit.evidence["sessions/0001/events.jsonl"]["status"] in ("partial", "unavailable")
    event_path = audit.directory / "sessions/0001/events.jsonl"
    events = event_path.read_text() if event_path.exists() else ""
    assert "session_finished" not in events
    for path in audit.directory.rglob("*"):
        if path.is_file():
            assert b"private-provider-token" not in path.read_bytes()
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source


@pytest.mark.parametrize(
    "event_name", ["model_requested", "model_result", "tool_requested", "tool_result"]
)
def test_normal_event_write_failure_stops_git_and_keeps_log_partial(repo, event_name):
    from git_ez.agent_runtime import SessionLog

    event = SessionLog.event
    failed = False

    def fail_once(self, logged_event, **data):
        nonlocal failed
        if logged_event == event_name and not failed:
            failed = True
            raise OSError("transient event destination failure")
        event(self, logged_event, **data)

    repo.mocker.patch.object(SessionLog, "event", fail_once)
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved", confidence=0.95)],
        ],
    )
    assert failed
    assert audit.view["status"] == "failed"
    assert "event capture failed" in audit.view["error"]
    if event_name == "tool_result":
        assert (repo.root / "file.txt").read_text() == "combined\n"
    assert any(item["phase"] == f"session {event_name}" for item in audit.audit_errors)
    assert audit.evidence["sessions/0001/events.jsonl"]["status"] in ("partial", "unavailable")
    event_path = audit.directory / "sessions/0001/events.jsonl"
    events = (
        [json.loads(line) for line in event_path.read_text().splitlines()]
        if event_path.exists()
        else []
    )
    assert not [item for item in events if item["event"] == "session_finished"]
    assert audit.view["sessions"][0]["status"] == "failed"
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source


def test_terminal_event_reflects_late_metadata_failure(repo):
    from git_ez.audit import Audit

    save = Audit.save
    failed = False

    def fail_once(self):
        nonlocal failed
        sessions = self.view.get("sessions", [])
        if sessions and sessions[-1]["status"] == "resolved" and not failed:
            failed = True
            raise OSError("late metadata destination failure")
        save(self)

    repo.mocker.patch.object(Audit, "save", fail_once)
    audit, _ = run_agent(
        repo,
        [
            [command("from pathlib import Path; Path('file.txt').write_text('combined\\n')")],
            [tool("finish", rationale="Resolved", confidence=0.95)],
        ],
    )
    assert failed
    session = audit.directory / "sessions/0001"
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
    terminal = [item for item in events if item["event"] == "session_finished"]
    assert len(terminal) == 1
    assert terminal[0]["status"] == audit.view["sessions"][0]["status"] == "resolved"
    saved = snapshot_view(json.loads((audit.directory / "run.json").read_text()))
    assert saved["sessions"][0]["status"] == "resolved"
    assert saved["sessions"][0]["evidence_health"] == "incomplete"
    assert saved["status"] == "failed"
    assert any(item["phase"] == "session metadata" for item in saved["audit_errors"])
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source


@pytest.mark.parametrize("phase", ["directory", "save", "head", "versions", "runtime"])
@pytest.mark.parametrize("secondary_capture_error", [False, True])
def test_startup_failure_finalizes_session_without_calling_model(
    repo, phase, secondary_capture_error
):
    import git_ez.agent_runtime as runtime
    from git_ez.audit import Audit, Record

    reason = f"startup {phase} unavailable"
    if phase == "directory":
        mkdir = Audit.create_directory

        def failing_mkdir(self, path):
            if path.name == "0001" and path.parent.name == "sessions":
                raise OSError(reason)
            return mkdir(self, path)

        repo.mocker.patch.object(Audit, "create_directory", failing_mkdir)
    elif phase == "save":
        save = Audit.save

        def failing_save(self):
            sessions = self.view.get("sessions", [])
            if sessions and sessions[-1]["status"] == "running":
                raise OSError(reason)
            save(self)

        repo.mocker.patch.object(Audit, "save", failing_save)
    elif phase == "head":
        text = Git.text

        def failing_head(git, *args):
            if args == ("rev-parse", "HEAD") and list(
                (repo.root / ".git/ez/runs").glob("*/sessions/0001")
            ):
                raise OSError(reason)
            return text(git, *args)

        repo.mocker.patch.object(Git, "text", failing_head)
    elif phase == "versions":
        repo.mocker.patch.object(runtime, "version", side_effect=OSError(reason))
    else:
        update = Record.update

        def failing_provenance(self, *args, **kwargs):
            if "runtime_metadata" in kwargs:
                raise OSError(reason)
            return update(self, *args, **kwargs)

        repo.mocker.patch.object(Record, "update", failing_provenance)
    if secondary_capture_error:
        capture = Audit.capture_worktree

        def failing_capture(self, git, directory, *args, **kwargs):
            if directory.parent.name == "sessions":
                raise OSError("secondary session capture unavailable")
            capture(self, git, directory, *args, **kwargs)

        repo.mocker.patch.object(Audit, "capture_worktree", failing_capture)
    model = repo.mocker.patch.object(runtime, "create_chat_model")
    audit = Engine(repo.root, repo.config).run("main")
    model.assert_not_called()
    assert audit.view["status"] == "failed"
    assert audit.view["failure"]["type"] == "OSError"
    assert audit.view["sessions"][0]["status"] == "failed"
    saved = snapshot_view(json.loads((audit.directory / "run.json").read_text()))
    assert saved["status"] == saved["sessions"][0]["status"] == "failed"
    assert saved["failure"]["type"] == "OSError"
    session = audit.directory / "sessions/0001"
    if phase != "directory":
        assert json.loads((session / "messages.json").read_text()) == []
        events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
        assert [item["status"] for item in events if item["event"] == "session_finished"] == [
            "failed"
        ]
    if secondary_capture_error:
        assert any(item["phase"] == "session worktree" for item in saved["audit_errors"])
    assert (audit.directory / "exit/files.json").is_file()
    assert not audit.view["integrated"]
    assert repo.git.operation_active()
    assert repo.git.branch_oid("feature") == repo.source
