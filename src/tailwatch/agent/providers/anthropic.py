"""Claude through the official Anthropic SDK (the default provider).

The SDK's own retries are off (``max_retries=0``): ``agent.model.call`` owns retries and key
failover for every provider alike. Structured answers use ``output_config.format`` (forced
``tool_choice`` is rejected by current models), thinking is left adaptive and depth is set
with ``output_config.effort``. The system prompt carries a cache breakpoint, so the stable
instructions of a routine are read from cache on every call after the first. On models that
support it, a policy refusal is retried server-side on a fallback model
(``TAILWATCH_REFUSAL_FALLBACK=off`` turns that off).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from tailwatch.agent.types import ModelError, Reply, Request, ToolCall

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Pre-4.6 models take neither adaptive thinking nor effort.
NO_EFFORT_PREFIXES = ("claude-3", "claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5", "claude-opus-4-1")


def to_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral messages to Messages API messages. Consecutive tool results become one user
    message (splitting them teaches the model to stop calling tools in parallel)."""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"],
                     "content": str(message.get("content") or "")}
            if message.get("is_error"):
                block["is_error"] = True
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant":
            if message.get("provider") == "anthropic" and message.get("native"):
                out.append({"role": "assistant", "content": message["native"]})
                continue
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message["content"])})
            for call in message.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no text)"}]})
        else:
            content = message.get("content")
            out.append({"role": "user", "content": content if isinstance(content, list) else str(content or "")})
    return out


def params(request: Request, model: str, env: Mapping[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_tokens,
        "messages": to_messages(request.messages),
    }
    if request.system:
        body["system"] = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
    if request.tools:
        body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                          "input_schema": t.get("input_schema") or {"type": "object", "properties": {}}}
                         for t in request.tools]
    output_config: dict[str, Any] = {}
    if not model.startswith(NO_EFFORT_PREFIXES):
        output_config["effort"] = request.effort
    if request.schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": request.schema}
    if output_config:
        body["output_config"] = output_config
    return body


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    return block.model_dump(exclude_none=True, by_alias=True)


def parse(response: Any, provider: str) -> Reply:
    blocks = [_block_dict(b) for b in getattr(response, "content", None) or []]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    calls = [ToolCall(id=b["id"], name=b["name"], args=dict(b.get("input") or {}))
             for b in blocks if b.get("type") == "tool_use"]
    usage = getattr(response, "usage", None)
    reply = Reply(text=text, tool_calls=calls, native=blocks, provider=provider,
                  model=str(getattr(response, "model", "") or ""), stop_reason=str(getattr(response, "stop_reason", "") or ""))
    if usage is not None:
        reply.tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        reply.tokens_cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        reply.tokens_cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        reply.tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    return reply


class AnthropicAdapter:
    name = "anthropic"
    key_env = "ANTHROPIC_API_KEY"

    def __init__(self, client_factory: Any = None) -> None:
        self._factory = client_factory

    def _client(self, key: str, env: Mapping[str, str], timeout: float) -> Any:
        if self._factory is not None:
            return self._factory(key)
        import anthropic

        kwargs: dict[str, Any] = {"api_key": key, "max_retries": 0, "timeout": timeout}
        if env.get("ANTHROPIC_BASE_URL"):
            kwargs["base_url"] = env["ANTHROPIC_BASE_URL"]
        return anthropic.Anthropic(**kwargs)

    def send(self, request: Request, model: str, key: str, env: Mapping[str, str]) -> Reply:
        import anthropic

        client = self._client(key, env, request.timeout_s)
        body = params(request, model, env)
        use_fallback = model in FALLBACK_MODELS and str(env.get("TAILWATCH_REFUSAL_FALLBACK", "on")).lower() != "off" \
            and not env.get("ANTHROPIC_BASE_URL")
        try:
            if use_fallback:
                response = client.beta.messages.create(**body, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                response = client.messages.create(**body)
        except anthropic.RateLimitError as error:
            raise ModelError(f"anthropic rate limited: {error}", status=429, retryable=True, rate_limited=True) from error
        except anthropic.AuthenticationError as error:
            raise ModelError("anthropic rejected the API key (401)", status=401, outage="key rejected") from error
        except anthropic.PermissionDeniedError as error:
            raise ModelError(f"anthropic refused the request (403): {error}", status=403, outage="permission denied") from error
        except anthropic.BadRequestError as error:
            text = str(error)
            outage = "out of credits" if "credit balance" in text.lower() else ""
            raise ModelError(f"anthropic bad request (400): {text[:400]}", status=400, outage=outage) from error
        except anthropic.APIStatusError as error:
            status = int(getattr(error, "status_code", 0) or 0)
            raise ModelError(f"anthropic HTTP {status}: {str(error)[:300]}", status=status,
                             retryable=status in (408, 409, 425) or status >= 500) from error
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as error:
            raise ModelError(f"anthropic connection: {error}", retryable=True) from error
        reply = parse(response, self.name)
        reply.model = reply.model or model
        if reply.stop_reason == "refusal":
            raise ModelError("the model declined this request (stop_reason refusal)", status=200)
        if request.schema is not None and not reply.tool_calls:
            try:
                json.loads(reply.text)
            except ValueError:
                if reply.stop_reason == "max_tokens":
                    raise ModelError(f"the answer reached the output limit of {request.max_tokens} tokens") from None
        return reply
