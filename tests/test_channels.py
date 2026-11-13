from datetime import datetime, timezone

from clipping import accumulate, gdelt, http, native
from clipping.models import Candidate, SourceResult
from clipping.relevance import build_matcher
from clipping.timewin import BRT, freshness_window

W = freshness_window(datetime(2026, 11, 13, 5, 30, tzinfo=BRT), cutoff_hour=13)

NEWS_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
 <url><loc>https://valor.globo.com/financas/noticia/2026/11/12/itau-recompra.ghtml</loc>
  <news:news><news:publication><news:name>Valor</news:name></news:publication>
  <news:publication_date>2026-11-12T19:40:00-03:00</news:publication_date>
  <news:title>Itaú anuncia recompra de ações</news:title></news:news></url>
 <url><loc>https://valor.globo.com/financas/noticia/2026/11/10/velha.ghtml</loc>
  <news:news><news:publication_date>2026-11-10T10:00:00-03:00</news:publication_date>
  <news:title>Notícia velha</news:title></news:news></url>
</urlset>""".encode()

INDEX = b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://valor.globo.com/sitemap/valor/2026/11/12_1.xml</loc></sitemap>
<sitemap><loc>https://valor.globo.com/sitemap/valor/2026/01/01_1.xml</loc></sitemap></sitemapindex>"""


class Resp:
    def __init__(self, url, body, code=200):
        self.url, self.content, self.status_code = url, body, code
        self.text = body.decode("utf-8")


def test_news_sitemap_parsing_keeps_window_only():
    urls, children = native.parse_sitemap(NEWS_SITEMAP)
    assert not children and len(urls) == 2
    cands = native._sitemap_candidates(urls, "Valor Econômico", "sitemap:Valor", "BR", W)
    assert [c.title for c in cands] == ["Itaú anuncia recompra de ações"]
    assert cands[0].feed_published == datetime(2026, 11, 12, 19, 40, tzinfo=BRT)


def test_sitemap_index_picks_dated_children():
    _, children = native.parse_sitemap(INDEX)
    assert native._pick_children(children, W) == ["https://valor.globo.com/sitemap/valor/2026/11/12_1.xml"]


def test_fetch_native_discovers_from_robots(monkeypatch):
    pages = {
        "https://valor.globo.com/robots.txt": b"User-agent: *\nSitemap: https://valor.globo.com/sitemap/news.xml\n",
        "https://valor.globo.com/sitemap/news.xml": NEWS_SITEMAP,
        "https://valor.globo.com/": b'<html><head><link rel="alternate" type="application/rss+xml" href="/rss/financas"></head></html>',
    }

    def fake_get(url, timeout=25, no_cache=False):
        if url not in pages:
            raise http.FetchError(url, 404)
        return Resp(url, pages[url])

    monkeypatch.setattr(http, "get", fake_get)
    res = native.fetch_native({"outlet": "Valor Econômico", "host": "valor.globo.com"}, W)
    assert res.reachable and len(res.items) == 1 and res.fresh
    assert "sitemap" in res.note


def test_plain_sitemap_lastmod_never_used_as_publish_date():
    urls = [{"loc": "https://x.com/a/banco-lucra-muito", "title": "", "published": "",
             "lastmod": "2026-11-12T20:00:00-03:00"}]
    c = native._sitemap_candidates(urls, "X", "sitemap:X", "BR", W)[0]
    assert c.feed_published is None and c.title == "Banco lucra muito"


def test_gdelt_parse():
    body = '{"articles":[{"url":"https://www.estadao.com.br/economia/a","title":"Bradesco eleva lucro",' \
           '"seendate":"20261112T231500Z"},{"url":"https://www.estadao.com.br/b","title":"Velha","seendate":"20261101T000000Z"}]}'
    items = gdelt.parse_response(body, "Estadão", "BR", W)
    assert [c.title for c in items] == ["Bradesco eleva lucro"]
    assert gdelt.parse_response("Your search was too short", "Estadão", "BR", W) == []


def test_relevance_matcher():
    rel = build_matcher({"companies": [{"name": "Itaú Unibanco", "ticker": "ITUB4", "terms": ['"Itaú Unibanco"', "Itaú"]}]})
    assert rel("Itaú anuncia novo CEO", "https://x/y")
    assert rel("Inadimplência sobe em outubro", "https://x/y")
    assert not rel("Flamengo vence clássico", "https://oglobo.globo.com/esportes/flamengo-vence")


def test_accumulator_keeps_items_that_left_the_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(accumulate, "FILE", tmp_path / "acc.json")
    afternoon = datetime(2026, 11, 12, 16, 0, tzinfo=BRT)
    r = SourceResult(name="Valor Econômico — Finanças", kind="rss_app", reachable=True, newest=afternoon,
                     items=[Candidate(title="Nubank lucra", url="https://v/1", source="Valor Econômico",
                                      discovered_via=["rss.app:Valor"], feed_published=afternoon)])
    data = accumulate.load()
    assert accumulate.add(data, [r], datetime(2026, 11, 12, 17, 0, tzinfo=BRT)) == 1
    accumulate.save(data)
    later = accumulate.load()  # morning: the feed no longer shows it, the accumulator does
    got = accumulate.candidates(later, W)
    assert [c.title for c in got] == ["Nubank lucra"] and "acumulado" in got[0].discovered_via
    assert accumulate.channel_newest(later, "Valor Econômico — Finanças") == afternoon
