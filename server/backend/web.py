"""web.py: internet search for the agent (DuckDuckGo HTML, NO key, zero deps).

Returns the top results as text; the agent calls it as the web_search tool when it
needs facts, news or events.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
import re

import httpx

DDG = "https://html.duckduckgo.com/html/"
UA = "Mozilla/5.0 (X11; Linux x86_64) smotri-web/0.1"


@dataclass
class Result:
    title: str
    url: str
    snippet: str


async def search(query: str, count: int = 5, region: str = "ru-ru") -> list[Result]:
    q = (query or "").strip()
    if not q:
        return []
    data = {"q": q}
    if region:
        data["kl"] = region
    headers = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8",
               "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0, max_redirects=5) as c:
        r = await c.post(DDG, data=data, headers=headers)
    r.raise_for_status()
    p = _Parser()
    p.feed(r.text)
    return p.results[:count]


async def search_text(query: str, count: int = 5) -> str:
    """Search → ready text for the tool (the agent retells the gist)."""
    try:
        items = await search(query, count=count)
    except Exception as e:  # noqa: BLE001
        return f"Search failed: {e}"
    if not items:
        return "Nothing found."
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it.title} — {it.snippet} ({it.url})")
    return "\n".join(lines)


_RE = re.compile(r"^(?://duckduckgo\.com/l/|/l/|https?://duckduckgo\.com/l/)", re.I)


def _resolve(href: str) -> str:
    if not href:
        return href
    raw = "https:" + href if href.startswith("//") else href
    if not _RE.match(href):
        return href
    real = parse_qs(urlparse(raw).query).get("uddg", [""])[0]
    return unquote(real) if real else href


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[Result] = []
        self._title: list[str] = []
        self._url: str | None = None
        self._snip: list[str] = []
        self._mode: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        cls = (next((v for k, v in attrs if k.lower() == "class"), "") or "").split()
        href = next((v for k, v in attrs if k.lower() == "href"), "") or ""
        if "result__a" in cls:
            self._mode, self._title, self._url = "title", [], _resolve(href)
        elif "result__snippet" in cls:
            self._mode, self._snip = "snippet", []

    def handle_endtag(self, tag):
        if tag == "a" and self._mode is not None:
            if self._mode == "snippet":
                title = " ".join("".join(self._title).split())
                snip = " ".join("".join(self._snip).split())
                if title and self._url:
                    self.results.append(Result(title, self._url, snip))
                self._title, self._url, self._snip = [], None, []
            self._mode = None

    def handle_data(self, data):
        if self._mode == "title":
            self._title.append(data)
        elif self._mode == "snippet":
            self._snip.append(data)
