"""Cheap keyword prefilter for high-volume, general-interest channels (whole-site sitemaps, GDELT).

Only applied to channels flagged `prefilter: true`; everything else goes to the editor untouched.
Deliberately broad: it only removes items with no financial-sector word and no covered-company name."""
from __future__ import annotations

import re
import unicodedata

SECTOR_TERMS = [
    "banco", "bancos", "bancario", "bancaria", "banca", "financeir", "financ", "fintech", "seguro", "segurador",
    "resseguro", "previdencia", "credito", "inadimplencia", "juros", "selic", "copom", "banco central", "bacen",
    "cvm", "susep", "b3", "bolsa", "acoes", "ipo", "follow-on", "debenture", "emissao", "fundo", "investimento",
    "pix", "cartao", "consignado", "pagamento", "maquininha", "corretora", "febraban", "lucro", "balanco",
    "resultado", "dividendo", "recompra", "fusao", "aquisicao", "liquidacao", "fgc",
    "aseguradora", "cnbv", "banxico", "bcra", "sbs", "superfinanciera", "bcrp", "banco de la republica",
]


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def build_matcher(companies: dict):
    names = set()
    for co in companies.get("companies", []):
        names.add(co["name"])
        names.add(co.get("ticker", ""))
        for t in co.get("terms", []):
            names.add(t.strip('"').split('"')[0])
    terms = [_fold(t) for t in SECTOR_TERMS + [n for n in names if n and len(n) >= 3]]
    rx = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True)) + ")")

    def relevant(title: str, url: str) -> bool:
        text = _fold(title) + " " + _fold(url.replace("-", " ").replace("/", " "))
        return bool(rx.search(text))

    return relevant
