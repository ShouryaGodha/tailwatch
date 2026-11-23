"""One spelling for every link, so a person is never listed twice.

``normalize(kind, value)`` returns ``(url, key)``: the canonical URL a connector reads and the
key that identifies the account (``github:ada``). ``guess_kind`` sorts a bare URL into a kind.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

KINDS = ("linkedin", "github", "x", "homepage", "arxiv", "semantic_scholar", "bluesky", "rss", "scholar", "openreview")


def _clean(value: str) -> str:
    return value.strip().strip("<>").strip()


def _path_parts(url: str) -> tuple[str, list[str]]:
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    return host, [p for p in parsed.path.split("/") if p]


def guess_kind(value: str) -> str:
    value = _clean(value)
    if value.startswith("@") and "." in value[1:]:
        return "bluesky"
    if value.startswith("@"):
        return "x"
    host, _parts = _path_parts(value)
    if "linkedin.com" in host:
        return "linkedin"
    if host == "github.com":
        return "github"
    if host in ("x.com", "twitter.com"):
        return "x"
    if host == "bsky.app":
        return "bluesky"
    if host == "arxiv.org":
        return "arxiv"
    if host == "semanticscholar.org":
        return "semantic_scholar"
    if host == "scholar.google.com":
        return "scholar"
    if host == "openreview.net":
        return "openreview"
    if value.endswith((".xml", "/feed", "/rss", "/atom", "feed.xml", "index.xml")):
        return "rss"
    return "homepage"


def normalize(kind: str, value: str) -> tuple[str, str]:
    value = _clean(value)
    if not value:
        raise ValueError(f"empty {kind} link")
    if kind == "github":
        handle = value.lstrip("@")
        if "/" in handle or "." in handle:
            _host, parts = _path_parts(value)
            handle = parts[0] if parts else ""
        handle = handle.lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38})", handle):
            raise ValueError(f"not a GitHub user: {value}")
        return f"https://github.com/{handle}", f"github:{handle}"
    if kind == "x":
        handle = value.lstrip("@")
        if "/" in handle or "." in handle:
            _host, parts = _path_parts(value)
            handle = parts[0] if parts else ""
        handle = handle.lower()
        if not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
            raise ValueError(f"not an X handle: {value}")
        return f"https://x.com/{handle}", f"x:{handle}"
    if kind == "linkedin":
        _host, parts = _path_parts(value)
        if len(parts) < 2 or parts[0] not in ("in", "pub"):
            raise ValueError(f"not a LinkedIn profile: {value}")
        slug = parts[1].lower()
        return f"https://www.linkedin.com/in/{slug}", f"linkedin:{slug}"
    if kind == "bluesky":
        handle = value.lstrip("@")
        if "bsky.app/profile" in handle:
            _host, parts = _path_parts(value)
            handle = parts[1] if len(parts) > 1 else ""
        handle = handle.lower()
        if "." not in handle:
            handle += ".bsky.social"
        return f"https://bsky.app/profile/{handle}", f"bluesky:{handle}"
    if kind == "arxiv":
        # an author query ("Lovelace, Ada") or an arxiv.org/a/<id> author page
        if "arxiv.org" in value:
            _host, parts = _path_parts(value)
            author = parts[-1] if parts else value
            return f"https://arxiv.org/a/{author}", f"arxiv:{author.lower()}"
        query = re.sub(r"\s+", " ", value).strip()
        return f"arxiv:au:{query}", f"arxiv:{query.lower()}"
    if kind == "semantic_scholar":
        found = re.search(r"(\d{3,})", value)
        if not found:
            raise ValueError(f"not a Semantic Scholar author id: {value}")
        return f"https://www.semanticscholar.org/author/{found.group(1)}", f"semantic_scholar:{found.group(1)}"
    if kind == "scholar":
        found = re.search(r"user=([\w-]+)", value)
        user = found.group(1) if found else value
        return f"https://scholar.google.com/citations?user={user}", f"scholar:{user}"
    # homepage, rss, openreview and anything else: the URL without scheme, www or trailing slash
    url = value if "://" in value else "https://" + value
    host, parts = _path_parts(url)
    path = "/".join(parts)
    key_body = host + ("/" + path if path else "")
    query = urlparse(url).query
    if query:
        key_body += "?" + query
    canonical = f"{urlparse(url).scheme or 'https'}://{host}" + ("/" + path if path else "") + (f"?{query}" if query else "")
    return canonical, f"{kind}:{key_body.lower()}"


def key_of(kind: str, value: str) -> str:
    try:
        return normalize(kind, value)[1]
    except ValueError:
        return ""


def normalize_all(links: dict[str, str]) -> dict[str, str]:
    """Canonical URLs by kind; unparseable links are dropped."""
    out: dict[str, str] = {}
    for kind, value in (links or {}).items():
        if not value:
            continue
        try:
            out[kind] = normalize(kind, value)[0]
        except ValueError:
            continue
    return out
