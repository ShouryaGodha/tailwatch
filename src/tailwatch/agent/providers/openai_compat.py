"""OpenAI Chat Completions and everything that speaks it: OpenAI, Azure OpenAI (v1 API),
OpenRouter, Ollama, vLLM, LM Studio. Raw HTTP with an injectable ``post`` (the test seam)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from tailwatch.agent.types import ModelError, Reply, Request, ToolCall

Post = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, dict[str, Any]]]

BASES = {
    "openai": ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "openrouter": ("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    "ollama": ("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    "openai_compatible": ("OPENAI_COMPATIBLE_BASE_URL", ""),
}
KEYS = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "ollama": "OLLAMA_API_KEY",
        "openai_compatible": "OPENAI_COMPATIBLE_API_KEY", "azure": "AZURE_OPENAI_API_KEY"}
NEEDS_KEY = {"openai", "openrouter", "azure"}


def http_post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    import httpx

    try:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException as error:
        raise ModelError(f"timeout calling {url}", retryable=True) from error
    except httpx.HTTPError as error:
        raise ModelError(f"connection error calling {url}: {error}", retryable=True) from error
    try:
        data = response.json()
    except ValueError:
        data = {"error": {"message": response.text[:300]}}
    return response.status_code, data


def to_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": str(message.get("content") or "")})
        elif role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": message.get("content") or None}
            if message.get("tool_calls"):
                item["tool_calls"] = [{"id": c["id"], "type": "function",
                                       "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                      for c in message["tool_calls"]]
            out.append(item)
        else:
            out.append({"role": "user", "content": str(message.get("content") or "")})
    return out


class OpenAICompatAdapter:
    def __init__(self, provider: str, post: Post | None = None) -> None:
        self.name = provider
        self.key_env = KEYS.get(provider, "OPENAI_COMPATIBLE_API_KEY")
        self._post = post or http_post

    def url(self, model: str, env: Mapping[str, str]) -> str:
        if self.name == "azure":
            endpoint = str(env.get("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
            if not endpoint:
                raise ModelError("AZURE_OPENAI_ENDPOINT is not set", outage="no credential")
            return f"{endpoint}/openai/v1/chat/completions"
        env_name, default = BASES.get(self.name, BASES["openai_compatible"])
        base = str(env.get(env_name) or default).rstrip("/")
        if not base:
            raise ModelError(f"{env_name} is not set", outage="no credential")
        return f"{base}/chat/completions"

    def body(self, request: Request, model: str) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": to_messages(request.system, request.messages),
                                "max_completion_tokens" if self.name in ("openai", "azure") else "max_tokens": request.max_tokens}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
                             for t in request.tools]
            body["parallel_tool_calls"] = False
        if request.schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": request.schema_name, "schema": request.schema, "strict": False}}
        if self.name in ("openai", "azure") and request.effort in ("low", "medium", "high"):
            body["reasoning_effort"] = request.effort
        return body

    def send(self, request: Request, model: str, key: str, env: Mapping[str, str]) -> Reply:
        headers = {"content-type": "application/json"}
        if key:
            headers["api-key" if self.name == "azure" else "authorization"] = key if self.name == "azure" else f"Bearer {key}"
        status, data = self._post(self.url(model, env), headers, self.body(request, model), request.timeout_s)
        if status >= 400:
            message = str((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error"))[:300]
            outage = {401: "key rejected", 402: "out of credits", 403: "permission denied"}.get(status, "")
            if status == 429 and "quota" in message.lower():
                outage = "out of credits"
            raise ModelError(f"{self.name} HTTP {status}: {message}", status=status,
                             retryable=status in (408, 409, 425, 429) or status >= 500, rate_limited=status == 429, outage=outage)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {"_unparsed": function.get("arguments")}
            calls.append(ToolCall(id=str(call.get("id")), name=str(function.get("name")), args=args if isinstance(args, dict) else {}))
        usage = data.get("usage") or {}
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        reply = Reply(text=str(message.get("content") or ""), tool_calls=calls, provider=self.name,
                      model=str(data.get("model") or model), stop_reason=str(choice.get("finish_reason") or ""),
                      tokens_in=max(0, int(usage.get("prompt_tokens") or 0) - cached), tokens_cached=cached,
                      tokens_out=int(usage.get("completion_tokens") or 0))
        if reply.stop_reason == "content_filter":
            raise ModelError("the provider's content filter stopped the answer", status=200)
        return reply
