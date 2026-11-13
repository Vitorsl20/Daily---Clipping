"""Render the clipping as plain text (always) and HTML (convenience)."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from .timewin import fmt_en, fmt_pt

COUNTRY_ORDER = ["BR", "MX", "AR", "CO", "PE", "CL", "UY", "PA", "OTHER_LATAM"]
COUNTRY_NAMES = {
    "BR": "BRASIL", "MX": "MÉXICO", "AR": "ARGENTINA", "CO": "COLÔMBIA", "PE": "PERU",
    "CL": "CHILE", "UY": "URUGUAI", "PA": "PANAMÁ", "OTHER_LATAM": "OUTROS LATAM",
}
SEGMENT_ORDER = ["banks", "fintechs", "insurers", "exchanges_capital_markets", "regulation_credit", "other"]
SEGMENT_NAMES = {
    "banks": "Bancos", "fintechs": "Fintechs", "insurers": "Seguradoras",
    "exchanges_capital_markets": "Bolsas e mercado de capitais",
    "regulation_credit": "Regulação e crédito", "other": "Outros",
}
FLAG = " — NÃO VERIFICADO"


@dataclass
class Entry:
    id: str
    headline: str
    source: str
    published: datetime
    url: str
    takeaway_pt: str
    title_en: str
    paragraph_en: str
    unverified: bool
    country: str
    segment: str

    def line1(self) -> str:
        takeaway = self.takeaway_pt.strip().rstrip()
        s = f'"{self.headline}" ({self.source}, {fmt_pt(self.published)}) — {self.url} — {takeaway}'
        return s + (FLAG if self.unverified else "")

    def line2(self) -> str:
        return f"{self.title_en.strip()} ({self.source}, {fmt_en(self.published)})"

    def line3(self) -> str:
        return " ".join(self.paragraph_en.split())


def ordered(entries: list[Entry]) -> list[tuple[str, list[tuple[str, list[Entry]]]]]:
    out = []
    for country in COUNTRY_ORDER:
        in_country = [e for e in entries if e.country == country]
        if not in_country:
            continue
        groups = []
        for seg in SEGMENT_ORDER:
            items = sorted((e for e in in_country if e.segment == seg), key=lambda e: e.published, reverse=True)
            if items:
                groups.append((seg, items))
        out.append((country, groups))
    return out


def render_text(entries: list[Entry], subject: str, window_desc: str, notes: list[str]) -> str:
    lines = [subject, f"Janela de notícias: {window_desc}", ""]
    if not entries:
        lines += ["Nenhuma notícia dentro das regras foi encontrada nesta janela.", ""]
    for country, groups in ordered(entries):
        lines += ["=" * 60, COUNTRY_NAMES[country], "=" * 60, ""]
        for seg, items in groups:
            if country == "BR" or len(groups) > 1:
                lines += [f"— {SEGMENT_NAMES[seg]} —", ""]
            for e in items:
                lines += [e.line1(), "", e.line2(), e.line3(), ""]
    if notes:
        lines += ["-" * 60, "NOTAS DE COBERTURA", "-" * 60]
        lines += [f"• {n}" for n in notes]
    return "\n".join(lines).rstrip() + "\n"


def render_html(entries: list[Entry], subject: str, window_desc: str, notes: list[str]) -> str:
    esc = html.escape
    parts = [
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111;max-width:860px\">",
        f"<h2 style=\"margin-bottom:4px\">{esc(subject)}</h2>",
        f"<div style=\"color:#555;margin-bottom:16px\">Janela de notícias: {esc(window_desc)}</div>",
    ]
    if not entries:
        parts.append("<p>Nenhuma notícia dentro das regras foi encontrada nesta janela.</p>")
    for country, groups in ordered(entries):
        parts.append(f"<h3 style=\"border-bottom:2px solid #003b70;padding-bottom:2px\">{esc(COUNTRY_NAMES[country])}</h3>")
        for seg, items in groups:
            if country == "BR" or len(groups) > 1:
                parts.append(f"<h4 style=\"margin:12px 0 6px;color:#003b70\">{esc(SEGMENT_NAMES[seg])}</h4>")
            for e in items:
                flag = "<b style=\"color:#b00020\">" + esc(FLAG) + "</b>" if e.unverified else ""
                parts.append(
                    "<div style=\"margin-bottom:14px\">"
                    f"<div>\"{esc(e.headline)}\" ({esc(e.source)}, {fmt_pt(e.published)}) — "
                    f"<a href=\"{esc(e.url, quote=True)}\">{esc(e.url)}</a> — {esc(e.takeaway_pt)}{flag}</div>"
                    f"<div style=\"margin-top:6px;font-weight:bold\">{esc(e.line2())}</div>"
                    f"<div>{esc(e.line3())}</div></div>"
                )
    if notes:
        parts.append("<h4 style=\"margin-top:24px\">Notas de cobertura</h4><ul style=\"color:#444\">")
        parts += [f"<li>{esc(n)}</li>" for n in notes]
        parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)
