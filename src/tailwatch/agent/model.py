"""The one model client. Every model call in tailwatch goes through ``call``.

- ``provider:model`` strings (``anthropic:claude-opus-5-5``, ``openai:gpt-5``,
  ``ollama:llama3.3``); a bare model name uses ``TAILWATCH_PROVIDER`` (default anthropic).
- Retries: a retryable error (429, 408/409/425, 5xx, timeout, connection) is retried twice with
  backoff; 400/401/403/404/422 never are.
- Key failover: a provider's key variable may hold several keys separated by commas, or
  ``<KEY>_1`` .. ``<KEY>_20``. A rate limit moves to the next key before any wait.
- Usage and real cost from the price table. An unknown model is called and marked unpriced.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from tailwatch.agent import pricing
from tailwatch.agent.types import ModelError, Reply, Request

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "anthropic:claude-opus-5-5"
RETRIES = 2
BACKOFF_S = (2.0, 6.0)
PROVIDERS = ("anthropic", "openai", "azure", "openrouter", "ollama", "openai_compatible", "scripted")

_adapters: dict[str, Any] = {}


def _sleep(seconds: float) -> None:  # patched to a no-op in tests
    time.sleep(seconds)


def register_adapter(name: str, adapter: Any) -> None:
    """Install an adapter for a provider name (tests and the demo install ``scripted``)."""
    _adapters[name] = adapter


def adapter_for(provider: str) -> Any:
    if provider in _adapters:
        return _adapters[provider]
    if provider == "anthropic":
        from tailwatch.agent.providers.anthropic import AnthropicAdapter

        _adapters[provider] = AnthropicAdapter()
    elif provider in ("openai", "azure", "openrouter", "ollama", "openai_compatible"):
        from tailwatch.agent.providers.openai_compat import OpenAICompatAdapter

        _adapters[provider] = OpenAICompatAdapter(provider)
    else:
        raise ModelError(f"unknown model provider {provider!r} (known: {', '.join(PROVIDERS)})")
    return _adapters[provider]


def split_model(model: str, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    env = env if env is not None else os.environ
    if ":" in model:
        provider, name = model.split(":", 1)
        if provider in PROVIDERS or provider in _adapters:
            return provider, name
    return str(env.get("TAILWATCH_PROVIDER") or DEFAULT_PROVIDER), model


def keys_for(adapter: Any, env: Mapping[str, str]) -> list[str]:
    name = getattr(adapter, "key_env", "")
    if not name:
        return [""]
    keys: list[str] = []
    for value in [env.get(name, "")] + [env.get(f"{name}_{n}", "") for n in range(1, 21)]:
        keys.extend(k.strip() for k in str(value or "").split(",") if k.strip())
    keys = list(dict.fromkeys(keys))
    if not keys and getattr(adapter, "name", "") in ("ollama", "openai_compatible", "scripted"):
        return [""]
    return keys


def call(request: Request, env: Mapping[str, str] | None = None, *,
         prices: Mapping[str, tuple[float, float, float]] | None = None,
         on_retry: Callable[[ModelError, int], None] | None = None) -> Reply:
    env = env if env is not None else os.environ
    provider, model = split_model(request.model, env)
    adapter = adapter_for(provider)
    keys = keys_for(adapter, env)
    if not keys:
        raise ModelError(f"no API key for {provider}: set {adapter.key_env}", outage="no credential")
    key_index = 0
    attempt = 0
    while True:
        try:
            reply = adapter.send(request, model, keys[key_index], env)
            break
        except ModelError as error:
            if error.rate_limited and key_index + 1 < len(keys):
                key_index += 1
                continue
            if error.status == 401 and key_index + 1 < len(keys):
                key_index += 1
                continue
            if not error.retryable or attempt >= RETRIES:
                if error.rate_limited and not error.outage and len(keys) > 1:
                    error.outage = "every key rate limited"
                raise
            if on_retry:
                on_retry(error, attempt)
            _sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1
    reply.provider = reply.provider or provider
    reply.cost_usd, reply.priced = pricing.cost_of(
        provider, model, tokens_in=reply.tokens_in, tokens_cached=reply.tokens_cached,
        tokens_cache_write=reply.tokens_cache_write, tokens_out=reply.tokens_out, extra=prices)
    return reply
