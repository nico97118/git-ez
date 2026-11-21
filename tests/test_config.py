# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Configuration and decisions have strict, provider-neutral contracts."""

import json
from pathlib import Path

import pytest

from git_ez.config import Config
from git_ez.conflicts import Decision
from git_ez.errors import EzError


def test_valid_decision_explains_resolution():
    assert Decision.parse({"rationale": "Combined both intents", "confidence": 0.9}) == Decision(
        "Combined both intents", 0.9
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"rationale": "why"},
        {"rationale": "", "confidence": 1},
        {"rationale": "why", "confidence": True},
        {"rationale": "why", "confidence": float("nan")},
        {"rationale": "why", "confidence": -1},
        {"rationale": "why", "confidence": 1.1},
        {"rationale": "why", "confidence": 1, "extra": "field"},
    ],
)
def test_invalid_decisions_are_rejected(value):
    with pytest.raises(EzError):
        Decision.parse(value)


@pytest.mark.parametrize(
    "settings",
    [
        {"model": ""},
        {"model": "test", "min_confidence": 2},
        {"model": "test", "timeout": "forever"},
        {"model": "test", "max_rounds": 1.5},
        {"model": "test", "endpoint": "http://example.com/v1"},
        {"model": "test", "endpoint": "https://user:pass@example.com/v1"},
        {"model": "test", "api_key": True},
        {"model": "test", "api_key": ""},
        {"model": "test", "provider": "command"},
        {"model": "test", "ca_bundle": "/nonexistent/ca.pem"},
    ],
)
def test_invalid_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        Config(**settings).validate()


@pytest.mark.parametrize("env_key", [None, "env-key"])
@pytest.mark.parametrize("git_key", [None, "git-key"])
def test_git_key_takes_precedence_over_environment(monkeypatch, env_key, git_key):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if env_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", env_key)
    assert Config(model="test", api_key=git_key).resolved_api_key() == (git_key or env_key or "")


def test_audit_redacts_api_key():
    data = Config(model="test", api_key="top-secret").audit_dict()
    assert data["api_key"] == "<redacted>"
    assert "top-secret" not in json.dumps(data)


@pytest.mark.parametrize(
    "options",
    [
        {"model": "override"},
        {"stream": True},
        {"format": "text"},
        {"temperature": float("nan")},
        {"value": Path("/tmp")},
    ],
)
def test_invalid_request_options_are_rejected(options):
    with pytest.raises(ValueError):
        Config(model="test", request_options=options).validate()
