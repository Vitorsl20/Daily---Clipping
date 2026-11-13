"""Article-level verification: open each article page and read its OWN publish date (byline / metadata).

Feed pubDates and search snippets are only a first signal. An item counts as VERIFIED only when the
article page itself confirms a publish date inside the freshness window."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from . import http
from .models import AMBIGUOUS, NO_DATE, STALE, UNVERIFIED, VERIFIED, Candidate
from .timewin import BRT, COUNTRY_TZ, Window

log = logging.getLogger(__name__)
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)
logging.getLogger("htmldate").setLevel(logging.CRITICAL)

# meta tags that carry the ORIGINAL publish time (never "modified"/"updated")
_META_PUBLISHED = (
    ("property", "article:published_time"),
    ("property", "og:article:published_time"),
    ("name", "article:published_time"),
    ("itemprop", "datePublished"),
    ("name", "datePublished"),
    ("name", "publish-date"),
    ("name", "publishdate"),
    ("name", "pubdate"),
    ("name", "date"),
    ("name", "DC.date.issued"),
    ("name", "dcterms.created"),
    ("name", "parsely-pub-date"),
    ("name", "sailthru.date"),
    ("name", "cXenseParse:recs:publishtime"),
    ("property", "published_time"),
)

_MONTHS = {
    # pt
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    # es
    "enero": 1, "febrero": 2, "marzo": 3, "mayo": 5, "junio": 6, "julio": 7, "septiembre": 9,
    "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# "Publicado em 12/11/2026 - 18:42", "12/11/2026 às 18h42", "28/08/2026 at 13:07", "12/11/2026 18:42"
_RE_NUMERIC = re.compile(
    r"(?:publicad[oa]\s+(?:em|el)\s*:?\s*)?(\d{1,2})/(\d{1,2})/(\d{4})\s*(?:-|–|,|às|as|a las|at)?\s*(\d{1,2})[:hH](\d{2})",
    re.I,
)
_RE_NUMERIC_DATE_ONLY = re.compile(r"publicad[oa]\s+(?:em|el)\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.I)
# "12 de novembro de 2026 às 18:42" / "12 de noviembre de 2026"
_RE_WORDS = re.compile(
    r"(\d{1,2})\s+de\s+([a-zçã]+)\s+(?:de|del)\s+(\d{4})(?:[^\d]{1,12}(\d{1,2})[:hH](\d{2}))?",
    re.I,
)


def parse_iso(value: str, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """Returns (datetime, has_time)."""
    v = value.strip()
    if not v:
        return None, False
    if re.fullmatch(r"\d{10}(\d{3})?", v):  # epoch seconds / millis
        ts = int(v[:10])
        return datetime.fromtimestamp(ts, tz=ZoneInfo("UTC")), True
    v = v.replace("Z", "+00:00")
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)  # +0300 -> +03:00
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
        if not m:
            return None, False
        return datetime.combine(date(*map(int, m.groups())), time(0), tz), False
    has_time = bool(re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", v))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt, has_time


def _walk_jsonld(node, found: list[str]) -> None:
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, found)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        is_article = any(t and ("Article" in str(t) or t in ("ReportageNewsArticle", "BlogPosting")) for t in types)
        if is_article and node.get("datePublished"):
            found.insert(0, str(node["datePublished"]))
        elif node.get("datePublished"):
            found.append(str(node["datePublished"]))
        for k in ("@graph", "mainEntity", "mainEntityOfPage"):
            if k in node:
                _walk_jsonld(node[k], found)


def extract_published(page_html: str, country: str | None = None) -> tuple[datetime | None, bool, str]:
    """Find the article's original publish datetime. Returns (dt, has_time, method)."""
    tz = COUNTRY_TZ.get(country or "BR", BRT)
    soup = BeautifulSoup(page_html, "lxml")

    # 1) JSON-LD (most reliable on news sites)
    found: list[str] = []
    for s in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            _walk_jsonld(json.loads(s.string or s.get_text() or "{}"), found)
        except (json.JSONDecodeError, TypeError):
            continue
    for v in found:
        dt, has_time = parse_iso(v, tz)
        if dt:
            return dt, has_time, "json-ld datePublished"

    # 2) meta tags
    for attr, val in _META_PUBLISHED:
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            dt, has_time = parse_iso(tag["content"], tz)
            if dt:
                return dt, has_time, f"meta {val}"

    # 3) <time datetime> inside the article (or itemprop=datePublished)
    t = soup.find(attrs={"itemprop": "datePublished"})
    if t is not None:
        v = t.get("datetime") or t.get("content") or ""
        dt, has_time = parse_iso(v, tz)
        if dt:
            return dt, has_time, "itemprop datePublished"
    scope = soup.find("article") or soup
    t = scope.find("time", datetime=True)
    if t is not None:
        dt, has_time = parse_iso(t["datetime"], tz)
        if dt:
            return dt, has_time, "<time datetime>"

    # 4) visible byline text
    text = (scope.get_text(" ", strip=True) if scope is not soup else soup.get_text(" ", strip=True))[:20000]
    m = _RE_NUMERIC.search(text)
    if m:
        d, mo, y, hh, mm = map(int, m.groups())
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=tz), True, "byline dd/mm/aaaa hh:mm"
        except ValueError:
            pass
    m = _RE_NUMERIC_DATE_ONLY.search(text)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=tz), False, "byline dd/mm/aaaa"
        except ValueError:
            pass
    for m in _RE_WORDS.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if not month:
            continue
        try:
            if m.group(4):
                return datetime(int(m.group(3)), month, int(m.group(1)), int(m.group(4)), int(m.group(5)), tzinfo=tz), True, "byline por extenso"
            return datetime(int(m.group(3)), month, int(m.group(1)), tzinfo=tz), False, "byline por extenso"
        except ValueError:
            continue

    # 5) htmldate as last resort (date only, original date preferred)
    try:
        from htmldate import find_date

        d = find_date(page_html, original_date=True, extensive_search=False)
        if d:
            return datetime.combine(date.fromisoformat(d), time(0), tz), False, "htmldate"
    except Exception:  # pragma: no cover
        pass
    return None, False, ""


def extract_title_and_text(page_html: str) -> tuple[str | None, str]:
    soup = BeautifulSoup(page_html, "lxml")
    title = None
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.find("h1"):
        title = soup.find("h1").get_text(" ", strip=True)
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(page_html, include_comments=False, include_tables=False) or ""
    except Exception:  # pragma: no cover
        text = ""
    if not text:
        body = soup.find("article") or soup.body or soup
        text = "\n".join(p.get_text(" ", strip=True) for p in body.find_all("p"))
    return title, text.strip()


def classify(c: Candidate, window: Window, dt: datetime | None, has_time: bool) -> tuple[str, str]:
    """Decide status from the article-level date (page was opened successfully)."""
    feed_ok = window.contains(c.feed_published)
    if dt is None:
        # Page opened but carries no date at all: we could not verify -> NÃO VERIFICADO if the feed says fresh.
        if feed_ok:
            return UNVERIFIED, "página aberta, mas sem data de publicação na página"
        return NO_DATE, "sem data na página nem no feed"
    if has_time:
        if window.contains(dt):
            return VERIFIED, ""
        if dt > window.end:
            return AMBIGUOUS, f"data da página no futuro ({dt.isoformat()})"
        return STALE, f"publicado em {dt.astimezone(BRT):%d/%m/%Y %H:%M} BRT"
    in_win = window.contains_date(dt.date())
    if in_win is True:
        return VERIFIED, "página informa só a data (sem horário)"
    if in_win is False:
        if dt.date() < window.start.date():
            return STALE, f"publicado em {dt:%d/%m/%Y}"
        return AMBIGUOUS, f"data da página fora da janela ({dt:%d/%m/%Y})"
    # Same calendar day as the window start, no time on the page: use feed timestamp only if it agrees.
    if c.feed_published and feed_ok and c.feed_published.astimezone(BRT).date() == dt.date():
        return VERIFIED, "página informa só a data; horário do feed confirma"
    return AMBIGUOUS, "página informa só a data, no limite da janela, sem horário para confirmar"


def verify(c: Candidate, window: Window) -> Candidate:
    country = c.triage_country or c.country_hint
    if c.needs_gnews_decode:
        c.status, c.verify_note = UNVERIFIED, "link do Google News não pôde ser resolvido"
        if not window.contains(c.feed_published):
            c.status = NO_DATE
        return c
    try:
        r = http.get(c.url, timeout=25)
        page = r.text
        c.http_status = r.status_code
        if http.looks_like_challenge(page):
            raise http.FetchError(c.url, r.status_code, "bot challenge")
    except http.FetchError as e:
        c.http_status = e.status
        if window.contains(c.feed_published):
            c.status, c.verify_note = UNVERIFIED, f"página bloqueada ({e.status or 'erro de conexão'})"
        else:
            c.status, c.verify_note = NO_DATE, f"página bloqueada ({e.status or 'erro'}) e sem data confiável no feed"
        return c
    if r.url and r.url != c.url:
        c.url = r.url
    dt, has_time, method = extract_published(page, country)
    c.article_title, c.article_text = extract_title_and_text(page)
    c.article_published, c.article_date_only, c.date_method = dt, (dt is not None and not has_time), method
    c.status, c.verify_note = classify(c, window, dt, has_time)
    return c
