"""Native Git configuration works independently of the git-ez checkout and TOML files."""

import json

import pytest

from git_ez.config import Config
from git_ez.errors import EzError
from git_ez.git import Git


@pytest.fixture
def git(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git = Git(root)
    git.run("init", "-b", "main")
    return git


def test_native_settings_have_typed_values_and_repository_relative_ca(git):
    bundle = git.path / "ca.pem"
    bundle.write_text("path validation only")
    settings = {
        "model": "generic-model",
        "endpoint": "http://internal.example/v1",
        "allowInsecureHttp": "yes",
        "caBundle": "ca.pem",
        "timeout": "12.5",
        "minConfidence": "0.9",
        "maxFileBytes": "64k",
        "requestOptions": json.dumps({"chat_template_kwargs": {"enable_thinking": False}}),
    }
    for key, value in settings.items():
        git.run("config", f"ez.{key}", value)
    subdir = git.path / "src"
    subdir.mkdir()
    config = Config.from_git(subdir)
    assert config.model == "generic-model"
    assert config.allow_insecure_http
    assert config.ca_bundle == str(bundle)
    assert config.timeout == 12.5
    assert config.min_confidence == 0.9
    assert config.max_file_bytes == 65536
    assert config.request_options == {"chat_template_kwargs": {"enable_thinking": False}}


def test_git_scopes_override_individual_values(git, tmp_path, monkeypatch):
    global_file = tmp_path / "global.gitconfig"
    git.run("config", "--file", str(global_file), "ez.model", "global-model")
    git.run("config", "--file", str(global_file), "ez.endpoint", "https://models.example/v1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_file))
    assert Config.from_git(git.path).model == "global-model"
    git.run("config", "ez.model", "local-model")
    config = Config.from_git(git.path)
    assert config.model == "local-model"
    assert config.endpoint == "https://models.example/v1"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "ez.model")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "runtime-model")
    assert Config.from_git(git.path).model == "runtime-model"


def test_git_token_and_environment_fallback_are_redacted(git, monkeypatch):
    git.run("config", "ez.model", "generic-model")
    git.run("config", "ez.apiKeyEnv", "EZ_NATIVE_TEST_KEY")
    monkeypatch.setenv("EZ_NATIVE_TEST_KEY", "fake-env-key")
    config = Config.from_git(git.path)
    assert config.resolved_api_key() == "fake-env-key"
    assert "fake-env-key" not in json.dumps(config.audit_dict())
    git.run("config", "ez.apiKey", "fake-git-key")
    config = Config.from_git(git.path)
    assert config.resolved_api_key() == "fake-git-key"
    assert config.audit_dict()["api_key"] == "<redacted>"
    assert "fake-git-key" not in json.dumps(config.audit_dict())


def test_ollama_native_defaults_are_preserved(git):
    git.run("config", "ez.provider", "ollama")
    git.run("config", "ez.model", "installed-model")
    config = Config.from_git(git.path)
    assert config.endpoint == "http://localhost:11434/api/chat"
    assert config.api_key_env == "LOCAL_MODEL_API_KEY"


@pytest.mark.parametrize(
    "key,value",
    [
        ("unknown", "value"),
        ("allowInsecureHttp", "sometimes"),
        ("maxRounds", "1.5"),
        ("timeout", "forever"),
        ("command", '["python", 1]'),
        ("requestOptions", "not-json"),
        ("requestOptions", "[]"),
    ],
)
def test_invalid_native_settings_are_rejected_without_echoing_values(git, key, value):
    git.run("config", "ez.model", "generic-model")
    git.run("config", f"ez.{key}", value)
    with pytest.raises(EzError) as error:
        Config.from_git(git.path)
    assert value not in str(error.value)


@pytest.mark.parametrize("key", ["config", "resolver", "command"])
def test_unknown_configuration_keys_are_rejected(git, key):
    git.run("config", "ez.model", "model")
    git.run("config", f"ez.{key}", "unused")
    with pytest.raises(EzError, match="Unknown Git configuration"):
        Config.from_git(git.path)


def test_native_settings_do_not_implicitly_load_project_env(git, monkeypatch):
    git.run("config", "ez.model", "generic-model")
    git.run("config", "ez.apiKeyEnv", "EZ_NATIVE_NO_DOTENV")
    (git.path / ".env").write_text("EZ_NATIVE_NO_DOTENV=project-value\n")
    monkeypatch.delenv("EZ_NATIVE_NO_DOTENV", raising=False)
    monkeypatch.chdir(git.path)
    assert Config.from_git(git.path).resolved_api_key() == ""


def test_native_configuration_can_be_read_outside_a_repository(tmp_path, monkeypatch):
    global_file = tmp_path / "global.gitconfig"
    git = Git(tmp_path)
    git.run("config", "--file", str(global_file), "ez.model", "global-model")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_file))
    assert Config.from_git(tmp_path).model == "global-model"


def test_standard_git_configuration_example_can_be_included(git):
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "examples/ez.gitconfig"
    git.run("config", "include.path", str(example))
    config = Config.from_git(git.path)
    assert config.model == "exact-model-id-on-your-server"
    assert config.max_file_bytes == 262144
