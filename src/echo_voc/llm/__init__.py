from __future__ import annotations

import os
from dataclasses import dataclass

from echo_voc.errors import ConfigError
from echo_voc.llm.base import LLM


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    kind: str  # "openai" (OpenAI-compatible) | "anthropic"
    default_model: str
    api_key_env: str | None
    base_url: str | None = None
    json_mode: bool = True


PROVIDERS: dict[str, ProviderPreset] = {
    p.name: p
    for p in (
        ProviderPreset(
            "deepseek", "openai", "deepseek-flash", "DEEPSEEK_API_KEY", "https://api.deepseek.com"
        ),
        ProviderPreset("openai", "openai", "gpt-5-mini", "OPENAI_API_KEY"),
        ProviderPreset("anthropic", "anthropic", "claude-sonnet-5", "ANTHROPIC_API_KEY"),
        ProviderPreset(
            "qwen",
            "openai",
            "qwen-plus",
            "DASHSCOPE_API_KEY",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        ProviderPreset(
            "moonshot", "openai", "kimi-latest", "MOONSHOT_API_KEY", "https://api.moonshot.cn/v1"
        ),
        ProviderPreset(
            "gemini",
            "openai",
            "gemini-2.5-flash",
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        ),
        ProviderPreset(
            "openrouter",
            "openai",
            "deepseek/deepseek-flash",
            "OPENROUTER_API_KEY",
            "https://openrouter.ai/api/v1",
        ),
        ProviderPreset("ollama", "openai", "qwen3", None, "http://localhost:11434/v1"),
        # Generic: point ECHO_BASE_URL / ECHO_API_KEY / ECHO_MODEL at any compatible server.
        ProviderPreset("custom", "openai", "", "ECHO_API_KEY"),
    )
}


def create_llm(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLM:
    """Build an LLM client. Explicit args win over ECHO_* env vars, which win over presets."""
    name = (provider or os.getenv("ECHO_PROVIDER") or "deepseek").lower()
    preset = PROVIDERS.get(name)
    if preset is None:
        raise ConfigError(f"Unknown provider '{name}'. Available: {', '.join(PROVIDERS)}")

    model = model or os.getenv("ECHO_MODEL") or preset.default_model
    base_url = base_url or os.getenv("ECHO_BASE_URL") or preset.base_url
    api_key = (
        api_key
        or (os.getenv(preset.api_key_env) if preset.api_key_env else None)
        or os.getenv("ECHO_API_KEY")
    )
    if not model:
        raise ConfigError(f"Provider '{name}' needs a model: pass --model or set ECHO_MODEL")
    if preset.kind == "openai" and name == "custom" and not base_url:
        raise ConfigError("Provider 'custom' needs ECHO_BASE_URL (or --base-url)")
    if not api_key:
        if preset.api_key_env is None:
            api_key = "not-needed"  # local servers such as Ollama ignore the key
        else:
            raise ConfigError(f"Missing API key: set {preset.api_key_env} (or ECHO_API_KEY)")

    if preset.kind == "anthropic":
        from echo_voc.llm.anthropic import AnthropicLLM

        return AnthropicLLM(model=model, api_key=api_key, base_url=base_url)

    from echo_voc.llm.openai_compat import OpenAICompatLLM

    return OpenAICompatLLM(
        provider=name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        # Reasoning models (o-series, gpt-5*) reject custom temperature.
        temperature=None if name == "openai" else 0.2,
        supports_json_mode=preset.json_mode,
    )


__all__ = ["LLM", "PROVIDERS", "ProviderPreset", "create_llm"]
