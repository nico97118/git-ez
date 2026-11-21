# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Deep Agents adapter: native backends, LangGraph checkpoints, and local audit hooks."""

import json
import shlex
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from threading import RLock
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import CompositeBackend, LocalShellBackend, StateBackend
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.middleware.types import ToolCallRequest, hook_config
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage, messages_to_dict
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context

from .agent import AGENT_PROMPT
from .audit import Audit, now, write_json
from .checks import GitStateChanged, WorktreeChecks
from .config import Config
from .conflicts import Decision, LowConfidence
from .errors import EzError
from .git import Git, git_environment
from .llm import create_chat_model


def agent_environment(config: Config) -> dict[str, str]:
    """Keep caller Git settings while removing credentials transported through the environment."""
    env = git_environment()
    env.pop(config.api_key_env, None)
    for name, value in list(env.items()):
        if name.startswith("GIT_CONFIG_KEY_") and value.lower() == "ez.apikey":
            env[name.replace("_KEY_", "_VALUE_")] = "<redacted>"
    if parameters := env.get("GIT_CONFIG_PARAMETERS"):
        entries = shlex.split(parameters)
        if any(entry.partition("=")[0].lower() == "ez.apikey" for entry in entries):
            entries = [
                entry.partition("=")[0] + "=<redacted>"
                if entry.partition("=")[0].lower() == "ez.apikey"
                else entry
                for entry in entries
            ]
            # Git uses single-quoted shell words; preserve embedded apostrophes in other settings.
            env["GIT_CONFIG_PARAMETERS"] = " ".join(
                "'" + entry.replace("'", "'\\''") + "'" for entry in entries
            )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def error_info(error: BaseException) -> dict[str, Any]:
    """Do not persist HTTP bodies/headers that may echo credentials."""
    return {"type": type(error).__name__, "status_code": getattr(error, "status_code", None)}


class SessionLog(BaseCallbackHandler):
    """Local SDK callbacks; no remote tracing service is required."""

    raise_error = True
    run_inline = True

    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = RLock()
        self.conflicts = 0
        self.tools = 0
        self.messages: list[Any] = []

    def event(self, event: str, **data: Any) -> None:
        with self.lock, (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": now(), "event": event, **data}, default=str) + "\n")

    def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, **kwargs: Any) -> None:
        self.conflicts += 1
        self.messages = list(messages[-1]) if messages else []
        parameters = kwargs.get("invocation_params", {})
        self.event(
            "model_requested",
            run_id=kwargs.get("run_id"),
            messages=[messages_to_dict(batch) for batch in messages],
            tools=parameters.get("tools", []),
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for batch in response.generations:
            if batch and hasattr(batch[0], "message"):
                self.messages.append(batch[0].message)
        self.event(
            "model_result", run_id=kwargs.get("run_id"), response=response.model_dump(mode="json")
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.event("model_error", **error_info(error))

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        self.tools += 1
        self.event(
            "tool_requested",
            name=serialized.get("name"),
            input=input_str,
            run_id=kwargs.get("run_id"),
        )

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        result = output.model_dump(mode="json") if hasattr(output, "model_dump") else str(output)
        self.event("tool_result", result=result, run_id=kwargs.get("run_id"))

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.event("tool_error", **error_info(error))


class IntegrationMiddleware(AgentMiddleware[Any, Any, Any]):
    """Project-specific Git checks; execution and conversation belong to Deep Agents."""

    def __init__(
        self,
        config: Config,
        guard: Callable[[], None],
        log: SessionLog,
        finished: Callable[[], bool],
    ):
        self.config = config
        self.guard = guard
        self.log = log
        self.finished = finished
        self.lock = RLock()

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if self.finished():
            return {"jump_to": "end"}
        return None

    def wrap_model_call(self, request: Any, handler: Callable[..., Any]) -> Any:
        self.guard()
        size = len(json.dumps(messages_to_dict(request.messages), default=str).encode())
        if size > self.config.agent_max_context_bytes:
            raise EzError("Agent context byte budget exceeded")
        try:
            return handler(request)
        except Exception as exc:
            # LangGraph checkpoints persist task exceptions; sanitize before that boundary.
            self.log.event("model_request_failed", **error_info(exc))
            raise EzError(
                f"Model request failed ({type(exc).__name__}); inspect local events"
            ) from None

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[..., Any]) -> Any:
        # LangGraph may schedule a batch concurrently. Serialize worktree access so
        # edits/commands and checks do not race against each other.
        with self.lock:
            self.guard()
            call = request.tool_call
            if call["name"] == "finish":
                last = next(
                    (m for m in reversed(request.state["messages"]) if isinstance(m, AIMessage)),
                    None,
                )
                if last and len(last.tool_calls) != 1:
                    return ToolMessage(
                        content="finish must be called alone",
                        status="error",
                        tool_call_id=call["id"],
                    )
            if (
                call["name"] == "execute"
                and isinstance(call["args"].get("timeout"), (int, float))
                and call["args"]["timeout"] > self.config.timeout
            ):
                return ToolMessage(
                    content="Command timeout exceeds configured limit",
                    status="error",
                    tool_call_id=call["id"],
                )
            try:
                result = handler(request)
            except (LowConfidence, GitStateChanged):
                raise
            except (EzError, OSError, UnicodeError) as exc:
                result = ToolMessage(content=str(exc), status="error", tool_call_id=call["id"])
            self.guard()
            return result


def run_agent(
    config: Config,
    git: Git,
    audit: Audit,
    round_number: int,
    paths: list[str],
    check: tuple[str, ...],
) -> dict[str, Any]:
    directory = audit.directory / "sessions" / f"{round_number:04d}"
    directory.mkdir(parents=True)
    log = SessionLog(directory)
    session: dict[str, Any] = {
        "round": round_number,
        "status": "running",
        "conflicts": paths,
        "runtime": "deepagents",
        "tool_calls": 0,
        "artifacts": str(directory.relative_to(audit.directory)),
    }
    audit.manifest.setdefault("sessions", []).append(session)
    audit.save()
    checks = WorktreeChecks(config)
    head = git.text("rev-parse", "HEAD")
    branch = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() or None
    decision: dict[str, Any] = {}

    def guard() -> None:
        checks.guard(git, audit, head, branch)

    @tool
    def finish(rationale: str, confidence: float) -> str:
        """Finish ALL conflicts at this Git stop; call alone after editing and testing."""
        parsed = Decision.parse({"rationale": rationale, "confidence": confidence})
        proposed = {"rationale": parsed.rationale, "confidence": parsed.confidence}
        if parsed.confidence < config.min_confidence:
            session.update(proposed)
            write_json(directory / "decision.json", proposed)
            session["status"] = "low_confidence"
            raise LowConfidence("Agent confidence below threshold; worktree preserved")
        checks.verify(git, paths)
        decision.update(proposed)
        write_json(directory / "decision.json", decision)
        session.update(proposed)
        return "Resolution verified. Git continuation belongs to git-ez."

    metadata = {
        "runtime": "deepagents",
        "versions": {
            package: version(package)
            for package in (
                "deepagents",
                "langchain-openai",
                "langchain-ollama",
                "langgraph-checkpoint-sqlite",
            )
        },
        "provider": config.provider,
        "model": config.model,
        "model_context_tokens": config.model_context_tokens,
    }
    write_json(directory / "runtime.json", metadata)
    result: dict[str, Any] = {}
    try:
        model = create_chat_model(config)
        # Use the public profile API to disable default delegation for this single-agent task.
        register_harness_profile(
            f"{config.provider}:{config.model}",
            HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
        )
        env = agent_environment(config)
        shell = LocalShellBackend(
            root_dir=git.path,
            virtual_mode=True,
            timeout=max(1, int(config.timeout)),
            max_output_bytes=config.tool_output_bytes,
            env=env,
        )

        # Deep Agents offloads history/large results without polluting the project or git add -A.
        backend = CompositeBackend(
            default=shell,
            routes={"/.ez-agent/": StateBackend()},
            artifacts_root="/.ez-agent",
        )

        mission = {
            "operation": audit.manifest["operation"],
            "conflicts": paths,
            "worktree": str(git.path),
            "branch_sha": audit.manifest["before_oid"],
            "target_sha": audit.manifest["target_oid"],
            "head_sha": head,
            "validation_command": shlex.join(check) if check else None,
            "task": "Investigate and resolve this Git integration using your tools.",
        }
        write_json(directory / "mission.json", mission)
        with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
            agent = create_deep_agent(
                model=model,
                backend=backend,
                system_prompt=AGENT_PROMPT,
                tools=[finish],
                middleware=[
                    IntegrationMiddleware(config, guard, log, lambda: bool(decision)),
                    ModelCallLimitMiddleware(
                        run_limit=config.agent_max_turns, exit_behavior="error"
                    ),
                    ToolCallLimitMiddleware(
                        run_limit=config.agent_max_tool_calls, exit_behavior="error"
                    ),
                ],
                checkpointer=saver,
            )
            # Traces stay local even if this machine has global LangSmith settings.
            with tracing_context(enabled=False):
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": json.dumps(mission)}]},
                    config={
                        "configurable": {"thread_id": f"{audit.manifest['id']}-{round_number}"},
                        "callbacks": [log],
                        "recursion_limit": config.agent_max_turns * 20 + 100,
                        "max_concurrency": 1,
                    },
                )
            guard()
            if not decision:
                raise EzError("Deep Agents stopped without a verified finish decision")
            session.update(status="resolved", **decision)
            return decision
    except BaseException as exc:
        if session["status"] == "running":
            session["status"] = "failed"
        if isinstance(exc, (EzError, OSError, KeyboardInterrupt, SystemExit)):
            raise
        log.event("agent_error", **error_info(exc))
        raise EzError(f"Deep Agents failed ({type(exc).__name__}); inspect local events") from exc
    finally:
        session.update(tool_calls=log.tools, model_calls=log.conflicts)
        capture_failure: Exception | None = None
        try:
            audit.capture_worktree(git, directory, config.max_file_bytes, paths, reference=head)
        except (EzError, OSError, ValueError) as exc:
            session["capture_error"] = str(exc)
            if session["status"] == "resolved":
                session["status"] = "failed"
                capture_failure = exc
        write_json(
            directory / "messages.json", messages_to_dict(result.get("messages", log.messages))
        )
        # Callbacks + checkpoints retain the full trajectory even when invoke raises.
        log.event("session_finished", status=session["status"])
        audit.save()
        if capture_failure is not None:
            raise EzError(
                "Agent session audit capture failed; inspect the report"
            ) from capture_failure
