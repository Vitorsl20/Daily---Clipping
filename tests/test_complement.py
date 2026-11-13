from datetime import date, datetime

from clipping import complement as comp
from clipping import pipeline, verify
from clipping.mailbox import parse_edition
from clipping.models import STALE, UNVERIFIED, VERIFIED
from clipping.render import Entry
from clipping.timewin import BRT

EDITION = """LatAm Financials Clipping — 13/11/2026
Janela de notícias: x

"Itaú anuncia recompra" (InfoMoney, 12/11/2026) — https://www.infomoney.com.br/itau — Sinaliza capital.

Itau Announces Buyback (InfoMoney, Nov 12)
Para.

"Nubank lança "Caixinha" turbo" (Valor Econômico, 12/11/2026) — https://valor.globo.com/nubank — Produto novo. — NÃO VERIFICADO

"Bradesco troca CFO" (Estadão, 12/11/2026) — https://www.estadao.com.br/bradesco — Mudança. — NÃO VERIFICADO

"Velha" (O Globo, 12/11/2026) — https://oglobo.globo.com/velha — x — NÃO VERIFICADO
"""


def test_parse_edition():
    items = parse_edition(EDITION)
    assert [i.unverified for i in items] == [False, True, True, True]
    assert items[1].headline == 'Nubank lança "Caixinha" turbo' and items[1].takeaway == "Produto novo."
    assert items[0].published == date(2026, 11, 12) and items[0].url == "https://www.infomoney.com.br/itau"


def test_complement_flow(monkeypatch, tmp_path):
    now = datetime(2026, 11, 13, 6, 5, tzinfo=BRT)
    monkeypatch.setattr(comp, "DONE_DIR", tmp_path)
    monkeypatch.setattr(comp, "find_edition", lambda subject, day: (parse_edition(EDITION), now.replace(hour=5, minute=31)))

    def fake_verify(c, window):
        c.status = {"https://valor.globo.com/nubank": VERIFIED, "https://oglobo.globo.com/velha": STALE}.get(c.url, UNVERIFIED)
        c.verify_note = "publicado em 10/11/2026" if c.status == STALE else ""
        c.article_published = datetime(2026, 11, 12, 20, 0, tzinfo=BRT)
        return c

    monkeypatch.setattr(verify, "verify", fake_verify)
    seen = {}

    def fake_produce(window, t0, report, extra_usable=None, already_sent=None, record_health=True,
                     candidate_filter=None):
        seen["already"] = already_sent
        seen["filter"] = candidate_filter
        up = extra_usable[0]
        mk = lambda id_, h, url, unv: Entry(id_, h, "Valor Econômico", datetime(2026, 11, 12, 20, tzinfo=BRT), url,
                                            "t", "T", "P", unv, "BR", "banks")
        return ([mk(up.id, up.headline, up.url, False),
                 mk("n1", "Santander compra fintech", "https://valor.globo.com/santander", False),
                 mk("n2", "Itaú anuncia recompra", "https://www.infomoney.com.br/itau", False)], [], [])

    monkeypatch.setattr(pipeline, "produce", fake_produce)
    sent = {}
    monkeypatch.setattr(comp.pipeline, "_write_outputs", lambda *a, **k: tmp_path / "x.txt")
    import clipping.mailer as mailer
    monkeypatch.setattr(mailer, "send", lambda s, t, h=None: sent.update(subject=s, text=t))

    assert comp.complement(now=now) == 1
    txt = sent["text"]
    assert sent["subject"] == "LatAm Financials Clipping — 13/11/2026 — complemento"
    assert "CORREÇÕES" in txt and "Velha" in txt
    assert "AGORA VERIFICADOS" in txt and 'Nubank lança "Caixinha" turbo' in txt
    assert "NOVOS" in txt and "Santander compra fintech" in txt
    assert txt.count('"Itaú anuncia recompra"') == 0                 # already sent at 05:17
    assert "Continuam NÃO VERIFICADOS mesmo do computador: Bradesco troca CFO" in txt
    assert all("Nubank" not in a for a in seen["already"])            # re-verified item isn't a duplicate of itself
    from clipping.models import Candidate
    f = seen["filter"]
    early = datetime(2026, 11, 12, 18, 0, tzinfo=BRT)
    assert not f(Candidate("x", "https://www.infomoney.com.br/itau", "InfoMoney", ["t"], now))     # already sent
    assert f(Candidate("x", "https://exame.com/a", "Exame", ["t"], now))                         # after 05:17
    assert not f(Candidate("x", "https://exame.com/b", "Exame", ["t"], early))                   # judged at 05:17
    assert f(Candidate("x", "https://valor.globo.com/c", "Valor Econômico", ["t"], early))       # blocked in cloud
    # second login the same day: nothing happens
    sent.clear()
    assert comp.complement(now=now) == 0 and not sent


def test_complement_skips_weekend(tmp_path, monkeypatch):
    monkeypatch.setattr(comp, "DONE_DIR", tmp_path)
    assert comp.complement(now=datetime(2026, 11, 14, 6, 5, tzinfo=BRT)) == 0
