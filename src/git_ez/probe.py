# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Probe the actual SDK/model protocol without opening a repository or running commands."""

from pathlib import Path
from typing import Any
from uuid import uuid4

from .audit import now, write_json
from .config import Config
from .errors import EzError
from .llm import base_url, create_chat_model


def probe(config: Config, output: Path | None = None) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage, ToolMessage, messages_to_dict
    from langsmith import tracing_context

    marker = "ez-" + uuid4().hex
    schema = {
        "type": "function",
        "function": {
            "name": "compatibility_echo",
            "description": "Echo a harmless compatibility marker.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }
    report: dict[str, Any] = {
        "created_at": now(),
        "provider": config.provider,
        "model": config.model,
        "base_url": base_url(config),
        "status": "failed",
        "checks": [],
        "note": "Only protocol compatibility is tested; no Git or filesystem tools are exposed.",
    }
    if output is not None:
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
    try:
        model = create_chat_model(config)
        with tracing_context(enabled=False):
            message = model.invoke([HumanMessage(content=f"Reply with exactly {marker}")])
            report["checks"].append(
                {
                    "name": "chat",
                    "ok": marker in str(message.content),
                    "response": messages_to_dict([message]),
                }
            )
            if output is not None:
                write_json(output, report)
            if not report["checks"][-1]["ok"]:
                raise EzError("Model did not return the requested chat marker")
            bound = model.bind_tools([schema])
            initial = HumanMessage(
                content=f"Call compatibility_echo with value {marker}. "
                "After its result, reply with exactly the returned value."
            )
            call = bound.invoke([initial])
            calls = call.tool_calls
            ok = (
                len(calls) == 1
                and calls[0]["name"] == "compatibility_echo"
                and calls[0]["args"] == {"value": marker}
                and bool(calls[0].get("id"))
            )
            report["checks"].append(
                {"name": "tool_call", "ok": ok, "response": messages_to_dict([call])}
            )
            if output is not None:
                write_json(output, report)
            if not ok:
                raise EzError("Server/model did not produce the expected native tool call")
            final = bound.invoke(
                [
                    initial,
                    call,
                    ToolMessage(
                        content=marker, tool_call_id=calls[0]["id"], name="compatibility_echo"
                    ),
                ]
            )
            ok = not final.tool_calls and marker in str(final.content)
            report["checks"].append(
                {"name": "tool_result_roundtrip", "ok": ok, "response": messages_to_dict([final])}
            )
            if not ok:
                raise EzError("Model did not consume the tool result successfully")
            report["status"] = "compatible"
    except Exception as exc:
        # Keep credentials, HTTP bodies and headers out of diagnostics.
        report["error"] = {
            "type": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
        }
        if isinstance(exc, EzError):
            report["error"]["message"] = str(exc)
    finally:
        if output is not None:
            write_json(output, report)
    return report
