# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicit local model configuration shared by Deep Agents and the compatibility probe."""

from typing import Any

from .config import Config
from .errors import EzError


def base_url(config: Config) -> str:
    """Normalize provider base URLs and explicit chat endpoint URLs."""
    endpoint = config.endpoint.rstrip("/")
    suffix = "/api/chat" if config.provider == "ollama" else "/chat/completions"
    if endpoint.endswith(suffix):
        endpoint = endpoint[: -len(suffix)]
    if endpoint.endswith("/responses"):
        raise EzError("This adapter requires Chat Completions, not the Responses endpoint")
    return endpoint


def create_chat_model(config: Config) -> Any:
    import httpx
    from langchain_core.language_models import ModelProfile
    from langchain_ollama import ChatOllama
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    profile: ModelProfile = {
        "max_input_tokens": config.model_context_tokens - config.model_max_output_tokens,
        "max_output_tokens": config.model_max_output_tokens,
        "tool_calling": True,
    }
    key = config.resolved_api_key()
    if config.provider == "openai":
        return ChatOpenAI(
            model=config.model,
            base_url=base_url(config),
            api_key=SecretStr(key or "local-no-key"),
            timeout=config.timeout,
            max_retries=0,
            streaming=False,
            stream_usage=False,
            use_responses_api=False,
            max_completion_tokens=config.model_max_output_tokens,
            extra_body=config.request_options or None,
            profile=profile,
            http_client=httpx.Client(
                follow_redirects=False,
                timeout=config.timeout,
                verify=config.ca_bundle or True,
            ),
        )
    if config.provider == "ollama":
        options = dict(config.request_options)
        generation = options.pop("options", {})
        if not isinstance(generation, dict):
            raise EzError("Ollama request_options.options must be a table")
        if "think" in options:
            options["reasoning"] = options.pop("think")
        options = {
            "num_ctx": config.model_context_tokens,
            "num_predict": config.model_max_output_tokens,
            **generation,
            **options,
        }
        allowed = set(ChatOllama.model_fields) - {
            "model",
            "base_url",
            "profile",
            "client_kwargs",
            "sync_client_kwargs",
            "async_client_kwargs",
            "callbacks",
            "format",
            "validate_model_on_init",
        }
        if unknown := set(options) - allowed:
            raise EzError(f"Unsupported Ollama generation options: {', '.join(sorted(unknown))}")
        return ChatOllama(
            model=config.model,
            base_url=base_url(config),
            profile=profile,
            client_kwargs={
                "timeout": config.timeout,
                "follow_redirects": False,
                "verify": config.ca_bundle or True,
                "headers": {"Authorization": f"Bearer {key}"} if key else {},
            },
            **options,
        )
    raise EzError("Deep Agents requires an openai-compatible or ollama provider")
