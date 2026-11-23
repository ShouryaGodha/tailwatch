"""Slack delivery: the digest as one bot message, with its message key in the message metadata
so an uncertain send is found again before anything is resent. Needs ``SLACK_BOT_TOKEN`` and
``pip install 'tailwatch[slack]'``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailwatch.delivery.base import DeliveryRejected, render

EVENT_TYPE = "tailwatch_digest"


class SlackSink:
    name = "slack"

    def __init__(self, token: str, channel: str, client: Any = None) -> None:
        if client is None:
            from slack_sdk import WebClient

            client = WebClient(token=token)
        self.client = client
        self.channel = channel

    def deliver(self, payload: Mapping[str, Any], message_key: str) -> str:
        from slack_sdk.errors import SlackApiError

        try:
            response = self.client.chat_postMessage(
                channel=self.channel, text=render(payload, "slack"), unfurl_links=False, unfurl_media=False,
                metadata={"event_type": EVENT_TYPE, "event_payload": {"message_key": message_key}})
        except SlackApiError as error:
            # Slack answered and said no: nothing was posted, the digest stays prepared.
            raise DeliveryRejected(f"Slack refused the digest: {error.response.get('error')}") from error
        return str(response.get("ts") or "")

    def find(self, message_key: str) -> str | None:
        try:
            history = self.client.conversations_history(channel=self.channel, limit=50, include_all_metadata=True)
        except Exception:
            return None
        for message in history.get("messages") or []:
            metadata = message.get("metadata") or {}
            if metadata.get("event_type") == EVENT_TYPE and (metadata.get("event_payload") or {}).get("message_key") == message_key:
                return str(message.get("ts"))
        return None

    def notice(self, text: str) -> None:
        try:
            self.client.chat_postMessage(channel=self.channel, text=f"⚠️ {text}")
        except Exception:
            pass

    def post(self, text: str, *, thread_ts: str = "") -> str:
        kwargs: dict[str, Any] = {"channel": self.channel, "text": text, "unfurl_links": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return str(self.client.chat_postMessage(**kwargs).get("ts") or "")


def sinks_for(config: Any, env: Mapping[str, str], *, console: Any = None, shadow: bool = False) -> list[Any]:
    """The configured sinks: Slack when a token and channel are set, the console, a Markdown file."""
    from tailwatch.delivery.local import ConsoleSink, MarkdownSink

    out: list[Any] = []
    slack = config.delivery.slack
    channel = slack.shadow_channel if shadow else slack.digest_channel
    if env.get("SLACK_BOT_TOKEN") and channel:
        out.append(SlackSink(env["SLACK_BOT_TOKEN"], channel))
    if shadow:
        return out
    if config.delivery.console:
        out.append(ConsoleSink(console))
    if config.delivery.markdown_dir:
        out.append(MarkdownSink(config.path(config.delivery.markdown_dir)))
    return out
