"""Feed fetching: RSS.app feeds (with the mandatory cache-busting retry loop), outlet RSS feeds and
listing-page fallbacks."""
from __future__ import annotations

import calendar
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup

from . import http
from .models import Candidate, FeedAttempt, SourceResult
from .timewin import Window

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s))).strip()


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def parse_feed(content: bytes, source: str, via: str, country: str | None = None) -> list[Candidate]:
    parsed = feedparser.parse(content)
    out: list[Candidate] = []
    for e in parsed.entries:
        link = e.get("link") or ""
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        out.append(Candidate(
            title=title,
            url=link,
            source=source,
            discovered_via=[via],
            feed_published=_entry_time(e),
            summary=clean_text(e.get("summary"))[:600],
            country_hint=country,
        ))
    return out


def _newest(items: list[Candidate]) -> datetime | None:
    dates = [c.feed_published for c in items if c.feed_published]
    return max(dates) if dates else None


def cache_busted(url: str, ts: int) -> str:
    """Exactly ONE clean numeric cache-buster param — anything else has been seen to revert feeds to stale content."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_={ts}"


def fetch_rss_app(
    name: str,
    outlet: str,
    url: str,
    window: Window,
    attempts: int = 3,
    waits: tuple[float, ...] = (20.0, 60.0),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    prior: SourceResult | None = None,
) -> SourceResult:
    """Mandatory retry loop: fetch with a fresh `?_=<unix>` each attempt until the newest item is inside the
    freshness window (or attempts run out). Items from every attempt are merged, since feeds have been seen
    to oscillate between fresh and stale content."""
    res = prior or SourceResult(name=name, kind="rss_app")
    seen = {c.url for c in res.items}
    last_ts = max((int(a.url.rsplit("_=", 1)[-1]) for a in res.attempts if "_=" in a.url), default=0)
    for i in range(attempts):
        ts = max(int(clock()), last_ts + 1)
        last_ts = ts
        full = cache_busted(url, ts)
        try:
            r = http.get(full, no_cache=True)
            items = parse_feed(r.content, outlet, f"rss.app:{name}", "BR")
            newest = _newest(items)
            res.attempts.append(FeedAttempt(full, True, len(items), newest))
            res.reachable = True
            res.used_url = full
            for c in items:
                if c.url not in seen:
                    seen.add(c.url)
                    res.items.append(c)
        except http.FetchError as e:
            res.attempts.append(FeedAttempt(full, False, error=str(e)))
            log.warning("RSS.app %s attempt %d failed: %s", name, i + 1, e)
        res.newest = _newest(res.items)
        res.fresh = window.contains(res.newest) if res.newest else False
        if res.fresh:
            break
        if i < attempts - 1:
            sleep(waits[min(i, len(waits) - 1)])
    return res


# ---------------------------------------------------------------- generic sources

_SKIP_PATH = re.compile(
    r"/(tag|tags|tudo-sobre|autor|author|autores|categoria|category|page|pagina|busca|search|newsletter|"
    r"podcast|podcasts|video|videos|ao-vivo|assine|login|cadastro|termos|privacidade|sobre|contato|"
    r"publicidade|expediente|feed|rss)(/|$)",
    re.I,
)


def extract_listing_links(page_html: str, base_url: str, source: str, via: str,
                          country: str | None, limit: int = 120) -> list[Candidate]:
    """Pull article-looking links out of a listing/homepage. No dates here — they come from verification."""
    soup = BeautifulSoup(page_html, "lxml")
    base_host = urlparse(base_url).netloc.removeprefix("www.")
    out: list[Candidate] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.scheme not in ("http", "https") or p.netloc.removeprefix("www.") != base_host:
            continue
        path = p.path.rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        if _SKIP_PATH.search(p.path) or slug.count("-") < 3:
            continue
        title = clean_text(a.get_text(" "))
        if len(title) < 25:
            title = clean_text(a.get("title") or "")
        if len(title) < 25 or href in seen:
            continue
        seen.add(href)
        out.append(Candidate(title=title, url=href, source=source, discovered_via=[via], country_hint=country))
        if len(out) >= limit:
            break
    return out


def fetch_source(cfg: dict, kind: str, window: Window) -> SourceResult:
    """Try each RSS URL, then each listing URL; first one that yields items wins. One retry per URL."""
    name = cfg["name"]
    country = cfg.get("country")
    res = SourceResult(name=name, kind=kind)
    for url in cfg.get("rss") or []:
        for attempt in range(2):
            try:
                r = http.get(url)
                items = parse_feed(r.content, name, f"{kind}:{name}", country)
                res.attempts.append(FeedAttempt(url, True, len(items), _newest(items)))
                res.reachable = True
                if items:
                    res.items, res.used_url = items, url
                    res.newest = _newest(items)
                    res.fresh = window.contains(res.newest) if res.newest else None
                    return res
                break
            except http.FetchError as e:
                res.attempts.append(FeedAttempt(url, False, error=str(e)))
    for url in cfg.get("listing") or []:
        for attempt in range(2):
            try:
                r = http.get(url)
                if http.looks_like_challenge(r.text):
                    raise http.FetchError(url, r.status_code, "bot challenge")
                items = extract_listing_links(r.text, url, name, f"{kind}:{name}", country)
                res.attempts.append(FeedAttempt(url, True, len(items)))
                res.reachable = True
                if items:
                    res.items.extend(items)
                    res.used_url = res.used_url or url
                break
            except http.FetchError as e:
                res.attempts.append(FeedAttempt(url, False, error=str(e)))
    if res.items:
        res.fresh = None
        res.note = "listagem sem datas (datas conferidas artigo a artigo)"
    return res
