from datetime import datetime

from clipping.render import Entry, render_text
from clipping.timewin import BRT


def test_three_line_format_and_flag():
    e = Entry(id="1", headline="Itaú anuncia recompra", source="Valor Econômico",
              published=datetime(2026, 8, 28, 18, 0, tzinfo=BRT), url="https://v/1",
              takeaway_pt="Sinaliza confiança.", title_en="Itaú Announces Buyback", paragraph_en="Para.",
              unverified=True, country="BR", segment="banks")
    assert e.line1() == '"Itaú anuncia recompra" (Valor Econômico, 28/08/2026) — https://v/1 — Sinaliza confiança. — NÃO VERIFICADO'
    assert e.line2() == "Itaú Announces Buyback (Valor Econômico, Aug 28)"
    txt = render_text([e], "LatAm Financials Clipping — 29/08/2026", "w", ["nota"])
    assert "BRASIL" in txt and "— Bancos —" in txt and "NOTAS DE COBERTURA" in txt
