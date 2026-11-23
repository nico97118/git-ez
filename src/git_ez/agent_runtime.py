# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Deep Agents adapter: native backends, LangGraph checkpoints, and local audit hooks."""

import json
import shlex
from collections.abc import Callable, Mapping
from importlib.metadata import version
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any
from uuid import uuid4

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain.agents.middleware.types import ToolCallRequest, hook_config
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage, messages_to_dict
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langsmith import tracing_context

from .agent import AGENT_PROMPT
from .audit import Audit, now, safe_error
from .checks import GitStateChanged, WorktreeChecks
from .config import Config
from .conflicts import Decision, LowConfidence
from .errors import AuditError, EzError
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

    def __init__(self, directory: Path, audit: Audit):
        self.directory = directory
        self.audit = audit
        self.lock = RLock()
        self.conflicts = 0
        self.tools = 0
        self.messages: list[Any] = []
        self._tool_requests: set[str | None] = set()
        self._tool_results: set[str | None] = set()
        self._model_started: dict[str, float] = {}

    def event(self, event: str, **data: Any) -> None:
        with self.lock:
            self.audit.append_event(
                (self.directory / "events.jsonl").relative_to(self.audit.directory).as_posix(),
                {"at": now(), "event": event, **data},
            )

    def capture_event(self, event: str, **data: Any) -> Exception | None:
        return self.audit.attempt(
            f"session {event}",
            lambda: self.event(event, **data),
            (self.directory / "events.jsonl").relative_to(self.audit.directory).as_posix(),
        )

    def begin_tool(self, identifier: str | None) -> None:
        # A provider may reuse a call ID on a later turn. The middleware serializes
        # invocations; acknowledgements belong only to the current invocation.
        with self.lock:
            self._tool_requests.discard(identifier)
            self._tool_results.discard(identifier)

    def feedback(self, call: Mapping[str, Any], message: str) -> ToolMessage:
        """Record middleware feedback that bypasses ordinary tool-result callbacks."""
        return self.record_feedback(
            call,
            ToolMessage(
                content=message, status="error", tool_call_id=call["id"], name=call["name"]
            ),
        )

    def record_feedback(self, call: Mapping[str, Any], result: ToolMessage) -> ToolMessage:
        """Retain returned feedback once, including errors normalized by the SDK."""
        with self.lock:
            identifier = call["id"]
            if identifier in self._tool_results:
                return result
            if identifier not in self._tool_requests:
                self.tools += 1
                self.capture_event(
                    "tool_requested",
                    name=call["name"],
                    input=json.dumps(call["args"]),
                    tool_call_id=call["id"],
                    origin="middleware",
                )
                self._tool_requests.add(identifier)
            self.capture_event(
                "tool_result",
                result=result.model_dump(mode="json"),
                tool_call_id=call["id"],
                origin="middleware",
            )
            self._tool_results.add(identifier)
            return result

    def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, **kwargs: Any) -> None:
        self.conflicts += 1
        self._model_started[str(kwargs.get("run_id"))] = monotonic()
        self.messages = list(messages[-1]) if messages else []
        parameters = kwargs.get("invocation_params", {})
        self.capture_event(
            "model_requested",
            run_id=kwargs.get("run_id"),
            messages=[messages_to_dict(batch) for batch in messages],
            tools=parameters.get("tools", []),
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for batch in response.generations:
            if batch and hasattr(batch[0], "message"):
                self.messages.append(batch[0].message)
        self.capture_event(
            "model_result",
            run_id=kwargs.get("run_id"),
            response=response.model_dump(mode="json"),
            duration_seconds=monotonic()
            - self._model_started.pop(str(kwargs.get("run_id")), monotonic()),
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.capture_event(
            "model_error",
            run_id=kwargs.get("run_id"),
            duration_seconds=monotonic()
            - self._model_started.pop(str(kwargs.get("run_id")), monotonic()),
            **error_info(error),
        )

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        self.tools += 1
        self.capture_event(
            "tool_requested",
            name=serialized.get("name"),
            input=input_str,
            run_id=kwargs.get("run_id"),
            tool_call_id=kwargs.get("tool_call_id"),
        )
        if isinstance(identifier := kwargs.get("tool_call_id"), str):
            with self.lock:
                self._tool_requests.add(identifier)

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        result = output.model_dump(mode="json") if hasattr(output, "model_dump") else str(output)
        self.capture_event("tool_result", result=result, run_id=kwargs.get("run_id"))
        if isinstance(identifier := getattr(output, "tool_call_id", None), str):
            with self.lock:
                self._tool_results.add(identifier)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.capture_event("tool_error", **error_info(error))


class JournalShell(LocalShellBackend):
    """Keep command results before the SDK combines or offloads their output."""

    def __init__(self, log: SessionLog, output_limit: int, **kwargs: Any):
        super().__init__(max_output_bytes=output_limit, **kwargs)
        self.log = log
        self.output_limit = output_limit
        self.streams: dict[str, Any] = {}

    def _combine_output(self, stdout: str | None, stderr: str | None) -> tuple[str, bool]:
        # The pinned SDK exposes separate streams only at this combination hook.
        remaining = self.output_limit
        self.streams = {}
        for name, value in (("stdout", stdout), ("stderr", stderr)):
            raw = (value or "").encode()
            retained = raw[:remaining]
            self.streams[name] = retained.decode(errors="replace")
            self.streams[f"{name}_truncated"] = len(retained) < len(raw)
            remaining -= len(retained)
        return super()._combine_output(stdout, stderr)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        identifier = uuid4().hex
        start = monotonic()
        self.streams = {}
        self.log.capture_event(
            "command_started",
            command_id=identifier,
            command=command,
            cwd=str(self.cwd),
            timeout=timeout,
        )
        try:
            result = super().execute(command, timeout=timeout)
        except BaseException as exc:
            self.log.capture_event(
                "command_finished",
                command_id=identifier,
                duration_seconds=monotonic() - start,
                outcome="interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                else "failed",
                **error_info(exc),
            )
            raise
        self.log.capture_event(
            "command_finished",
            command_id=identifier,
            duration_seconds=monotonic() - start,
            exit_code=result.exit_code,
            outcome="timed_out"
            if result.exit_code == 124 and result.output.startswith("Error: Command timed out")
            else "returned",
            truncated=result.truncated,
            output=result.output,
            **self.streams,
        )
        return result


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
            self.log.capture_event("model_request_failed", **error_info(exc))
            raise EzError(
                f"Model request failed ({type(exc).__name__}); inspect local events"
            ) from None

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[..., Any]) -> Any:
        # LangGraph may schedule a batch concurrently. Serialize worktree access so
        # edits/commands and checks do not race against each other.
        with self.lock:
            self.guard()
            call = request.tool_call
            self.log.begin_tool(call["id"])
            if call["name"] == "finish":
                last = next(
                    (m for m in reversed(request.state["messages"]) if isinstance(m, AIMessage)),
                    None,
                )
                if last and len(last.tool_calls) != 1:
                    return self.log.feedback(
                        call,
                        "finish must be called alone",
                    )
            if (
                call["name"] == "execute"
                and isinstance(call["args"].get("timeout"), (int, float))
                and call["args"]["timeout"] > self.config.timeout
            ):
                return self.log.feedback(
                    call,
                    "Command timeout exceeds configured limit",
                )
            try:
                result = handler(request)
            except (LowConfidence, GitStateChanged, AuditError):
                raise
            except (EzError, OSError, UnicodeError) as exc:
                result = self.log.feedback(call, safe_error(exc))
            if isinstance(result, ToolMessage) and result.status == "error":
                result = self.log.record_feedback(call, result)
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
    log = SessionLog(directory, audit)
    session_data: dict[str, Any] = {
        "round": round_number,
        "status": "running",
        "conflicts": paths,
        "runtime": "deepagents",
        "tool_calls": 0,
        "artifacts": str(directory.relative_to(audit.directory)),
    }
    session = audit.add_record("sessions", session_data)
    result: dict[str, Any] = {}
    decision: dict[str, Any] = {}
    # Initialization may fail before HEAD is pinned; capture the current Git state then.
    head = "HEAD"
    try:
        proposal = audit.record("reviews")
        session.update(
            proposal_id=proposal["id"],
            conflict_ids=proposal.get("conflict_ids", []),
            stop_id=proposal.get("stop_id"),
        )
        audit.create_directory(directory)
        audit.save()
        checks = WorktreeChecks(config)
        head = git.text("rev-parse", "HEAD")
        branch = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip() or None

        def guard() -> None:
            checks.guard(git, audit, head, branch)

        @tool
        def finish(rationale: str, confidence: float) -> str:
            """Finish ALL conflicts at this Git stop; call alone after editing and testing."""
            parsed = Decision.parse({"rationale": rationale, "confidence": confidence})
            proposed = {"rationale": parsed.rationale, "confidence": parsed.confidence}
            if parsed.confidence < config.min_confidence:
                session.update(proposed)
                session.update(status="low_confidence")
                audit.export_json(directory / "decision.json", proposed)
                raise LowConfidence("Agent confidence below threshold; worktree preserved")
            checks.verify(git, paths, baseline)
            decision.update(proposed)
            session.update(proposed)
            audit.export_json(directory / "decision.json", decision)
            return "Resolution verified. Git continuation belongs to git-ez."

        metadata = {
            "runtime": "deepagents",
            "versions": {
                package: version(package)
                for package in (
                    "deepagents",
                    "langchain-openai",
                    "langchain-ollama",
                    "langgraph-checkpoint",
                )
            },
            "checkpoints": {"storage": "memory", "resumable": False},
            "agent_files": "agent-files/",
            "provider": config.provider,
            "model": config.model,
            "model_context_tokens": config.model_context_tokens,
        }
        session.update(runtime_metadata=metadata)
        audit.export_json(directory / "runtime.json", metadata)
        baseline = checks.baseline(git)
        model = create_chat_model(config)
        # Use the public profile API to disable default delegation for this single-agent task.
        register_harness_profile(
            f"{config.provider}:{config.model}",
            HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
        )
        env = agent_environment(config)
        shell = JournalShell(
            log,
            config.tool_output_bytes,
            root_dir=git.path,
            virtual_mode=True,
            timeout=max(1, int(config.timeout)),
            env=env,
        )

        # Deep Agents offloads history/large results without polluting the project or git add -A.
        audit.create_directory(directory / "agent-files")
        backend = CompositeBackend(
            default=shell,
            routes={
                "/.ez-agent/": FilesystemBackend(
                    root_dir=directory / "agent-files", virtual_mode=True
                )
            },
            artifacts_root="/.ez-agent",
        )

        mission = {
            "operation": audit.view["operation"],
            "conflicts": paths,
            "worktree": str(git.path),
            "branch_sha": audit.view["before_oid"],
            "target_sha": audit.view["target_oid"],
            "head_sha": head,
            "validation_command": shlex.join(check) if check else None,
            "task": "Investigate and resolve this Git integration using your tools.",
        }
        if audit.view.get("interactive"):
            review = audit.view["reviews"][-1]
            mission["review_round"] = review["round"]
            mission["user_feedback"] = review["feedback"]
            mission["revision_instruction"] = (
                "Revise the current worktree proposal using user_feedback; do not restore "
                "the initial conflict markers. Explain checks performed and uncertainties."
            )
        if "onto_oid" in audit.view:
            mission["onto_sha"] = audit.view["onto_oid"]
        if "rebase_plan" in audit.view:
            mission["rebase_base_sha"] = audit.view["rebase_plan"]["base_oid"]
            mission["replay_boundary_sha"] = audit.view["rebase_plan"]["selection_oid"]
        session.update(mission=mission, invocation_started=True)
        audit.export_json(directory / "mission.json", mission)
        audit.save()
        agent = create_deep_agent(
            model=model,
            backend=backend,
            system_prompt=AGENT_PROMPT,
            tools=[finish],
            middleware=[
                IntegrationMiddleware(config, guard, log, lambda: bool(decision)),
                ModelCallLimitMiddleware(run_limit=config.agent_max_turns, exit_behavior="error"),
                ToolCallLimitMiddleware(
                    run_limit=config.agent_max_tool_calls, exit_behavior="error"
                ),
            ],
            checkpointer=InMemorySaver(),
        )
        # Traces stay local even if this machine has global LangSmith settings.
        with tracing_context(enabled=False):
            result = agent.invoke(
                {"messages": [{"role": "user", "content": json.dumps(mission)}]},
                config={
                    "configurable": {"thread_id": f"{audit.view['id']}-{round_number}"},
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
            session.update(
                status="interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                else "failed"
            )
        if isinstance(
            exc, (ModelCallLimitExceededError, ToolCallLimitExceededError, GraphRecursionError)
        ):
            log.capture_event("agent_error", **error_info(exc))
            raise EzError(
                f"Deep Agents limit reached ({type(exc).__name__}); inspect local events"
            ) from exc
        if not isinstance(exc, (EzError, OSError, KeyboardInterrupt, SystemExit)):
            log.capture_event("agent_error", **error_info(exc))
        raise
    finally:
        session.update(tool_calls=log.tools, model_calls=log.conflicts)
        folder = directory.relative_to(audit.directory).as_posix()
        audit.attempt(
            "session messages",
            lambda: audit.write_json(
                f"{folder}/messages.json", messages_to_dict(result.get("messages", log.messages))
            ),
            f"{folder}/messages.json",
        )
        log.capture_event("session_finished", status=session["status"])
        audit.attempt("agent files", lambda: audit.redact_agent_files(directory / "agent-files"))
        audit.attempt("session metadata", audit.save)
