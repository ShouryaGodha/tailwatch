"""The Slack bot over Socket Mode: no public URL, no server to host. Needs ``SLACK_BOT_TOKEN``
(xoxb-) and ``SLACK_APP_TOKEN`` (xapp-, with ``connections:write``); the app manifest is in
``deploy/slack-manifest.yaml``.

- In the desk channel every message is for the desk agent (no mention needed); a reply under one
  of its posts carries that post.
- In the digest channel the bot answers a mention or a reply in a digest's thread ("why Ada?").
- Each Slack message is answered once: the message key is ``channel:ts``, the run is checkpointed
  under it, and a retried event resumes instead of answering twice.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

ROUTABLE = {"message", "app_mention"}


def should_answer(event: Mapping[str, Any], *, bot_user: str, desk_channel: str, digest_channels: set[str],
                  bot_threads: set[str]) -> str:
    """'' (ignore), 'desk' or 'channel'."""
    if event.get("type") not in ROUTABLE or event.get("bot_id") or event.get("subtype") not in (None, "thread_broadcast"):
        return ""
    if event.get("user") == bot_user:
        return ""
    channel = str(event.get("channel") or "")
    if channel and channel == desk_channel:
        return "desk" if event.get("type") == "message" else ""
    if channel in digest_channels:
        mentioned = event.get("type") == "app_mention"
        in_bot_thread = event.get("type") == "message" and str(event.get("thread_ts") or "") in bot_threads \
            and f"<@{bot_user}>" not in str(event.get("text") or "")
        return "channel" if mentioned or in_bot_thread else ""
    return ""


def serve(config_path: str | None = None) -> None:
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    from tailwatch.cli import Session
    from tailwatch.desk.chat import respond

    session = Session(config_path)
    config, env = session.config, session.env
    web = WebClient(token=env["SLACK_BOT_TOKEN"])
    bot_user = str(web.auth_test().get("user_id") or "")
    slack = config.delivery.slack
    digest_channels = {c for c in (slack.digest_channel, slack.shadow_channel) if c}
    lock = threading.Lock()

    def bot_threads() -> set[str]:
        rows = session.store.db.query("SELECT ts FROM digests WHERE ts != '' ORDER BY id DESC LIMIT 30")
        return {part for row in rows for part in str(row["ts"]).split(",") if part and ":" not in part}

    def handle(event: Mapping[str, Any]) -> None:
        mode = should_answer(event, bot_user=bot_user, desk_channel=slack.desk_channel, digest_channels=digest_channels,
                             bot_threads=bot_threads())
        if not mode:
            return
        channel, ts = str(event["channel"]), str(event["ts"])
        thread = str(event.get("thread_ts") or ts)
        text = str(event.get("text") or "").replace(f"<@{bot_user}>", "").strip()
        with lock:  # one store connection: answer one message at a time
            result = respond(session.store, config, env, text=text, user=str(event.get("user") or ""), channel=channel,
                             thread=thread, message_key=f"{channel}:{ts}", mode=mode)
        answer = str(result.get("answer") or "").strip()
        if answer and result.get("state") != "silent":
            web.chat_postMessage(channel=channel, thread_ts=thread, text=answer, unfurl_links=False)

    def listener(client: SocketModeClient, request: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type == "events_api":
            event = (request.payload or {}).get("event") or {}
            threading.Thread(target=handle, args=(event,), daemon=True).start()

    client = SocketModeClient(app_token=env["SLACK_APP_TOKEN"], web_client=web)
    client.socket_mode_request_listeners.append(listener)
    client.connect()
    threading.Event().wait()
