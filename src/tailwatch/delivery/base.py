"""Where a digest or a notice goes. A sink delivers a rendered digest under a stable message key
and, when it can, finds an earlier delivery by that key (so an uncertain send is reconciled
before anything is sent again)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class DeliveryRejected(RuntimeError):
    """The destination explicitly refused the message: nothing was delivered, safe to retry."""


class Sink(Protocol):
    name: str

    def deliver(self, payload: Mapping[str, Any], message_key: str) -> str:
        """Deliver and return an id for the delivered message (a Slack ts, a file path)."""
        ...

    def find(self, message_key: str) -> str | None:
        """The id of an earlier delivery of this key, or None. Sinks that cannot look return None."""
        ...

    def notice(self, text: str) -> None:
        """A one-line operational notice (a provider outage, a job that keeps failing)."""
        ...


def link(text: str, url: str, style: str) -> str:
    if not url:
        return text
    if style == "slack":
        return f"<{url}|{text.replace('|', '/').replace('>', '')}>"
    if style == "markdown":
        return f"[{text}]({url})"
    return text


def render(payload: Mapping[str, Any], style: str = "markdown") -> str:
    """The digest as text. ``style`` is markdown, slack (mrkdwn) or plain."""
    bold = (lambda s: f"*{s}*") if style == "slack" else (lambda s: f"**{s}**") if style == "markdown" else (lambda s: s)
    lines = [bold(payload.get("title") or "Digest")]
    if not payload.get("sections"):
        lines.append(payload.get("quiet_line") or "Nothing new since the last digest.")
    for section in payload.get("sections") or []:
        count = len(section["cards"])
        lines += ["", bold(f"{section['heading']} · {count} {'person' if count == 1 else 'people'}")]
        for card in section["cards"]:
            lines.append(f"{section['mark']} {link(card['name'], card.get('url') or '', style)} · {card['summary']}")
            if card.get("context"):
                lines.append(card["context"])
            if card.get("why"):
                lines.append(f"★ {card['why']}")
            extras = [link(label, url, style) for label, url in (card.get("links") or {}).items()]
            if extras:
                lines.append(" · ".join(extras))
            lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
    for title, items in (("Also", payload.get("also") or []),):
        if items:
            lines += ["", f"{title}: " + " · ".join(items)]
    unlisted = payload.get("unlisted") or []
    if unlisted:
        lines += ["", "Seen elsewhere, not on your list · " + " · ".join(link(u["text"], u.get("url") or "", style) for u in unlisted)]
    for note in payload.get("notes") or []:
        lines += ["", f"_{note}_" if style != "plain" else note]
    if payload.get("footer"):
        lines += ["", payload["footer"]]
    return "\n".join(lines).strip()
