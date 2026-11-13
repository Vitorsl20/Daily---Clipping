from datetime import datetime, timedelta

from clipping import feeds, http
from clipping.timewin import BRT, freshness_window

W = freshness_window(datetime(2026, 11, 13, 5, 30, tzinfo=BRT), cutoff_hour=13)


def rss(*items):
    body = "".join(
        f"<item><title>{t}</title><link>https://ex.com/{t.replace(' ', '-')}</link><pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>"
        for i, (t, d) in enumerate(items)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


class Resp:
    def __init__(self, content):
        self.content = content
        self.text = content.decode()
        self.status_code = 200
        self.url = ""


def test_rss_app_retry_until_fresh(monkeypatch):
    stale = rss(("Velho", datetime(2026, 11, 5, 12)))
    fresh = rss(("Novo banco anuncia", datetime(2026, 11, 13, 1)))
    calls = []

    def fake_get(url, timeout=25, no_cache=False):
        calls.append(url)
        return Resp(stale if len(calls) == 1 else fresh)

    monkeypatch.setattr(http, "get", fake_get)
    clock = iter([1000, 1000, 1000])
    res = feeds.fetch_rss_app("Feed", "Outlet", "https://rss.app/feeds/abc.xml", W,
                              sleep=lambda s: None, clock=lambda: next(clock))
    assert res.fresh and len(calls) == 2
    assert calls[0].endswith("?_=1000") and calls[1].endswith("?_=1001")  # distinct, single clean param
    assert all(c.count("?") == 1 and "&" not in c for c in calls)
    assert {c.title for c in res.items} == {"Velho", "Novo banco anuncia"}  # merged across attempts


def test_rss_app_reports_stuck_after_all_attempts(monkeypatch):
    stale = rss(("Velho", datetime(2026, 11, 5, 12)))
    monkeypatch.setattr(http, "get", lambda url, timeout=25, no_cache=False: Resp(stale))
    res = feeds.fetch_rss_app("Feed", "Outlet", "https://rss.app/feeds/abc.xml", W, sleep=lambda s: None)
    assert res.reachable and res.fresh is False and len(res.attempts) == 3
    assert res.newest.date().isoformat() == "2026-11-05"


def test_listing_link_extraction():
    html = """<a href="/mercado/itau-anuncia-recompra-de-acoes-de-r-3-bilhoes">Itaú anuncia recompra de ações de R$ 3 bilhões</a>
              <a href="/tag/itau">Itaú</a><a href="https://other.com/a-b-c-d">Externo com título bem longo aqui</a>"""
    items = feeds.extract_listing_links(html, "https://www.site.com.br/", "Site", "outlet:Site", "BR")
    assert [c.url for c in items] == ["https://www.site.com.br/mercado/itau-anuncia-recompra-de-acoes-de-r-3-bilhoes"]
