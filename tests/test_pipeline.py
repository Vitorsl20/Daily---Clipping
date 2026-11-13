"""End-to-end run with network and Claude mocked."""
from datetime import datetime

from clipping import editor, http, pipeline, search, state
from clipping.models import Candidate, FeedAttempt, SourceResult
from clipping.timewin import BRT, UTC

NOW = datetime(2026, 11, 13, 5, 30, tzinfo=BRT)


def c(title, url, src, feed_dt, via, country="BR"):
    return Candidate(title=title, url=url, source=src, discovered_via=[via], feed_published=feed_dt, country_hint=country)


ARTICLES = {
    "https://www.infomoney.com.br/itau-recompra": (
        200, '<meta property="article:published_time" content="2026-11-12T21:00:00Z">'
             '<meta property="og:title" content="Itaú anuncia recompra de R$ 3 bi"><article><p>O Itaú aprovou recompra.</p></article>'),
    "https://valor.globo.com/financas/nubank": (403, ""),
    "https://www.infomoney.com.br/velha": (
        200, '<meta property="article:published_time" content="2026-11-09T12:00:00Z"><article><p>Velha.</p></article>'),
    "https://www.infomoney.com.br/cripto": (
        200, '<meta property="article:published_time" content="2026-11-12T22:00:00Z"><article><p>Bitcoin.</p></article>'),
}


class Resp:
    def __init__(self, url, code, text):
        self.url, self.status_code, self.text = url, code, text
        self.content = text.encode()


def fake_get(url, timeout=25, no_cache=False):
    code, text = ARTICLES[url]
    if code >= 400:
        raise http.FetchError(url, code)
    return Resp(url, code, text)


def fake_call(task, payload, schema, effort):
    import json

    rows = [json.loads(l) for l in payload.splitlines() if l.strip()]
    if task.startswith("Triage"):
        return {"items": [{"id": r["id"], "keep": "Bitcoin" not in r["headline"], "reason": "in_scope", "country": "BR"}
                          for r in rows]}
    if task.startswith("FINAL QC"):
        return {"items": [{"id": r["id"], "verdict": "ok", "problem": "", "takeaway_pt": "", "title_en": "",
                           "paragraph_en": ""} for r in rows]}
    return {"items": [{"id": r["id"], "include": True, "exclusion_reason": "none", "duplicate_of": "", "country": "BR",
                       "segment": "banks" if "Itaú" in r["headline"] else "fintechs",
                       "takeaway_pt": "Relevante.", "title_en": "EN " + r["headline"], "paragraph_en": "Fact."}
                      for r in rows]}


def test_end_to_end(monkeypatch, tmp_path):
    fresh = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)
    rss = [SourceResult(name="Valor Econômico — Finanças", kind="rss_app", reachable=True, fresh=True, newest=fresh,
                        attempts=[FeedAttempt("u?_=1", True, 2, fresh)],
                        items=[c("Nubank lança produto", "https://valor.globo.com/financas/nubank", "Valor Econômico",
                                 fresh, "rss.app:Valor")]),
           SourceResult(name="Estadão — Economia", kind="rss_app", reachable=True, fresh=False,
                        newest=datetime(2026, 11, 4, tzinfo=UTC), attempts=[FeedAttempt("u", True, 1)] * 5,
                        items=[c("Antiga", "https://estadao.com.br/x", "Estadão", datetime(2026, 11, 4, tzinfo=UTC), "rss.app:Estadão")])]
    others = [SourceResult(name="InfoMoney", kind="outlet", reachable=True, fresh=True, newest=fresh, items=[
        c("Itaú anuncia recompra de R$ 3 bi", "https://www.infomoney.com.br/itau-recompra", "InfoMoney", fresh, "outlet:InfoMoney"),
        c("Matéria que parece nova", "https://www.infomoney.com.br/velha", "InfoMoney", fresh, "outlet:InfoMoney"),
        c("Bitcoin dispara", "https://www.infomoney.com.br/cripto", "InfoMoney", fresh, "outlet:InfoMoney"),
    ]), SourceResult(name="Itaú Unibanco [BR]", kind="search", reachable=True, items=[
        c("Itaú anuncia recompra de R$ 3 bi", "https://news.google.com/rss/articles/abc", "InfoMoney", fresh, "gnews:Itaú"),
    ])]
    monkeypatch.setattr(pipeline, "collect", lambda w, s, co, d=None: (rss, others))
    monkeypatch.setattr(http, "get", fake_get)
    monkeypatch.setattr(editor, "_call", fake_call)
    monkeypatch.setattr(search, "decode_google_news", lambda cands: None)
    monkeypatch.setattr(search, "news_search", lambda *a, **k: SourceResult(name="x", kind="search"))
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "feed_health.json")

    out = pipeline.run(dry_run=True, now=NOW, out_dir=tmp_path)
    txt = out.read_text()
    assert '"Itaú anuncia recompra de R$ 3 bi" (InfoMoney, 12/11/2026)' in txt
    assert "NÃO VERIFICADO" in txt and "Nubank lança produto" in txt          # blocked page kept, flagged
    assert "Matéria que parece nova" not in txt                               # stale at article level
    assert "Bitcoin" not in txt and "Antiga" not in txt                       # crypto + stale feed item
    assert txt.count("\"Itaú anuncia recompra") == 1                           # feed + search merged
    assert "Estadão — Economia só retornou itens até 03/11" in txt            # stuck feed reported
    assert "Busca dedicada" not in txt.split("NOTAS")[0]
