from datetime import datetime

from clipping.models import AMBIGUOUS, STALE, UNVERIFIED, VERIFIED, Candidate
from clipping.timewin import BRT, freshness_window
from clipping.verify import classify, extract_published

W = freshness_window(datetime(2026, 11, 13, 5, 30, tzinfo=BRT), cutoff_hour=13)


def test_jsonld_prefers_datepublished_over_modified():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[{"@type":"NewsArticle","datePublished":"2026-11-12T18:42:00-03:00",
    "dateModified":"2026-11-13T04:00:00-03:00"}]}</script></head><body></body></html>"""
    dt, has_time, method = extract_published(html)
    assert dt == datetime(2026, 11, 12, 18, 42, tzinfo=BRT) and has_time and "json-ld" in method


def test_meta_published_time():
    html = '<meta property="article:modified_time" content="2026-11-13T08:00:00Z">' \
           '<meta property="article:published_time" content="2026-11-12T21:10:00Z">'
    dt, has_time, _ = extract_published(html)
    assert dt.astimezone(BRT).hour == 18 and has_time


def test_byline_publicado_em():
    html = "<article><p>Por Fulano</p><p>Publicado em 12/11/2026 - 18:42</p><p>Texto</p></article>"
    dt, has_time, _ = extract_published(html, "BR")
    assert dt == datetime(2026, 11, 12, 18, 42, tzinfo=BRT) and has_time


def test_byline_finsiders_at():
    html = "<article><span>28/08/2026 at 13:07</span></article>"
    dt, has_time, _ = extract_published(html, "BR")
    assert (dt.day, dt.month, dt.hour, dt.minute) == (28, 8, 13, 7)


def test_byline_spanish_words():
    html = "<article><p>12 de noviembre de 2026 - 19:05</p></article>"
    dt, has_time, _ = extract_published(html, "AR")
    assert (dt.day, dt.month, dt.hour) == (12, 11, 19) and has_time


def _cand(feed=None):
    return Candidate(title="t", url="https://x/y", source="S", discovered_via=["test"], feed_published=feed)


def test_classify_cases():
    inside = datetime(2026, 11, 12, 19, 0, tzinfo=BRT)
    old = datetime(2026, 11, 10, 19, 0, tzinfo=BRT)
    assert classify(_cand(), W, inside, True)[0] == VERIFIED
    assert classify(_cand(inside), W, old, True)[0] == STALE  # page beats a fresh-looking feed date
    assert classify(_cand(), W, datetime(2026, 11, 13, tzinfo=BRT), False)[0] == VERIFIED
    # date-only on the boundary day: needs the feed time to agree
    boundary = datetime(2026, 11, 12, tzinfo=BRT)
    assert classify(_cand(), W, boundary, False)[0] == AMBIGUOUS
    assert classify(_cand(inside), W, boundary, False)[0] == VERIFIED
    # page opened but no date: NÃO VERIFICADO only if the feed says fresh
    assert classify(_cand(inside), W, None, False)[0] == UNVERIFIED
