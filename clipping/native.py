"""Native channels of each outlet, independent of RSS.app:

* Google News sitemaps, discovered from robots.txt. Publishers keep these current within minutes so
  Google can index them, and each entry carries the title and the exact publication time.
* The outlet's own RSS/Atom feed, discovered from <link rel="alternate"> on the homepage.
* Extra candidate URLs from config (`try`), ignored when they don't exist.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from lxml import etree

from . import http
from .feeds import clean_text, parse_feed
from .models import Candidate, FeedAttempt, SourceResult
from .timewin import BRT, COUNTRY_TZ, Window
from .verify import parse_iso

log = logging.getLogger(__name__)

MAX_CHILD_SITEMAPS = 4
MAX_ITEMS = 600
_NEWSY = re.compile(r"news|noticia|google", re.I)


def _local(tag) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(node, name: str) -> str:
    for el in node.iter():
        if _local(el.tag) == name and el.text:
            return el.text.strip()
    return ""


def parse_sitemap(content: bytes) -> tuple[list[dict], list[dict]]:
    """Returns (urls, child_sitemaps). Each url: loc, title, published (news:publication_date), lastmod."""
    try:
        root = etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    urls, children = [], []
    kind = _local(root.tag)
    for node in root:
        name = _local(node.tag)
        if kind == "sitemapindex" and name == "sitemap":
            children.append({"loc": _child_text(node, "loc"), "lastmod": _child_text(node, "lastmod")})
        elif name == "url":
            urls.append({
                "loc": _child_text(node, "loc"),
                "title": _child_text(node, "title"),                 # news:title
                "published": _child_text(node, "publication_date"),  # news:publication_date
                "lastmod": _child_text(node, "lastmod"),
            })
    return urls, children


def title_from_slug(url: str) -> str:
    slug = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"\.(g?html?|php)$", "", slug)
    slug = re.sub(r"(^|-)\d{5,}($|-)", " ", slug)
    words = slug.replace("_", " ").replace("-", " ").split()
    return " ".join(words).capitalize()


def robots_sitemaps(host: str) -> list[str]:
    r = http.get(f"https://{host}/robots.txt", timeout=15)
    return [line.split(":", 1)[1].strip() for line in r.text.splitlines()
            if line.lower().startswith("sitemap:")]


def homepage_feeds(host: str) -> list[str]:
    r = http.get(f"https://{host}/", timeout=20)
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        t = (link.get("type") or "").lower()
        if ("rss" in t or "atom" in t) and link.get("href"):
            out.append(urljoin(r.url or f"https://{host}/", link["href"]))
    return out


def _pick_children(children: list[dict], window: Window) -> list[str]:
    """From a sitemap index, keep news sitemaps, else the most recently modified ones / those dated in the window."""
    news = [c["loc"] for c in children if _NEWSY.search(c["loc"])]
    if news:
        return news[:MAX_CHILD_SITEMAPS]
    days = set()
    d = window.start.date()
    while d <= window.end.date():
        days.add(d)
        d += timedelta(days=1)
    pats = [re.compile(p) for day in days for p in (
        rf"{day:%Y}[/_-]?{day:%m}[/_-]?{day:%d}", rf"{day:%Y}/{day:%m}/{day.day}(?!\d)")]
    dated = [c["loc"] for c in children if any(p.search(c["loc"]) for p in pats)]
    if dated:
        return dated[:MAX_CHILD_SITEMAPS]
    with_mod = sorted((c for c in children if c["lastmod"]), key=lambda c: c["lastmod"], reverse=True)
    return [c["loc"] for c in with_mod[:2]]


def _sitemap_candidates(urls: list[dict], outlet: str, via: str, country: str, window: Window) -> list[Candidate]:
    tz = COUNTRY_TZ.get(country, BRT)
    out = []
    for u in urls:
        if not u["loc"]:
            continue
        published = parse_iso(u["published"], tz)[0] if u["published"] else None
        if published is not None:
            title = clean_text(u["title"]) or title_from_slug(u["loc"])
            feed_dt = published
        else:
            # Plain sitemap: lastmod is a *modified* date, never trusted as publish date. Use it only to
            # skip clearly old URLs; the article page must supply the real date.
            lastmod = parse_iso(u["lastmod"], tz)[0] if u["lastmod"] else None
            if lastmod is None or lastmod < window.start:
                continue
            title, feed_dt = title_from_slug(u["loc"]), None
        if feed_dt is not None and not window.contains(feed_dt):
            continue
        out.append(Candidate(title=title, url=u["loc"], source=outlet, discovered_via=[via],
                             feed_published=feed_dt, country_hint=country))
    return out


def fetch_native(cfg: dict, window: Window) -> SourceResult:
    outlet, host, country = cfg["outlet"], cfg["host"], cfg.get("country", "BR")
    res = SourceResult(name=f"{outlet} (canais nativos)", kind="native", outlet=outlet)
    via_sm, via_rss = f"sitemap:{outlet}", f"rss-nativo:{outlet}"
    channels: list[str] = []
    items: list[Candidate] = []

    # 1) sitemaps from robots.txt (+ any sitemap-looking `try` URLs)
    sitemap_urls: list[str] = []
    try:
        declared = robots_sitemaps(host)
        res.attempts.append(FeedAttempt(f"https://{host}/robots.txt", True, len(declared)))
        newsy = [s for s in declared if _NEWSY.search(s)]
        sitemap_urls += newsy or declared[:3]
    except http.FetchError as e:
        res.attempts.append(FeedAttempt(f"https://{host}/robots.txt", False, error=str(e)))
    feed_urls = [u for u in cfg.get("try", []) if "sitemap" not in u.lower()]
    sitemap_urls += [u for u in cfg.get("try", []) if "sitemap" in u.lower()]

    seen_sm: set[str] = set()
    queue = list(dict.fromkeys(sitemap_urls))
    while queue and len(seen_sm) < 10:
        sm = queue.pop(0)
        if sm in seen_sm:
            continue
        seen_sm.add(sm)
        try:
            r = http.get(sm, timeout=25)
        except http.FetchError as e:
            res.attempts.append(FeedAttempt(sm, False, error=str(e)))
            continue
        urls, children = parse_sitemap(r.content)
        res.attempts.append(FeedAttempt(sm, True, len(urls) or len(children)))
        if children:
            queue += [c for c in _pick_children(children, window) if c not in seen_sm]
        got = _sitemap_candidates(urls, outlet, via_sm, country, window)
        if got:
            channels.append(f"sitemap {urlparse(sm).path or sm} ({len(got)})")
            items += got

    # 2) the outlet's own RSS (homepage autodiscovery + configured guesses)
    try:
        feed_urls = homepage_feeds(host) + feed_urls
    except http.FetchError as e:
        res.attempts.append(FeedAttempt(f"https://{host}/", False, error=str(e)))
    for fu in list(dict.fromkeys(feed_urls))[:4]:
        try:
            r = http.get(fu, timeout=20, no_cache=True)
            got = parse_feed(r.content, outlet, via_rss, country)
            res.attempts.append(FeedAttempt(fu, True, len(got)))
            got = [c for c in got if c.feed_published is None or window.contains(c.feed_published)]
            if got:
                channels.append(f"RSS próprio {fu} ({len(got)})")
                items += got
        except http.FetchError as e:
            res.attempts.append(FeedAttempt(fu, False, error=str(e)))

    paths = cfg.get("paths") or []
    if paths:
        items = [c for c in items if any(p in urlparse(c.url).path for p in paths)]
    seen: set[str] = set()
    res.items = [c for c in items if not (c.url in seen or seen.add(c.url))][:MAX_ITEMS]
    res.reachable = any(a.ok for a in res.attempts)
    dates = [c.feed_published for c in res.items if c.feed_published]
    res.newest = max(dates) if dates else None
    res.fresh = window.contains(res.newest) if res.newest else (None if res.items else False)
    res.note = "; ".join(channels) if channels else "nenhum canal nativo com itens na janela"
    res.used_url = ", ".join(sorted(seen_sm))
    return res
