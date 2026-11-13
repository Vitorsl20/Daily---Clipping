"""Data structures shared across the pipeline."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


# Verification statuses
PENDING = "pending"
VERIFIED = "verified"          # article page opened, publish date confirmed inside the window
UNVERIFIED = "unverified"      # fresh per feed/search, but article page could not be opened/dated -> NÃO VERIFICADO
STALE = "stale"                # confirmed older than the window -> excluded
AMBIGUOUS = "ambiguous"        # page opened but date doubt not caused by access -> excluded
NO_DATE = "no_date"            # nothing anywhere says when it was published -> excluded


@dataclass
class Candidate:
    title: str
    url: str
    source: str                       # outlet name, e.g. "Valor Econômico"
    discovered_via: list[str]         # e.g. ["rss.app:Valor Econômico — Finanças", "gnews:Itaú Unibanco"]
    feed_published: datetime | None = None   # timezone-aware (UTC) pubDate from the feed/search result
    summary: str = ""
    country_hint: str | None = None
    company_hint: str | None = None
    needs_gnews_decode: bool = False  # url is a news.google.com redirect

    # filled by verification
    status: str = PENDING
    article_published: datetime | None = None
    article_date_only: bool = False   # page gave a calendar date without a time
    date_method: str = ""
    article_title: str | None = None
    article_text: str = ""
    verify_note: str = ""
    http_status: int | None = None

    # filled by the editor
    triage_country: str | None = None
    id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id(self.url or self.title)

    @property
    def best_date(self) -> datetime | None:
        return self.article_published or self.feed_published

    @property
    def headline(self) -> str:
        """Headline as published: prefer the article page's own title when we opened it."""
        return (self.article_title or self.title).strip()


def make_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


@dataclass
class FeedAttempt:
    url: str
    ok: bool
    item_count: int = 0
    newest: datetime | None = None
    error: str = ""


@dataclass
class SourceResult:
    """Outcome of checking one source (feed, listing page or search query)."""
    name: str
    kind: str                          # "rss_app" | "secondary" | "outlet" | "search"
    items: list[Candidate] = field(default_factory=list)
    attempts: list[FeedAttempt] = field(default_factory=list)
    reachable: bool = False
    newest: datetime | None = None
    fresh: bool | None = None          # None = no dates available to judge (listing page)
    used_url: str = ""
    note: str = ""
