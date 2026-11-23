"""Provider-neutral shapes shared by the model client and the agent loop.

A conversation is a list of plain dicts so it can be checkpointed as JSON and resumed:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id", "name", "args"}],
     "native": [...provider blocks...], "provider": "anthropic"}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "...", "is_error": False}

``native`` keeps the provider's own assistant blocks (thinking blocks with their signatures,
fallback blocks) so the next request to the same provider can pass them back unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Request:
    """One model call. ``model`` is ``provider:model`` (``anthropic:claude-opus-5-5``)."""

    model: str
    system: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)  # {name, description, input_schema}
    schema: dict[str, Any] | None = None  # JSON schema the final answer must follow
    schema_name: str = "answer"
    effort: str = "medium"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    timeout_s: float = 300.0


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    native: list[dict[str, Any]] | None = None
    provider: str = ""
    model: str = ""
    stop_reason: str = ""
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    priced: bool = True

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text, "provider": self.provider}
        if self.tool_calls:
            message["tool_calls"] = [{"id": c.id, "name": c.name, "args": c.args} for c in self.tool_calls]
        if self.native is not None:
            message["native"] = self.native
        return message


class ModelError(RuntimeError):
    """A model call that failed. ``retryable`` errors are retried by ``model.call``; a
    ``rate_limited`` one moves to the next key first; ``outage`` names a provider-wide refusal
    (no credits, every key refused) so a pass can stop instead of failing check by check."""

    def __init__(self, message: str, *, status: int = 0, retryable: bool = False,
                 rate_limited: bool = False, outage: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.outage = outage
