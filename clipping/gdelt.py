"""GDELT DOC 2.0 API: free public news index refreshed every 15 minutes, no key required.

Used as one more channel, independent of RSS.app, to see what each outlet published in the window.
GDELT's `seendate` is when its crawler first saw the article (usually minutes after publication), so it is
treated like a feed date: a first signal, always confirmed at the article page when the page opens."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote

from . import http
from .models import Candidate, FeedAttempt, SourceResult
from .timewin import Window

log = logging.getLogger(__name__)
API = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_INTERVAL = 5.5  # GDELT asks for at most one request every 5 seconds


def gdelt_url(domain: str, hours: int) -> str:
    q = quote(f"domain:{domain}")
    return f"{API}?query={q}&mode=artlist&format=json&maxrecords=250&sort=datedesc&timespan={hours}h"


def parse_seendate(v: str) -> datetime | None:
    try:
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_response(text: str, outlet: str, country: str, window: Window) -> list[Candidate]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []  # GDELT answers plain-text errors with HTTP 200
    out = []
    for a in data.get("articles", []) or []:
        seen = parse_seendate(a.get("seendate", ""))
        if not a.get("url") or not a.get("title") or (seen and not window.contains(seen)):
            continue
        out.append(Candidate(title=a["title"].strip(), url=a["url"], source=outlet,
                             discovered_via=[f"gdelt:{outlet}"], feed_published=seen, country_hint=country))
    return out


def fetch_all(domains: list[dict], window: Window, sleep=time.sleep) -> list[SourceResult]:
    hours = max(1, int((window.end - window.start).total_seconds() // 3600) + 1)
    results = []
    consecutive_down = 0
    for i, cfg in enumerate(domains):
        url = gdelt_url(cfg["domain"], hours)
        res = SourceResult(name=f"{cfg['outlet']} (GDELT)", kind="gdelt", outlet=cfg["outlet"], used_url=url)
        if consecutive_down >= 3:  # GDELT itself is down: don't spend minutes on the rest
            res.note = "GDELT fora do ar nesta execução"
            results.append(res)
            continue
        if i:
            sleep(MIN_INTERVAL)
        for attempt in range(2):
            try:
                r = http.get(url, timeout=30)
                res.items = parse_response(r.text, cfg["outlet"], cfg.get("country", "BR"), window)
                ok = r.text.lstrip().startswith("{")
                res.attempts.append(FeedAttempt(url, ok, len(res.items), error="" if ok else r.text[:120]))
                res.reachable = ok
                if ok:
                    break
            except http.FetchError as e:
                res.attempts.append(FeedAttempt(url, False, error=str(e)))
            if attempt == 0:
                sleep(MIN_INTERVAL)
        consecutive_down = 0 if res.reachable else consecutive_down + 1
        dates = [c.feed_published for c in res.items if c.feed_published]
        res.newest = max(dates) if dates else None
        res.fresh = bool(res.items)
        if cfg.get("prefilter") and not res.note:
            res.note = "prefilter"
        results.append(res)
    return results
