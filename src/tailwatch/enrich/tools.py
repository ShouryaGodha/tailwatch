"""The enricher's read-only toolkit: a few uniform tools that return plain JSON.

The toolkit is shared: the enricher runs on it, and the editor (``desk.editor``) borrows it to
check a card. It is not in the tool registry because nobody chats with it; a routine hands
``specs(names)`` to the loop and ``call`` as the executor.

What it keeps for the proof checks after the model answers:

- ``texts``: url → the text a tool returned for that URL (a page, a profile, a search result's
  highlights). A quote counts only when it is in here.
- ``urls``: every URL a tool returned, including the links a page or profile carries. A link
  the model names that is not in here was made up.
- ``profiles``: url → the structured profile (name, work history, links) where a tool read one.
- ``usd``: what the tools spent; every charge also goes on the run's ``loop.Budget``.

Rules: a paid answer is cached in ``source_cache`` and a cache hit costs $0; a live call checks
its tool's cap and the budget first and raises ``loop.ToolStop("BUDGET")`` when the budget is
out; a provider that refuses the key or rate-limits raises ``NEED_KEY`` / ``RATE_LIMITED`` so the
run answers with what it has. ``web_search`` without ``EXA_API_KEY`` is the exception: it says
so in its result and the run goes on with the free tools.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from tailwatch import links as link_rules
from tailwatch import util
from tailwatch.agent import loop
from tailwatch.connectors._text import clean, html_page, urls_in
from tailwatch.connectors.http import Http, HttpError, Response
from tailwatch.enrich import budget as prices

EXA = "https://api.exa.ai"
GITHUB = "https://api.github.com"
ARXIV = "http://export.arxiv.org/api/query"
SCHOLAR = "https://api.semanticscholar.org/graph/v1"
BLUESKY = "https://public.api.bsky.app/xrpc"
TWITTERAPI = "https://api.twitterapi.io"
CACHE_HOURS = 30 * 24
TEXT_CAP = 12_000
RESULT_TEXT_CAP = 2_500
SEARCH_KINDS = {"people": {"category": "people"}, "linkedin": {"category": "people"},
                "personal_site": {"category": "personal site"}, "github": {"includeDomains": ["github.com"]},
                "news": {"category": "news"}, "papers": {"category": "research paper"}, "web": {}}
NO_READ_HOSTS = ("linkedin.com", "scholar.google.com", "x.com", "twitter.com")
_STR = {"type": "string"}


def _spec(tool: str, description: str, required: Iterable[str] = (), /, **properties: Any) -> dict[str, Any]:
    return {"name": tool, "description": description,
            "input_schema": {"type": "object", "properties": properties, "required": list(required),
                             "additionalProperties": False}}


SPECS: dict[str, dict[str, Any]] = {s["name"]: s for s in (
    _spec("web_search", "Search the web (Exa). kind: 'people' for professional profiles (LinkedIn), 'personal_site', "
          "'github', 'news', 'papers' or 'web'. Best query for a person: the full name and both organizations. "
          "Returns url, title, highlights and text per result.", ["query"],
          query=_STR, kind={"type": "string", "enum": list(SEARCH_KINDS)},
          limit={"type": "integer", "description": "1 to 10, default 5"}),
    _spec("web_read", "Read one web page: its text and the links it carries. Not for LinkedIn, X or Scholar.", ["url"],
          url=_STR),
    _spec("github_user", "A GitHub account: name, company, bio, location, blog and X handle.", ["login"],
          login={"type": "string", "description": "the login or the profile URL"}),
    _spec("github_search", "Search GitHub users by name or email domain; returns logins and URLs only.", ["query"],
          query=_STR),
    _spec("arxiv_search", "Papers on arXiv by an author name: title, date, co-authors and affiliations when listed.",
          ["name"], name=_STR),
    _spec("scholar_search", "Semantic Scholar authors by name: affiliations, homepage, paper count.", ["name"],
          name=_STR),
    _spec("bluesky_profile", "A Bluesky account: display name and bio.", ["handle"], handle=_STR),
    _spec("x_profile", "An X account: display name, bio, location and the link in its profile.", ["handle"],
          handle=_STR),
    _spec("x_posts", "An X account's latest posts (text, date, URL).", ["handle"], handle=_STR),
    _spec("linkedin_profile", "The live LinkedIn profile: name, headline, location and every position with dates. "
          "Paid, at most two a run: use it to settle a doubt, not to browse.", ["url"], url=_STR),
    _spec("list_lookup", "Whether URLs or a name are already on the tracked list (or were removed from it).", [],
          urls={"type": "array", "items": {"type": "string"}}, name=_STR),
)}
REQUIRES = {"x_profile": "TWITTERAPI_IO_KEY", "x_posts": "TWITTERAPI_IO_KEY", "linkedin_profile": "APIFY_API_KEY"}


def _json(response: Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


class Toolkit:
    EVIDENCE: frozenset[str] = frozenset(set(SPECS) - {"list_lookup"})

    def __init__(self, store: Any, config: Any, env: Mapping[str, str], *, budget: loop.Budget,
                 http: Http | None = None) -> None:
        self.store, self.config, self.env = store, config, env or {}
        self.budget = budget
        self.http = http or Http()
        self.texts: dict[str, str] = {}
        self.urls: set[str] = set()
        self.profiles: dict[str, dict[str, Any]] = {}
        self.usd = 0.0
        self.counts: dict[str, int] = {}
        self.ledger: list[dict[str, Any]] = []

    # -- the interface the loop uses -----------------------------------------------------------
    def available(self) -> list[str]:
        """Tools whose keys are set (web_search is always offered: it says when it has no key)."""
        return [n for n in SPECS if not REQUIRES.get(n) or self.env.get(REQUIRES[n])]

    def specs(self, names: Iterable[str]) -> list[dict[str, Any]]:
        return [dict(SPECS[n]) for n in names if n in SPECS]

    def call(self, name: str, args: dict[str, Any]) -> Any:
        spec = SPECS.get(name)
        if spec is None:
            return {"error": f"no tool named {name}"}
        args = dict(args or {})
        allowed = set(spec["input_schema"]["properties"])
        unknown = [k for k in args if k not in allowed]
        if unknown:
            return {"error": f"{name} takes no argument(s) {', '.join(unknown)}"}
        missing = [k for k in spec["input_schema"]["required"] if not str(args.get(k) or "").strip()]
        if missing:
            return {"error": f"{name} needs {', '.join(missing)}"}
        return getattr(self, "_" + name)(**args)

    def quote_found(self, quote: str, url: str) -> bool:
        """Whether ``quote`` is a run of the text a tool returned for ``url`` (letters and digits
        compared; case, spacing and punctuation do not count)."""
        text = self.text_for(url)
        needle = _letters(quote)
        return len(needle) >= 3 and needle in _letters(text)

    def text_for(self, url: str) -> str:
        if url in self.texts:
            return self.texts[url]
        key = self._key(url)
        return "\n".join(t for u, t in self.texts.items() if key and self._key(u) == key)

    def profile_for(self, url: str) -> dict[str, Any] | None:
        if url in self.profiles:
            return self.profiles[url]
        key = self._key(url)
        return next((p for u, p in self.profiles.items() if key and self._key(u) == key), None)

    def returned(self, url: str) -> bool:
        if url in self.urls:
            return True
        key = self._key(url)
        return bool(key) and any(self._key(u) == key for u in self.urls)

    @staticmethod
    def _key(url: str) -> str:
        return link_rules.key_of(link_rules.guess_kind(url), url) if url else ""

    # -- bookkeeping -----------------------------------------------------------------------------
    def _record(self, result: Mapping[str, Any]) -> None:
        for url, text in (result.get("_texts") or {}).items():
            if url and text:
                self.texts[url] = (self.texts[url] + "\n" + text) if url in self.texts and text not in self.texts[url] else text
                self.urls.add(url)
        for url in result.get("_urls") or []:
            if url:
                self.urls.add(url)
        for url, profile in (result.get("_profiles") or {}).items():
            if url:
                self.profiles[url] = dict(profile)
                self.urls.add(url)
                self.urls.update(str(link) for link in profile.get("links") or [] if link)

    def _charge(self, name: str, usd: float, key: str, cached: bool) -> None:
        cost = 0.0 if cached else max(0.0, float(usd))
        if cost:
            self.usd = round(self.usd + cost, 6)
            self.budget.charge(cost)
        if not cached:
            self.counts[name] = self.counts.get(name, 0) + 1
        self.ledger.append({"tool": name, "key": key[:200], "usd": round(cost, 6), "cached": cached})

    def _may_call(self, name: str, price: float) -> dict[str, Any] | None:
        """An error result when the tool is at its cap; ToolStop when the budget is out."""
        cap = prices.CAPS.get(name, 0)
        if self.counts.get(name, 0) >= cap:
            return {"error": f"{name} is at its cap of {cap} calls for this person; use what you have"}
        if price and self.budget.spent + price > self.budget.limit + 1e-9:
            raise loop.ToolStop("BUDGET", f"${self.budget.spent:.3f} of ${self.budget.limit:.2f} spent; "
                                          f"{name} would go over")
        return None

    def _cached(self, name: str, key_args: Mapping[str, Any], fetch: Callable[[], tuple[dict[str, Any], float]],
                *, price: float | None = None, cacheable: Callable[[Mapping[str, Any]], bool] | None = None,
                provider: str = "") -> dict[str, Any]:
        """One tool call through the cache, the cap and the budget. ``fetch`` returns
        ``(result, usd)``; private keys (``_texts``, ``_urls``, ``_profiles``) are recorded and
        kept out of what the model reads."""
        provider = provider or f"enrich:{name}"
        key = util.sha(util.dumps({"tool": name, **dict(key_args)}))
        hit = self.store.cache_get(provider, key, max_age_hours=CACHE_HOURS) if self.store is not None else None
        if isinstance(hit, dict):
            self._charge(name, 0.0, util.dumps(key_args), cached=True)
            self._record(hit)
            return _public(hit)
        price = prices.PRICES.get(name, 0.0) if price is None else price
        refused = self._may_call(name, price)
        if refused:
            return refused
        result, usd = fetch()
        self._charge(name, usd, util.dumps(key_args), cached=False)
        if not result.get("error") and (cacheable is None or cacheable(result)) and self.store is not None:
            self.store.cache_put(provider, key, result)
        self._record(result)
        return _public(result)

    # -- the tools -----------------------------------------------------------------------------
    def _exa(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        key = str(self.env.get("EXA_API_KEY") or "").strip()
        try:
            response = self.http.post(f"{EXA}{path}", json=dict(body),
                                      headers={"x-api-key": key, "content-type": "application/json"}, timeout=40)
        except HttpError as error:
            return {"error": f"Exa did not answer: {error}"}
        if response.status in (401, 403):
            raise loop.ToolStop("NEED_KEY", f"Exa refused the key (HTTP {response.status})")
        if response.status == 402:
            raise loop.ToolStop("NEED_KEY", "Exa is out of credits (HTTP 402)")
        if response.status == 429:
            raise loop.ToolStop("RATE_LIMITED", "Exa rate limited (HTTP 429)")
        answer = _json(response)
        if response.status != 200 or not isinstance(answer, dict):
            return {"error": f"Exa answered HTTP {response.status}"}
        return answer

    def _web_search(self, query: str, kind: str = "web", limit: int = 5) -> dict[str, Any]:
        kind = kind if kind in SEARCH_KINDS else "web"
        try:
            limit = max(1, min(10, int(limit)))
        except (TypeError, ValueError):
            limit = 5
        body = {"query": " ".join(str(query).split())[:400], "type": "auto", "numResults": limit,
                "contents": {"highlights": {"numSentences": 3}, "text": {"maxCharacters": RESULT_TEXT_CAP}},
                **SEARCH_KINDS[kind]}

        def fetch() -> tuple[dict[str, Any], float]:
            answer = self._exa("/search", body)
            if answer.get("error"):
                return answer, 0.0
            results, texts, profiles = [], {}, {}
            for item in answer.get("results") or []:
                url = clean(item.get("url"))
                if not url:
                    continue
                highlights = [clean(h) for h in item.get("highlights") or [] if clean(h)]
                text = clean(item.get("text"))[:RESULT_TEXT_CAP]
                title = clean(item.get("title"))
                results.append({"url": url, "title": title, "author": clean(item.get("author")),
                                "published": clean(item.get("publishedDate"))[:10], "highlights": highlights, "text": text})
                texts[url] = "\n".join([title, *highlights, text])
                if link_rules.guess_kind(url) == "linkedin":
                    profiles[url] = _search_profile(url, title, texts[url], item)
            usd = float((answer.get("costDollars") or {}).get("total") or prices.PRICES["web_search"])
            return {"query": body["query"], "kind": kind, "results": results, "_texts": texts, "_profiles": profiles}, usd

        args = {"query": body["query"], "kind": kind, "limit": limit}
        if not str(self.env.get("EXA_API_KEY") or "").strip():
            hit = self.store.cache_get("enrich:web_search", util.sha(util.dumps({"tool": "web_search", **args})),
                                       max_age_hours=CACHE_HOURS) if self.store is not None else None
            if not isinstance(hit, dict):
                return {"error": "NEED_KEY: web_search needs EXA_API_KEY; use web_read and the profile tools instead"}
        return self._cached("web_search", args, fetch)

    def _web_read(self, url: str) -> dict[str, Any]:
        url = clean(url)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        host = (re.match(r"https?://([^/]+)", url) or [None, ""])[1].lower()
        if any(host == h or host.endswith("." + h) for h in NO_READ_HOSTS):
            return {"error": f"web_read does not read {host}; use the profile tool for it"}
        if str(self.env.get("EXA_API_KEY") or "").strip():
            def fetch() -> tuple[dict[str, Any], float]:
                answer = self._exa("/contents", {"urls": [url], "text": {"maxCharacters": TEXT_CAP}, "extras": {"links": 50}})
                if answer.get("error"):
                    return answer, 0.0
                item = (answer.get("results") or [{}])[0]
                status = str(((answer.get("statuses") or [{}])[0]).get("status") or ("success" if item.get("text") else "error"))
                usd = float((answer.get("costDollars") or {}).get("total") or prices.PRICES["web_read"])
                if status != "success" or not item.get("text"):
                    return {"error": f"could not read {url} ({status})"}, usd
                page_links = [clean(link) for link in (item.get("extras") or {}).get("links") or [] if clean(link)]
                return _page(url, clean(item.get("title")), str(item.get("text") or ""), page_links), usd

            return self._cached("web_read", {"url": url}, fetch)

        def plain_fetch() -> tuple[dict[str, Any], float]:
            try:
                response = self.http.get(url, headers={"accept": "text/html,*/*"})
            except HttpError as error:
                return {"error": f"could not read {url}: {error}"}, 0.0
            if response.status != 200:
                return {"error": f"{url} answered HTTP {response.status}"}, 0.0
            page = html_page(response.text, response.url or url)
            return _page(url, page.title, page.text, page.links), 0.0

        return self._cached("web_read", {"url": url, "reader": "plain"}, plain_fetch, price=0.0)

    def _github_headers(self) -> dict[str, str]:
        headers = {"accept": "application/vnd.github+json"}
        token = str(self.env.get("GITHUB_TOKEN") or "").strip()
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    def _github(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        try:
            response = self.http.get(f"{GITHUB}{path}", params=dict(params or {}), headers=self._github_headers())
        except HttpError as error:
            return 0, {"error": f"GitHub did not answer: {error}"}
        if response.status == 429 or (response.status == 403 and str(response.headers.get("x-ratelimit-remaining", "")) == "0"):
            raise loop.ToolStop("RATE_LIMITED", "GitHub rate limited; set GITHUB_TOKEN for a higher limit")
        return response.status, _json(response)

    def _github_user(self, login: str) -> dict[str, Any]:
        handle = clean(login).lstrip("@")
        if "/" in handle:
            try:
                handle = link_rules.normalize("github", handle)[1].split(":", 1)[1]
            except ValueError:
                return {"error": f"not a GitHub profile: {login}"}

        def fetch() -> tuple[dict[str, Any], float]:
            status, data = self._github(f"/users/{handle}")
            if status == 404:
                return {"error": f"no GitHub user {handle}"}, 0.0
            if status != 200 or not isinstance(data, dict):
                return {"error": f"GitHub answered HTTP {status}"}, 0.0
            url = f"https://github.com/{clean(data.get('login')) or handle}".lower()
            blog = clean(data.get("blog"))
            if blog and not blog.startswith(("http://", "https://")):
                blog = "https://" + blog
            twitter = clean(data.get("twitter_username"))
            found = [link for link in [blog, f"https://x.com/{twitter}" if twitter else ""] if link]
            found += [u for u in urls_in(clean(data.get("bio"))) if u not in found]
            user = {"url": url, "name": clean(data.get("name")), "company": clean(data.get("company")),
                    "bio": clean(data.get("bio")), "location": clean(data.get("location")), "blog": blog,
                    "x": twitter, "email": clean(data.get("email")), "links": found}
            text = " | ".join(f"{k}: {v}" for k, v in user.items() if v and k not in ("url", "links"))
            profile = {"kind": "github", "url": url, "name": user["name"], "text": text, "work": [], "links": found}
            return {**user, "_texts": {url: text + (" | " + " ".join(found) if found else "")},
                    "_profiles": {url: profile}}, 0.0

        return self._cached("github_user", {"login": handle.lower()}, fetch)

    def _github_search(self, query: str) -> dict[str, Any]:
        query = " ".join(str(query).split())[:200]

        def fetch() -> tuple[dict[str, Any], float]:
            status, data = self._github("/search/users", {"q": query, "per_page": 8})
            if status != 200 or not isinstance(data, dict):
                return {"error": f"GitHub search answered HTTP {status}"}, 0.0
            users = [{"login": clean(i.get("login")), "url": clean(i.get("html_url")).lower()}
                     for i in data.get("items") or [] if i.get("login")]
            return {"query": query, "users": users, "_urls": [u["url"] for u in users]}, 0.0

        return self._cached("github_search", {"query": query}, fetch)

    def _arxiv_search(self, name: str) -> dict[str, Any]:
        from tailwatch.connectors.arxiv import parse_papers

        name = " ".join(str(name).split())[:120]

        def fetch() -> tuple[dict[str, Any], float]:
            try:
                response = self.http.get(ARXIV, params={"search_query": f'au:"{name}"', "max_results": 10,
                                                        "sortBy": "submittedDate", "sortOrder": "descending"})
            except HttpError as error:
                return {"error": f"arXiv did not answer: {error}"}, 0.0
            if response.status == 429 or response.status == 503:
                raise loop.ToolStop("RATE_LIMITED", f"arXiv answered HTTP {response.status}")
            if response.status != 200:
                return {"error": f"arXiv answered HTTP {response.status}"}, 0.0
            try:
                papers = parse_papers(response.text)
            except Exception as error:  # an unreadable feed is a failed read
                return {"error": f"arXiv answered something unreadable: {error}"}, 0.0
            out, texts = [], {}
            for paper in papers[:10]:
                affiliations = {k: v for k, v in (paper.get("_affiliations") or {}).items()}
                out.append({"title": paper["title"], "date": paper["date"], "url": paper["url"],
                            "authors": paper["authors"][:12], "affiliations": affiliations})
                texts[paper["url"]] = f"{paper['title']}. Authors: {', '.join(paper['authors'])}. " + \
                    " ".join(f"{k}: {v}." for k, v in affiliations.items())
            return {"name": name, "papers": out, "_texts": texts}, 0.0

        return self._cached("arxiv_search", {"name": name.lower()}, fetch)

    def _scholar_search(self, name: str) -> dict[str, Any]:
        name = " ".join(str(name).split())[:120]

        def fetch() -> tuple[dict[str, Any], float]:
            try:
                response = self.http.get(f"{SCHOLAR}/author/search", params={
                    "query": name, "limit": 5, "fields": "name,affiliations,homepage,url,paperCount,hIndex"})
            except HttpError as error:
                return {"error": f"Semantic Scholar did not answer: {error}"}, 0.0
            if response.status == 429:
                raise loop.ToolStop("RATE_LIMITED", "Semantic Scholar rate limited (HTTP 429)")
            data = _json(response)
            if response.status != 200 or not isinstance(data, dict):
                return {"error": f"Semantic Scholar answered HTTP {response.status}"}, 0.0
            authors, texts, profiles = [], {}, {}
            for item in data.get("data") or []:
                url = clean(item.get("url")) or f"https://www.semanticscholar.org/author/{clean(item.get('authorId'))}"
                homepage = clean(item.get("homepage"))
                author = {"name": clean(item.get("name")), "url": url, "affiliations": [clean(a) for a in item.get("affiliations") or []],
                          "homepage": homepage, "papers": item.get("paperCount"), "h_index": item.get("hIndex")}
                authors.append(author)
                text = f"{author['name']}. Affiliations: {', '.join(author['affiliations']) or 'none listed'}." + \
                    (f" Homepage: {homepage}" if homepage else "")
                texts[url] = text
                profiles[url] = {"kind": "semantic_scholar", "url": url, "name": author["name"], "text": text, "work": [],
                                 "links": [homepage] if homepage else []}
            return {"name": name, "authors": authors, "_texts": texts, "_profiles": profiles}, 0.0

        return self._cached("scholar_search", {"name": name.lower()}, fetch)

    def _bluesky_profile(self, handle: str) -> dict[str, Any]:
        try:
            url, key = link_rules.normalize("bluesky", clean(handle))
        except ValueError:
            return {"error": f"not a Bluesky handle: {handle}"}
        actor = key.split(":", 1)[1]

        def fetch() -> tuple[dict[str, Any], float]:
            try:
                response = self.http.get(f"{BLUESKY}/app.bsky.actor.getProfile", params={"actor": actor})
            except HttpError as error:
                return {"error": f"Bluesky did not answer: {error}"}, 0.0
            if response.status == 429:
                raise loop.ToolStop("RATE_LIMITED", "Bluesky rate limited (HTTP 429)")
            data = _json(response)
            if response.status != 200 or not isinstance(data, dict):
                return {"error": f"no Bluesky profile {actor} (HTTP {response.status})"}, 0.0
            bio = clean(data.get("description"))
            found = urls_in(bio)
            user = {"url": url, "handle": clean(data.get("handle")) or actor, "name": clean(data.get("displayName")), "bio": bio,
                    "links": found}
            text = f"{user['name']} (@{user['handle']}): {bio}"
            return {**user, "_texts": {url: text},
                    "_profiles": {url: {"kind": "bluesky", "url": url, "name": user["name"], "text": text, "work": [],
                                        "links": found}}}, 0.0

        return self._cached("bluesky_profile", {"actor": actor}, fetch)

    def _x(self, path: str, handle: str) -> Any:
        key = str(self.env.get("TWITTERAPI_IO_KEY") or "").strip()
        if not key:
            raise loop.ToolStop("NEED_KEY", "TWITTERAPI_IO_KEY is not set")
        base = str(self.env.get("TWITTERAPI_IO_URL") or TWITTERAPI).rstrip("/")
        try:
            response = self.http.get(f"{base}{path}", params={"userName": handle}, headers={"X-API-Key": key})
        except HttpError as error:
            return {"error": f"twitterapi.io did not answer: {error}"}
        if response.status in (401, 402, 403):
            raise loop.ToolStop("NEED_KEY", f"twitterapi.io refused the key (HTTP {response.status})")
        if response.status == 429:
            raise loop.ToolStop("RATE_LIMITED", "twitterapi.io rate limited (HTTP 429)")
        body = _json(response)
        if response.status != 200 or not isinstance(body, dict) or body.get("status") == "error":
            message = clean((body or {}).get("msg") or (body or {}).get("message")) if isinstance(body, dict) else ""
            return {"error": f"no X answer for @{handle}: {message or 'HTTP ' + str(response.status)}"}
        return body

    @staticmethod
    def _x_handle(value: str) -> str:
        try:
            return link_rules.normalize("x", clean(value))[1].split(":", 1)[1]
        except ValueError:
            return ""

    def _x_profile(self, handle: str) -> dict[str, Any]:
        login = self._x_handle(handle)
        if not login:
            return {"error": f"not an X handle: {handle}"}

        def fetch() -> tuple[dict[str, Any], float]:
            body = self._x("/twitter/user/info", login)
            if body.get("error"):
                return body, 0.0
            user = body.get("data") if isinstance(body.get("data"), dict) else body
            url = f"https://x.com/{login}"
            bio = clean(user.get("description"))
            entities = user.get("entities") if isinstance(user.get("entities"), dict) else {}
            expanded = [clean(u.get("expanded_url")) for part in ("url", "description")
                        for u in ((entities.get(part) or {}).get("urls") or []) if isinstance(u, dict) and u.get("expanded_url")]
            found = list(dict.fromkeys([*expanded, *urls_in(bio)]))
            out = {"url": url, "handle": login, "name": clean(user.get("name")), "bio": bio,
                   "location": clean(user.get("location")), "links": found}
            text = f"{out['name']} (@{login}): {bio} {out['location']} {' '.join(found)}".strip()
            usd = prices.X_PROFILE_CREDITS / prices.X_CREDITS_PER_USD
            return {**out, "_texts": {url: text},
                    "_profiles": {url: {"kind": "x", "url": url, "name": out["name"], "text": text, "work": [], "links": found}}}, usd

        return self._cached("x_profile", {"handle": login}, fetch)

    def _x_posts(self, handle: str) -> dict[str, Any]:
        login = self._x_handle(handle)
        if not login:
            return {"error": f"not an X handle: {handle}"}

        def fetch() -> tuple[dict[str, Any], float]:
            body = self._x("/twitter/user/last_tweets", login)
            if body.get("error"):
                return body, 0.0
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            tweets = [t for t in (data.get("tweets") or []) if isinstance(t, dict)]
            posts, texts = [], {}
            for tweet in tweets[:20]:
                url = clean(tweet.get("url")) or f"https://x.com/{login}/status/{clean(tweet.get('id'))}"
                text = clean(tweet.get("text"))
                posts.append({"url": url, "at": clean(tweet.get("createdAt"))[:30], "text": text})
                texts[url] = text
            listing = f"https://x.com/{login}"
            texts[listing] = "\n".join(p["text"] for p in posts)
            return {"handle": login, "posts": posts, "_texts": texts,
                    "_urls": [u for p in posts for u in urls_in(p["text"])]}, prices.x_posts_usd(len(posts))

        return self._cached("x_posts", {"handle": login}, fetch, price=prices.x_posts_usd(20))

    def _linkedin_profile(self, url: str) -> dict[str, Any]:
        from tailwatch.connectors import linkedin
        from tailwatch.connectors.base import CheckContext

        canonical = linkedin.profile_url(url)
        if not canonical:
            return {"error": f"not a LinkedIn profile URL (linkedin.com/in/...): {url}"}
        raw = self.store.cache_get(linkedin.CACHE, canonical, max_age_hours=CACHE_HOURS) if self.store is not None else None
        if isinstance(raw, dict) and linkedin.is_profile(raw):
            self._charge("linkedin_profile", 0.0, canonical, cached=True)
            return self._linkedin_result(canonical, linkedin.normalise(raw), linkedin)
        options = self.config.connector("linkedin").options if self.config is not None else {}
        price = float(options.get("usd_per_profile", linkedin.USD_PER_PROFILE))
        refused = self._may_call("linkedin_profile", price)
        if refused:
            return refused
        ctx = CheckContext(http=self.http, env=self.env, options=options, cache_get=lambda _p, _k, _a: None,
                           cache_put=self.store.cache_put if self.store is not None else (lambda _p, _k, _v: None))
        read = linkedin.LinkedInChecker().read(canonical, None, ctx)
        self._charge("linkedin_profile", read.usd, canonical, cached=False)
        if read.status == "gone":
            return {"error": f"no LinkedIn profile at {canonical}"}
        if read.status != "ok":
            if read.outage in ("no credential", "key rejected", "out of credits"):
                raise loop.ToolStop("NEED_KEY", f"LinkedIn: {read.error}")
            if read.outage == "rate limited":
                raise loop.ToolStop("RATE_LIMITED", f"LinkedIn: {read.error}")
            return {"error": f"LinkedIn: {read.error}"}
        return self._linkedin_result(canonical, read.content, linkedin)

    def _linkedin_result(self, url: str, content: Mapping[str, Any], linkedin: Any) -> dict[str, Any]:
        text = linkedin.LinkedInChecker().describe(content)
        work = [{"company": p.get("company", ""), "title": p.get("title", ""), "start": p.get("start", ""),
                 "end": p.get("end", ""), "current": bool(p.get("current"))} for p in content.get("positions") or []]
        found = urls_in(str(content.get("bio") or ""))
        result = {"url": url, "name": content.get("name", ""), "headline": content.get("headline", ""),
                  "location": content.get("location", ""), "about": util.clip(content.get("bio"), 1500), "positions": work,
                  "_texts": {url: text},
                  "_profiles": {url: {"kind": "linkedin", "url": url, "name": content.get("name", ""), "text": text,
                                      "work": work, "links": found, "source": "live"}}}
        self._record(result)
        return _public(result)

    def _list_lookup(self, urls: list[str] | None = None, name: str = "") -> dict[str, Any]:
        if self.store is None:
            return {"error": "no list to look in"}
        refused = self._may_call("list_lookup", 0.0)
        if refused:
            return refused
        found: list[dict[str, Any]] = []
        removed = self.store.removed_keys()
        for url in [u for u in (urls or []) if str(u).strip()][:20]:
            kind = link_rules.guess_kind(str(url))
            key = link_rules.key_of(kind, str(url))
            person = self.store.person_by_key(key) if key else None
            found.append({"url": url, "listed": bool(person and person["status"] == "tracked"),
                          "person": f"#{person['id']} {person['name']}" if person else "",
                          "removed": bool(key and key in removed) or bool(person and person["status"] == "ignored")})
        named = []
        if str(name or "").strip():
            from tailwatch.enrich import names as name_rules

            for person in self.store.find_people(str(name), limit=10):
                if name_rules.compatible(name, person["name"], "initials"):
                    named.append({"person": f"#{person['id']} {person['name']}", "organization": person.get("organization"),
                                  "status": person["status"], "links": person.get("links") or {}})
        self._charge("list_lookup", 0.0, util.dumps({"urls": urls, "name": name}), cached=False)
        return {"urls": found, "people_named": named}


def _public(result: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def _page(url: str, title: str, text: str, page_links: list[str]) -> dict[str, Any]:
    body = text[:TEXT_CAP]
    found = list(dict.fromkeys([*page_links, *urls_in(body)]))[:80]
    profile = {"kind": link_rules.guess_kind(url), "url": url, "name": "", "text": f"{title}\n{body}", "work": [],
               "links": found}
    return {"url": url, "title": title, "text": body, "links": found, "_texts": {url: f"{title}\n{body}"},
            "_urls": found, "_profiles": {url: profile}}


def _search_profile(url: str, title: str, text: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """A professional-profile search result as the facts the checks read: the name from the
    title ("Ada Lovelace - Researcher - Analytical Engines | LinkedIn") or Exa's record, and the
    work history when Exa gives one."""
    name = re.split(r"\s+[-–|·]\s+|\s*\|\s*", title)[0].strip() if title else ""
    work: list[dict[str, Any]] = []
    for entity in item.get("entities") or []:
        record = entity.get("properties") if isinstance(entity, dict) and isinstance(entity.get("properties"), dict) else entity
        if not isinstance(record, dict):
            continue
        name = clean(record.get("name")) or name
        for job in record.get("workHistory") or []:
            if not isinstance(job, dict):
                continue
            dates = job.get("dates") if isinstance(job.get("dates"), dict) else {}
            company = job.get("company")
            work.append({"company": clean(company.get("name") if isinstance(company, dict) else company),
                         "title": clean(job.get("title")), "start": clean(dates.get("from"))[:7],
                         "end": clean(dates.get("to"))[:7], "current": not dates.get("to")})
    return {"kind": "linkedin", "url": url, "name": name, "text": text, "work": work, "links": [], "source": "search"}
