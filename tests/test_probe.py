# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise actual LangChain HTTP adapters with a deterministic HTTP transport."""

import json
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from openai import APIConnectionError

from git_ez.config import Config
from git_ez.llm import base_url, create_chat_model
from git_ez.probe import probe


def run_probe(mocker, monkeypatch, tmp_path: Path, provider="openai", broken_tools=False):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        marker = re.search("ez-[0-9a-f]{32}", json.dumps(payload)).group()
        index = len(requests)
        message = {"role": "assistant", "content": marker}
        reason = "stop"
        if index == 2 and (not broken_tools):
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "probe-call",
                        "type": "function",
                        "function": {
                            "name": "compatibility_echo",
                            "arguments": {"value": marker}
                            if provider == "ollama"
                            else json.dumps({"value": marker}),
                        },
                    }
                ],
            }
            reason = "tool_calls"
        if provider == "ollama":
            body = {"model": "test-qwen", "message": message, "done": True, "done_reason": "stop"}
        else:
            body = {
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-qwen",
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
            }
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(respond), base_url="http://localhost:11434")
    config = Config(
        model="test-qwen",
        provider=provider,
        api_key_env="EZ_PROBE_TEST_KEY",
        endpoint="http://localhost:11434/api/chat"
        if provider == "ollama"
        else "http://localhost:8000/v1/chat/completions",
    )
    folder = tmp_path / "case-1"
    folder.mkdir()
    output = Path(folder) / "probe.json"
    monkeypatch.setenv("EZ_PROBE_TEST_KEY", "not-to-be-archived")
    model = create_chat_model(config)
    owner = model.root_client if provider == "openai" else model._client
    owner._client.close()
    owner._client = client
    mocker.patch("git_ez.probe.create_chat_model", return_value=model)
    report = probe(config, output)
    saved = output.read_text()
    assert "not-to-be-archived" not in saved
    assert json.loads(saved)["status"] == report["status"]
    client.close()
    return (report, requests)


def test_real_compatible_sdk_three_step_roundtrip(monkeypatch, mocker, tmp_path: Path):
    report, requests = run_probe(mocker, monkeypatch, tmp_path)
    assert report["status"] == "compatible", report
    assert len(requests) == 3
    assert requests[1]["tools"][0]["function"]["name"] == "compatibility_echo"
    assert requests[2]["messages"][-1]["role"] == "tool"
    assert requests[2]["messages"][-1]["tool_call_id"] == "probe-call"


def test_real_ollama_sdk_three_step_roundtrip(monkeypatch, mocker, tmp_path: Path):
    report, requests = run_probe(mocker, monkeypatch, tmp_path, "ollama")
    assert report["status"] == "compatible", report
    assert len(requests) == 3
    assert all(p["model"] == "test-qwen" for p in requests)


def test_plain_chat_success_does_not_prove_tool_compatibility(monkeypatch, mocker, tmp_path: Path):
    report, requests = run_probe(mocker, monkeypatch, tmp_path, broken_tools=True)
    assert report["status"] == "failed"
    assert report["checks"][0]["ok"]
    assert not report["checks"][1]["ok"]
    assert len(requests) == 2


def test_chat_model_trusts_configured_ca_bundle_only(https_server):
    body = json.dumps(
        {
            "id": "chat-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-qwen",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "pong"},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode()
    url, bundle = https_server(body)
    config = Config(
        model="test-qwen",
        endpoint=url + "/v1",
        api_key_env="EZ_TLS_TEST_KEY",
        ca_bundle=str(bundle),
    )
    model = create_chat_model(config)
    try:
        assert model.invoke("ping").content == "pong"
    finally:
        model.http_client.close()
    default = create_chat_model(Config(model="test-qwen", endpoint=url + "/v1"))
    try:
        with pytest.raises(APIConnectionError):
            default.invoke("ping")
    finally:
        default.http_client.close()


def test_ollama_client_kwargs_carry_ca_bundle(ca_bundle: Path):
    config = Config(
        provider="ollama",
        model="test-qwen",
        endpoint="http://localhost:11434",
        ca_bundle=str(ca_bundle),
    )
    model = create_chat_model(config)
    try:
        assert model.client_kwargs["verify"] == str(ca_bundle)
    finally:
        model._client._client.close()


def test_full_and_base_urls_are_supported():
    config = Config(model="test", endpoint="http://localhost:8000/v1/chat/completions")
    assert base_url(config) == "http://localhost:8000/v1"
    assert (
        base_url(replace(config, endpoint="http://localhost:8000/v1/"))
        == "http://localhost:8000/v1"
    )
    config = replace(config, provider="ollama", endpoint="http://localhost:11434/api/chat")
    assert base_url(config) == "http://localhost:11434"
