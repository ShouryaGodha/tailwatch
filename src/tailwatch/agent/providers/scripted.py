"""A provider that answers from a script: the test seam and the engine of ``tailwatch demo``.

``ScriptedAdapter(answer)`` calls ``answer(request) -> Reply | dict | str`` for every request.
A dict is a JSON answer, a str is text, and a ``Reply`` is used as it is (tool calls included).
``scripted(*turns)`` replays a fixed list of turns in order.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from tailwatch.agent.types import ModelError, Reply, Request, ToolCall


def turn(text: str = "", *calls: tuple[str, dict[str, Any]], tokens: tuple[int, int] = (1000, 200)) -> Reply:
    return Reply(text=text, tool_calls=[ToolCall(id=f"call_{i}_{name}", name=name, args=args)
                                        for i, (name, args) in enumerate(calls)],
                 provider="scripted", model="scripted", tokens_in=tokens[0], tokens_out=tokens[1])


def as_reply(value: Any) -> Reply:
    if isinstance(value, Reply):
        return value
    if isinstance(value, BaseException):
        raise value
    if isinstance(value, (dict, list)):
        return turn(json.dumps(value))
    return turn(str(value))


class ScriptedAdapter:
    name = "scripted"
    key_env = ""

    def __init__(self, answer: Callable[[Request], Any]) -> None:
        self.answer = answer
        self.requests: list[Request] = []

    def send(self, request: Request, model: str, key: str, env: Mapping[str, str]) -> Reply:
        self.requests.append(request)
        reply = as_reply(self.answer(request))
        reply.provider, reply.model = "scripted", model
        return reply


def scripted(*turns: Any) -> ScriptedAdapter:
    queue = list(turns)

    def answer(_request: Request) -> Any:
        if not queue:
            raise ModelError("the script ran out of turns")
        return queue.pop(0)

    return ScriptedAdapter(answer)
