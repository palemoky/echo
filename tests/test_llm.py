from __future__ import annotations

import pytest

from echo_voc.errors import ConfigError
from echo_voc.llm import create_llm
from echo_voc.llm.anthropic import AnthropicLLM
from echo_voc.llm.openai_compat import OpenAICompatLLM


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "ECHO_PROVIDER",
        "ECHO_MODEL",
        "ECHO_BASE_URL",
        "ECHO_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_is_deepseek(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk")
    llm = create_llm()
    assert isinstance(llm, OpenAICompatLLM)
    assert (llm.provider, llm.model) == ("deepseek", "deepseek-flash")
    assert str(llm._client.base_url).startswith("https://api.deepseek.com")


def test_anthropic_and_overrides(monkeypatch):
    monkeypatch.setenv("ECHO_MODEL", "claude-opus-5")
    llm = create_llm("anthropic", api_key="k")
    assert isinstance(llm, AnthropicLLM) and llm.model == "claude-opus-5"


def test_missing_key():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        create_llm("openai")


def test_ollama_needs_no_key():
    assert create_llm("ollama").model == "qwen3"


def test_custom_requires_base_url_and_model(monkeypatch):
    with pytest.raises(ConfigError):
        create_llm("custom", api_key="k")
    llm = create_llm("custom", model="m", api_key="k", base_url="http://localhost:8000/v1")
    assert llm.model == "m"


def test_unknown_provider():
    with pytest.raises(ConfigError):
        create_llm("nope")
