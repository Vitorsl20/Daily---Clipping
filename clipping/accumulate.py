"""Hourly accumulator. Collection runs every hour from the afternoon until the morning brief and stores
every candidate seen in state/accumulated.json. The 05:17 run then also uses items that already scrolled
out of a feed (RSS.app feeds keep only the latest ~20 items) or that a slow feed showed only for a while."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Candidate, SourceResult
from .timewin import Window

FILE = Path(__file__).resolve().parent.parent / "state" / "accumulated.json"
KEEP_DAYS = 5


def load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"items": {}, "channels": {}, "runs": []}


def save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def add(data: dict, results: list[SourceResult], now: datetime) -> int:
    """Store dated candidates and each channel's newest item. Returns how many new items were stored."""
    items = data.setdefault("items", {})
    new = 0
    for r in results:
        ch = data.setdefault("channels", {}).setdefault(r.name, {})
        if r.newest and (not ch.get("newest") or r.newest.isoformat() > ch["newest"]):
            ch["newest"] = r.newest.isoformat()
        ch["last_check"] = now.isoformat()
        ch["last_ok"] = now.isoformat() if r.reachable else ch.get("last_ok")
        for c in r.items:
            if c.feed_published is None or c.needs_gnews_decode:
                continue  # undated listing links are rediscovered by the morning run anyway
            key = c.url
            prev = items.get(key)
            if prev is None:
                new += 1
                items[key] = {"title": c.title, "url": c.url, "source": c.source, "via": c.discovered_via,
                              "published": c.feed_published.isoformat(), "summary": c.summary[:600],
                              "country": c.country_hint, "first_seen": now.isoformat()}
            else:
                prev["via"] = sorted(set(prev["via"]) | set(c.discovered_via))
                if c.feed_published.isoformat() < prev["published"]:
                    prev["published"] = c.feed_published.isoformat()
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    for k in [k for k, v in items.items() if v["published"] < cutoff]:
        del items[k]
    runs = data.setdefault("runs", [])
    runs.append(now.isoformat())
    data["runs"] = runs[-200:]
    return new


def candidates(data: dict, window: Window) -> list[Candidate]:
    out = []
    for v in data.get("items", {}).values():
        dt = datetime.fromisoformat(v["published"])
        if not window.contains(dt):
            continue
        out.append(Candidate(title=v["title"], url=v["url"], source=v["source"],
                             discovered_via=list(v["via"]) + ["acumulado"], feed_published=dt,
                             summary=v.get("summary", ""), country_hint=v.get("country")))
    return out


def runs_in_window(data: dict, window: Window) -> int:
    return sum(1 for r in data.get("runs", []) if window.contains(datetime.fromisoformat(r)))


def channel_newest(data: dict, name: str) -> datetime | None:
    v = data.get("channels", {}).get(name, {}).get("newest")
    return datetime.fromisoformat(v) if v else None
