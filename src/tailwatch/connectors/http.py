"""HTTP for connectors, with one seam for tests: ``Http(transport=...)`` takes a function that
answers a ``Call`` with a ``Response``, so every connector test runs on recorded fixtures."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tailwatch import __version__

USER_AGENT = f"tailwatch/{__version__} (+https://github.com/ShouryaGodha/tailwatch)"


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    params: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    json: Any = None


@dataclass
class Response:
    status: int
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""
    url: str = ""

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class HttpError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


Transport = Callable[[Call, float], Response]


def httpx_transport(call: Call, timeout: float) -> Response:
    import httpx

    headers = {"user-agent": USER_AGENT, **dict(call.headers)}
    try:
        response = httpx.request(call.method, call.url, params=dict(call.params) or None, headers=headers,
                                 json=call.json, timeout=timeout, follow_redirects=True)
    except httpx.TimeoutException as error:
        raise HttpError(f"timeout reading {call.url}") from error
    except httpx.HTTPError as error:
        raise HttpError(f"could not reach {call.url}: {type(error).__name__}") from error
    return Response(status=response.status_code, text=response.text, headers=dict(response.headers),
                    content=response.content, url=str(response.url))


class Http:
    def __init__(self, transport: Transport | None = None, timeout: float = 20.0) -> None:
        self.transport = transport or httpx_transport
        self.timeout = timeout

    def request(self, method: str, url: str, *, params: Mapping[str, Any] | None = None,
                headers: Mapping[str, str] | None = None, json: Any = None,
                timeout: float | None = None) -> Response:
        """``timeout`` overrides the client's default for one slow call (a synchronous actor run)."""
        return self.transport(Call(method, url, dict(params or {}), dict(headers or {}), json),
                              self.timeout if timeout is None else timeout)

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)


def fixture_transport(routes: Mapping[str, Any]) -> Transport:
    """A transport answering from a dict: url prefix -> Response | (status, text) | dict (JSON) |
    callable(call) -> one of those. Unknown URLs raise, so a test never reaches the network."""
    import json as _json

    def answer(call: Call, _timeout: float) -> Response:
        for prefix in sorted(routes, key=len, reverse=True):
            if call.url.startswith(prefix):
                value = routes[prefix]
                if callable(value):
                    value = value(call)
                if isinstance(value, Response):
                    return value
                if isinstance(value, tuple):
                    status, body = value
                    return Response(status=status, text=body if isinstance(body, str) else _json.dumps(body), url=call.url)
                return Response(status=200, text=value if isinstance(value, str) else _json.dumps(value), url=call.url)
        raise HttpError(f"no fixture for {call.method} {call.url}")

    return answer
