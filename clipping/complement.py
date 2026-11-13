"""The 6h complement, run on Vitor's own computer when he turns it on.

From a home connection the paywalled/bot-protected outlets (Valor, O Globo, Estadão…) usually open, so this run:
  1. reads the 05:17 edition back from Gmail;
  2. re-opens every NÃO VERIFICADO item from it — confirmed fresh → "Agora verificados" with a full write-up;
     confirmed older than the window → "Correções" (tells Vitor to disregard it);
  3. runs the whole collection again (all channels, all 36 company searches) and keeps only stories that were
     NOT in the morning edition → "Novos / não captados às 05:17";
  4. emails a short complement — only when there is something to say.
If the morning edition is not found (cloud run failed), it sends the full brief instead.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from . import pipeline, verify
from .mailbox import SentItem, find_edition
from .models import STALE, UNVERIFIED, VERIFIED, Candidate
from .pipeline import ROOT, RunReport, norm_url
from .render import Entry, render_html, render_text
from .timewin import BRT, UTC, freshness_window

log = logging.getLogger(__name__)
DONE_DIR = ROOT / "state"


def _done_marker(day: str) -> Path:
    return DONE_DIR / f"complement-done-{day}"


def complement(now: datetime | None = None, dry_run: bool = False, force: bool = False) -> int:
    t0 = time.time()
    now_brt = (now or datetime.now(UTC)).astimezone(BRT)
    day = now_brt.strftime("%Y-%m-%d")
    latest = int(os.environ.get("COMPLEMENT_LATEST_HOUR", "14"))
    if not force:
        if now_brt.weekday() >= 5:
            log.info("Fim de semana: sem complemento.")
            return 0
        if _done_marker(day).exists():
            log.info("Complemento de hoje já foi feito (%s).", _done_marker(day))
            return 0
        if now_brt.hour >= latest:
            log.info("Depois das %dh: complemento de hoje não roda mais.", latest)
            return 0

    # The morning window — same start as the 05:17 edition, ending now.
    window = freshness_window(now_brt)
    subject = pipeline.subject_line(window)

    morning = _find_morning(subject, window.end.date())
    if morning is None:
        log.warning("Edição das 05:17 não encontrada no Gmail — enviando o clipping completo pelo computador.")
        pipeline.run(dry_run=dry_run, now=now_brt, subject_suffix=" (enviado pelo computador)",
                     extra_note="A edição automática das 05:17 não foi encontrada no Gmail; este clipping completo "
                                "foi gerado e enviado pelo computador ao ligar.")
        _mark_done(day, dry_run)
        return 1
    sent, sent_at = morning
    log.info("Edição das %s encontrada: %d itens (%d NÃO VERIFICADOS).",
             sent_at.strftime("%H:%M") if sent_at else "?", len(sent), sum(s.unverified for s in sent))

    report = RunReport(started=window.end.isoformat(), window=window.describe())

    # 2) re-check every NÃO VERIFICADO item from the morning, now from the home connection
    upgrades, corrections, still_blocked = recheck_unverified(sent, window)

    # 3) full collection again; the editor drops stories already sent
    already = [f"{s.headline} ({s.source})" for s in sent if not any(norm_url(u.url) == norm_url(s.url) for u in upgrades)]
    entries, _, _ = pipeline.produce(window, t0, report, extra_usable=upgrades, already_sent=already,
                                     record_health=False)
    sent_urls = {norm_url(s.url) for s in sent}
    upgrade_ids = {c.id for c in upgrades}
    now_verified = [e for e in entries if e.id in upgrade_ids and not e.unverified]
    new = [e for e in entries if e.id not in upgrade_ids and norm_url(e.url) not in sent_urls]

    notes = [
        f"Complemento gerado pelo computador às {now_brt:%H:%M}, a partir da edição das "
        f"{sent_at:%H:%M} ({len(sent)} itens, {sum(s.unverified for s in sent)} NÃO VERIFICADOS)." if sent_at else
        f"Complemento gerado pelo computador às {now_brt:%H:%M}.",
        f"Dos itens NÃO VERIFICADOS da edição: {len(now_verified)} agora verificados, {len(corrections)} com data "
        f"fora da janela (ver Correções), {len(still_blocked)} continuam sem acesso.",
    ]
    if still_blocked:
        notes.append("Continuam NÃO VERIFICADOS mesmo do computador: " + "; ".join(s.headline[:90] for s in still_blocked))
    notes += [n for n in report.notes if n.startswith(("Canais ", "Busca dedicada", "PRAZO", "Estatísticas"))]
    notes.append(f"Tempo total: {(time.time() - t0) / 60:.1f} min.")

    if not (now_verified or new or corrections) and os.environ.get("COMPLEMENT_SEND_EMPTY", "0") != "1":
        log.info("Nada a acrescentar à edição das 05:17; nenhum e-mail enviado.")
        _mark_done(day, dry_run)
        return 0

    subj = subject + " — complemento"
    text, html_body = render_complement(subj, window.describe(), corrections, now_verified, new, notes)
    pipeline._write_outputs(None, f"clipping-{day}-complemento", text, html_body, report)
    if not dry_run:
        from . import mailer

        mailer.send(subj, text, html_body)
        log.info("Complemento enviado: %s", subj)
    print(text)
    _mark_done(day, dry_run)
    return 1


def _find_morning(subject: str, day) -> tuple[list[SentItem], datetime | None] | None:
    """The cloud edition may land a little late; wait for it up to COMPLEMENT_WAIT_MINUTES before giving up."""
    wait_until = time.time() + 60 * float(os.environ.get("COMPLEMENT_WAIT_MINUTES", "20"))
    while True:
        try:
            found = find_edition(subject, day)
        except Exception as e:  # IMAP down / auth problem: don't block the complement forever
            log.error("Não foi possível ler o Gmail via IMAP: %s", e)
            found = None
            if time.time() > wait_until:
                return None
        if found is not None:
            return found
        if time.time() > wait_until or datetime.now(BRT).hour >= 7:
            return None
        log.info("Edição das 05:17 ainda não chegou; nova tentativa em 2 min.")
        time.sleep(120)


def recheck_unverified(sent: list[SentItem], window) -> tuple[list[Candidate], list[tuple[SentItem, str]], list[SentItem]]:
    upgrades, corrections, still = [], [], []
    for s in sent:
        if not s.unverified:
            continue
        # Only the article page counts now: no feed date is carried over, so a page that still won't open
        # (or carries no date) stays in "continuam NÃO VERIFICADOS" instead of being promoted.
        c = Candidate(title=s.headline, url=s.url, source=s.source, discovered_via=["edição 05:17"])
        verify.verify(c, window)
        if c.status == VERIFIED:
            upgrades.append(c)
        elif c.status == STALE:
            corrections.append((s, c.verify_note))
        else:
            still.append(s)
    return upgrades, corrections, still


def render_complement(subject: str, window_desc: str, corrections, now_verified: list[Entry], new: list[Entry],
                      notes: list[str]) -> tuple[str, str]:
    sections_txt, sections_html = [], []
    if corrections:
        lines = ["CORREÇÕES — desconsiderar estes itens da edição das 05:17 (data confirmada fora da janela):", ""]
        lines += [f'• "{s.headline}" ({s.source}) — {s.url} — {note}' for s, note in corrections]
        sections_txt.append("\n".join(lines))
        sections_html.append("<h3 style=\"color:#b00020\">Correções — desconsiderar da edição das 05:17</h3><ul>" +
                             "".join(f"<li>\"{_e(s.headline)}\" ({_e(s.source)}) — {_e(note)}</li>" for s, note in corrections)
                             + "</ul>")
    for title, items in (("AGORA VERIFICADOS (substituem os itens NÃO VERIFICADOS da edição das 05:17)", now_verified),
                         ("NOVOS — não estavam na edição das 05:17", new)):
        if not items:
            continue
        body = render_text(items, title, window_desc, [])
        sections_txt.append(title + "\n\n" + "\n".join(body.splitlines()[2:]).strip())  # inner header dropped
        h = render_html(items, title, window_desc, [])
        h = h.replace(f"<div style=\"color:#555;margin-bottom:16px\">Janela de notícias: {_e(window_desc)}</div>", "")
        sections_html.append(h.replace("<html><body", "<div").replace("</body></html>", "</div>"))
    header = f"{subject}\nJanela de notícias: {window_desc}\n"
    text = header + "\n\n".join(f"{'#' * 60}\n{s}" for s in sections_txt) + \
        "\n\n" + "-" * 60 + "\nNOTAS\n" + "-" * 60 + "\n" + "\n".join(f"• {n}" for n in notes) + "\n"
    html_body = (f"<html><body style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;max-width:860px\">"
                 f"<h2>{_e(subject)}</h2><div style=\"color:#555\">Janela de notícias: {_e(window_desc)}</div>"
                 + "".join(sections_html) + "<h4>Notas</h4><ul style=\"color:#444\">"
                 + "".join(f"<li>{_e(n)}</li>" for n in notes) + "</ul></body></html>")
    return text, html_body


def _e(s: str) -> str:
    import html

    return html.escape(s)


def _mark_done(day: str, dry_run: bool) -> None:
    if dry_run:
        return
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    _done_marker(day).write_text(datetime.now(BRT).isoformat(), encoding="utf-8")
