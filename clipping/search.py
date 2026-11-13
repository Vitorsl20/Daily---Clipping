"""News search: Google News RSS (primary) with Bing News RSS as fallback, plus Google News URL decoding."""
from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import parse_qs, quote_plus, urlparse

from . import http
from .feeds import parse_feed
from .models import Candidate, FeedAttempt, SourceResult

log = logging.getLogger(__name__)

# Google News edition per country (hl, gl, ceid)
EDITIONS = {
    "BR": ("pt-BR", "BR", "BR:pt-419"),
    "MX": ("es-419", "MX", "MX:es-419"),
    "AR": ("es-419", "AR", "AR:es-419"),
    "CO": ("es-419", "CO", "CO:es-419"),
    "PE": ("es-419", "PE", "PE:es-419"),
    "CL": ("es-419", "CL", "CL:es-419"),
    "UY": ("es-419", "UY", "UY:es-419"),
    "PA": ("es-419", "PA", "PA:es-419"),
    "US": ("en-US", "US", "US:en"),
    "OTHER": ("en-US", "US", "US:en"),
}
BING_MKT = {
    "BR": "pt-BR", "MX": "es-MX", "AR": "es-AR", "CO": "es-CO", "PE": "es-PE",
    "CL": "es-CL", "UY": "es-XL", "PA": "es-XL", "US": "en-US", "OTHER": "en-US",
}


# Circuit breaker: if Google News fails many times in a row (rate limit/block), go straight to Bing.
_GOOGLE_TRIP = 6
_google_fail_streak = 0
_lock = threading.Lock()


def _google_ok() -> bool:
    return _google_fail_streak < _GOOGLE_TRIP


def _google_result(success: bool) -> None:
    global _google_fail_streak
    with _lock:
        _google_fail_streak = 0 if success else _google_fail_streak + 1


def or_query(terms: list[str]) -> str:
    return "(" + " OR ".join(terms) + ")" if len(terms) > 1 else terms[0]


def google_news_url(query: str, edition: str, when_days: int) -> str:
    hl, gl, ceid = EDITIONS[edition]
    q = quote_plus(f"{query} when:{when_days}d")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def bing_news_url(query: str, edition: str) -> str:
    return f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&mkt={BING_MKT[edition]}&qft=sortbydate%3d%221%22"


def _strip_source_suffix(title: str, source: str) -> str:
    if source and title.endswith(f" - {source}"):
        return title[: -len(source) - 3].strip()
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", title).strip() if " - " in title else title


def _gnews_items(content: bytes, label: str, country: str | None, company: str | None) -> list[Candidate]:
    import feedparser

    parsed = feedparser.parse(content)
    items = parse_feed(content, "", label, country)
    # feedparser exposes <source> as entry.source.title; re-walk to attach outlet names.
    by_link = {e.get("link"): e for e in parsed.entries}
    for c in items:
        e = by_link.get(c.url)
        src = ""
        if e is not None and e.get("source"):
            src = e["source"].get("title", "")
        c.source = src or "Google News"
        c.title = _strip_source_suffix(c.title, src)
        c.summary = ""  # Google News summaries just repeat the title + outlet
        c.needs_gnews_decode = "news.google.com" in c.url
        c.company_hint = company
    return items


def _bing_items(content: bytes, label: str, country: str | None, company: str | None) -> list[Candidate]:
    items = parse_feed(content, "Bing News", label, country)
    for c in items:
        p = urlparse(c.url)
        if "bing.com" in p.netloc:
            real = parse_qs(p.query).get("url")
            if real:
                c.url = real[0]
        c.source = urlparse(c.url).netloc.removeprefix("www.")
        c.company_hint = company
    return items


def news_search(label: str, query: str, edition: str, when_days: int,
                country: str | None = None, company: str | None = None,
                deadline: float | None = None) -> SourceResult:
    """Google News first; if it errors or returns nothing, Bing News. Each engine gets one retry."""
    res = SourceResult(name=label, kind="search")
    if deadline is not None and time.time() > deadline:
        res.note = "skipped_deadline"
        return res
    gurl = google_news_url(query, edition, when_days)
    for _ in range(2 if _google_ok() else 0):
        try:
            r = http.get(gurl, timeout=20)
            items = _gnews_items(r.content, f"gnews:{label}", country, company)
            _google_result(True)
            res.attempts.append(FeedAttempt(gurl, True, len(items)))
            res.reachable = True
            if items:
                res.items, res.used_url = items, gurl
                return res
            break
        except http.FetchError as e:
            _google_result(False)
            res.attempts.append(FeedAttempt(gurl, False, error=str(e)))
    burl = bing_news_url(query, edition)
    for _ in range(2):
        try:
            r = http.get(burl, timeout=20)
            items = _bing_items(r.content, f"bing:{label}", country, company)
            res.attempts.append(FeedAttempt(burl, True, len(items)))
            res.reachable = True
            res.items, res.used_url = items, burl
            return res
        except http.FetchError as e:
            res.attempts.append(FeedAttempt(burl, False, error=str(e)))
    return res


def decode_google_news(cands: list[Candidate], chunk: int = 15) -> None:
    """Resolve news.google.com redirect links to the publisher URL, in place."""
    todo = [c for c in cands if c.needs_gnews_decode]
    if not todo:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:  # pragma: no cover
        log.error("googlenewsdecoder not installed; Google News links will stay as redirects")
        return
    for i in range(0, len(todo), chunk):
        batch = todo[i:i + chunk]
        try:
            results = gnewsdecoder([c.url for c in batch], interval=1)
        except Exception as e:  # network or format change at Google
            log.warning("Google News decode failed for batch: %s", e)
            results = [{"success": False}] * len(batch)
        for c, r in zip(batch, results):
            if r.get("success") and r.get("decoded_url"):
                c.url = r["decoded_url"]
                c.needs_gnews_decode = False
            else:
                # last resort, one at a time
                try:
                    single = gnewsdecoder(c.url, interval=1)
                    if single.get("success"):
                        c.url = single["decoded_url"]
                        c.needs_gnews_decode = False
                except Exception:
                    pass
