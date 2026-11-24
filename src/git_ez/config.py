# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Typed native ez.* Git settings."""

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import EzError
from .git import Git

# Git variable names are case-insensitive. Camel case is used only for readability.
GIT_SETTINGS = {
    "provider": "provider",
    "model": "model",
    "endpoint": "endpoint",
    "api_key": "apiKey",
    "api_key_env": "apiKeyEnv",
    "allow_insecure_http": "allowInsecureHttp",
    "ca_bundle": "caBundle",
    "timeout": "timeout",
    "min_confidence": "minConfidence",
    "max_file_bytes": "maxFileBytes",
    "max_rounds": "maxRounds",
    "validation_attempts": "validationAttempts",
    "model_context_tokens": "modelContextTokens",
    "model_max_output_tokens": "modelMaxOutputTokens",
    "agent_max_turns": "agentMaxTurns",
    "agent_max_tool_calls": "agentMaxToolCalls",
    "agent_max_context_bytes": "agentMaxContextBytes",
    "tool_output_bytes": "toolOutputBytes",
    "request_options": "requestOptions",
}


@dataclass(frozen=True)
class Config:
    provider: str = "openai"
    model: str = ""
    endpoint: str = "https://api.openai.com/v1/chat/completions"
    api_key: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    allow_insecure_http: bool = False
    ca_bundle: str | None = None
    timeout: float = 120
    min_confidence: float = 0.8
    max_file_bytes: int = 131072
    validation_attempts: int = 3
    max_rounds: int = 100
    model_context_tokens: int = 32768
    model_max_output_tokens: int = 8192
    agent_max_turns: int = 40
    agent_max_tool_calls: int = 100
    agent_max_context_bytes: int = 1_000_000
    tool_output_bytes: int = 32768
    request_options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_git(cls, repository: Path) -> "Config":
        """Read the effective ez.* settings using native Git scope and include rules."""
        git = Git(repository.resolve())
        result = git.run("config", "--null", "--get-regexp", r"^ez\.", check=False)
        if result.returncode not in (0, 1):
            raise EzError("Cannot read ez.* Git configuration")
        values: dict[str, str] = {}
        for entry in result.stdout.decode().split("\0"):
            if entry:
                key, _, value = entry.partition("\n")
                values[key.lower()] = value
        allowed = {f"ez.{key.lower()}" for key in GIT_SETTINGS.values()}
        if unknown := set(values) - allowed:
            raise EzError("Unknown Git configuration keys: " + ", ".join(sorted(unknown)))
        data: dict[str, Any] = {}
        defaults = cls()
        try:
            for name, suffix in GIT_SETTINGS.items():
                key = f"ez.{suffix}"
                if key.lower() not in values:
                    continue
                value = values[key.lower()]
                if isinstance(getattr(defaults, name), bool):
                    parsed = git.run("config", "--type=bool", "--get", key, check=False)
                    if parsed.returncode:
                        raise ValueError(f"{key} must be a Git boolean")
                    data[name] = parsed.stdout.strip() == b"true"
                elif name in ("timeout", "min_confidence"):
                    try:
                        data[name] = float(value)
                    except ValueError as exc:
                        raise ValueError(f"{key} must be a number") from exc
                elif isinstance(getattr(defaults, name), int):
                    parsed = git.run("config", "--type=int", "--get", key, check=False)
                    if parsed.returncode:
                        raise ValueError(f"{key} must be a Git integer")
                    data[name] = int(parsed.stdout)
                elif name == "request_options":
                    try:
                        data[name] = json.loads(value)
                    except ValueError as exc:
                        raise ValueError(f"{key} must contain valid JSON") from exc
                else:
                    data[name] = value
            if data.get("provider") == "ollama":
                data.setdefault("endpoint", "http://localhost:11434/api/chat")
                data.setdefault("api_key_env", "LOCAL_MODEL_API_KEY")
            if (
                "ez.cabundle" in values
                and isinstance(data.get("ca_bundle"), str)
                and data["ca_bundle"].strip()
            ):
                root = git.run("rev-parse", "--show-toplevel", check=False)
                base = Path(root.stdout.decode().strip()) if root.returncode == 0 else git.path
                bundle = Path(data["ca_bundle"]).expanduser()
                data["ca_bundle"] = str((base / bundle).resolve())
            config = cls(**data)
            config.validate()
            return config
        except (ValueError, TypeError) as exc:
            raise EzError(
                f"Invalid Git configuration: {exc}. "
                "Configure ez.model and the provider settings with git config."
            ) from exc

    def validate(self) -> None:
        if self.provider not in ("openai", "ollama"):
            raise ValueError("provider must be openai or ollama")
        for name in ("model", "endpoint", "api_key_env"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if self.api_key is not None and (
            not isinstance(self.api_key, str) or not self.api_key.strip()
        ):
            raise ValueError("api_key must be a non-empty string")
        if not self.model.strip():
            raise ValueError("model is required")
        if self.provider in ("openai", "ollama"):
            url = urlsplit(self.endpoint)
            local = url.hostname in ("localhost", "127.0.0.1", "::1")
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or (url.scheme == "http" and not local and not self.allow_insecure_http)
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "endpoint must use HTTPS; for a trusted HTTP server set "
                    "allow_insecure_http=true explicitly"
                )
        if not isinstance(self.allow_insecure_http, bool):
            raise ValueError("allow_insecure_http must be a boolean")
        if self.ca_bundle is not None:
            if not isinstance(self.ca_bundle, str) or not self.ca_bundle.strip():
                raise ValueError("ca_bundle must be a non-empty path to a CA bundle file")
            if not Path(self.ca_bundle).is_file():
                raise ValueError(f"ca_bundle file not found: {self.ca_bundle}")
        for name, minimum, maximum in (
            ("timeout", 0.1, 3600),
            ("min_confidence", 0, 1),
            ("max_file_bytes", 1, 10_000_000),
            ("max_rounds", 1, 10000),
            ("validation_attempts", 1, 1000),
            ("model_context_tokens", 4096, 2_000_000),
            ("model_max_output_tokens", 1, 128000),
            ("agent_max_turns", 1, 1000),
            ("agent_max_tool_calls", 1, 10000),
            ("agent_max_context_bytes", 4096, 50_000_000),
            ("tool_output_bytes", 1024, 1_000_000),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        for name in (
            "max_file_bytes",
            "max_rounds",
            "validation_attempts",
            "model_context_tokens",
            "model_max_output_tokens",
            "agent_max_turns",
            "agent_max_tool_calls",
            "agent_max_context_bytes",
            "tool_output_bytes",
        ):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be an integer")

        if self.model_max_output_tokens >= self.model_context_tokens:
            raise ValueError("model_max_output_tokens must be smaller than model_context_tokens")

        if not isinstance(self.request_options, dict):
            raise ValueError("request_options must be an object")
        reserved = {
            "model",
            "messages",
            "stream",
            "format",
            "response_format",
            "tools",
            "tool_choice",
        }
        if reserved & self.request_options.keys():
            raise ValueError("request_options cannot override model, messages or output format")
        try:
            json.dumps(self.request_options, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("request_options must contain finite JSON-compatible values") from exc

    def resolved_api_key(self) -> str:
        """Return the API key: the Git value first, then the environment variable."""
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env, "")

    def audit_dict(self) -> dict[str, Any]:
        """Serializable configuration safe to archive: the API key value is redacted."""
        data = asdict(self)
        data["api_key"] = "<redacted>" if self.api_key else None
        return data
