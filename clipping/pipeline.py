"""Orchestrates one clipping run end to end."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml

from . import accumulate, editor, feeds, gdelt, native, search, state, verify
from .relevance import build_matcher
from .models import STALE, UNVERIFIED, VERIFIED, Candidate, SourceResult
from .render import Entry, render_html, render_text
from .timewin import BRT, Window, freshness_window

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
STOPWORDS = set("""a o as os de da do das dos e em no na nos nas um uma para por com sem que se ao à às é
el la los las del y en un una por con sin que al es para su sus""".split())


@dataclass
class RunReport:
    started: str = ""
    window: str = ""
    sources: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)


def load_config() -> tuple[dict, dict]:
    sources = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    companies = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text(encoding="utf-8"))
    return sources, companies


# ------------------------------------------------------------------ helpers

def norm_url(u: str) -> str:
    p = urlparse(u)
    return urlunparse((p.scheme.lower(), p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), "", "", ""))


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", t)
    return " ".join(words[:14])


def merge_duplicates(cands: list[Candidate]) -> list[Candidate]:
    """Collapse the same article found by several channels (feed + search, etc.)."""
    by_key: dict[str, Candidate] = {}
    alias: dict[str, str] = {}
    out: list[Candidate] = []
    for c in cands:
        nt = norm_title(c.title)
        keys = [f"t:{nt}"] if len(nt.split()) >= 5 else []  # very short titles are too generic to match on
        if not c.needs_gnews_decode:
            keys.append(f"u:{norm_url(c.url)}")
        hit = next((by_key[alias[k]] for k in keys if k in alias), None)
        if hit is None:
            out.append(c)
            for k in keys:
                alias[k] = c.id
            by_key[c.id] = c
            continue
        hit.discovered_via += [v for v in c.discovered_via if v not in hit.discovered_via]
        if hit.needs_gnews_decode and not c.needs_gnews_decode:
            hit.url, hit.needs_gnews_decode = c.url, False
        if c.feed_published and (not hit.feed_published or c.feed_published < hit.feed_published):
            hit.feed_published = c.feed_published
        if len(c.summary) > len(hit.summary):
            hit.summary = c.summary
        if hit.source in ("Google News", "Bing News") or "." in hit.source and "." not in c.source:
            hit.source = c.source
        hit.company_hint = hit.company_hint or c.company_hint
        hit.country_hint = hit.country_hint or c.country_hint
        for k in keys:
            alias.setdefault(k, hit.id)
    return out


def _name_outlets(cands: list[Candidate], sources: dict) -> None:
    """Bing results only carry a domain; map known domains to outlet names."""
    names = {s["domain"].split("/")[0]: s["outlet"] for s in sources.get("rss_app_site_queries", [])}
    for s in sources.get("secondary", []) + sources.get("outlets", []):
        if s.get("site_query"):
            names.setdefault(s["site_query"].split("/")[0], s["name"].replace(" (BR)", ""))
    for c in cands:
        if "." in c.source and " " not in c.source:
            host = c.source.removeprefix("www.")
            c.source = next((v for d, v in names.items() if host == d or host.endswith("." + d)), host)
        c.source = c.source.replace(" (BR)", "")


def keywords(title: str, n: int = 6) -> str:
    words = [w for w in re.findall(r"[\wÀ-ÿ$%]+", title) if w.lower() not in STOPWORDS and len(w) > 2]
    return " ".join(words[:n])


# ------------------------------------------------------------------ collection

def collect(window: Window, sources: dict, companies: dict, deadline: float | None = None,
            include_searches: bool = True) -> tuple[list[SourceResult], list[SourceResult]]:
    """Every channel, every run: RSS.app, the outlets' native sitemaps/RSS, GDELT, secondary sources,
    other outlets and (unless include_searches=False, as in the hourly accumulator) all news searches."""
    when_days = 4 if window.monday else 2
    kw = sources.get("site_query_keywords", {})
    searches: list[tuple[str, str, str, str | None, str | None]] = []  # label, query, edition, country, company

    for co in companies["companies"]:
        q = search.or_query(co["terms"])
        editions = [co["country"]] + list(co.get("extra_countries", []))
        for ed in editions:
            searches.append((f"{co['name']} [{ed}]", q, ed, co["country"], co["name"]))
        if co.get("english"):
            searches.append((f"{co['name']} [EN]", q, "US", co["country"], co["name"]))
    for country, qs in companies.get("sector_queries", {}).items():
        for q in qs:
            ed = country if country in search.EDITIONS else "US"
            searches.append((f"setor {country}: {q}", q, ed, None if country == "OTHER" else country, None))
    site_cfgs = list(sources.get("rss_app_site_queries", []))
    for s in sources.get("secondary", []) + sources.get("outlets", []):
        if s.get("site_query"):
            site_cfgs.append({"outlet": s["name"], "domain": s["site_query"], "country": s.get("country", "BR")})
    for s in site_cfgs:
        country = s.get("country", "BR")
        lang_kw = kw.get("pt" if country == "BR" else "es", "")
        ed = country if country in search.EDITIONS else "US"
        searches.append((f"site:{s['domain']}", f"site:{s['domain']} {lang_kw}".strip(), ed,
                         None if country == "OTHER" else country, None))

    if not include_searches:
        searches = []
    site_outlet = {f"site:{s['domain']}": s["outlet"] for s in site_cfgs}

    rss_results: list[SourceResult] = []
    other_results: list[SourceResult] = []
    with ThreadPoolExecutor(max_workers=10) as feed_pool, ThreadPoolExecutor(max_workers=8) as src_pool, \
            ThreadPoolExecutor(max_workers=int(os.environ.get("SEARCH_CONCURRENCY", "6"))) as search_pool, \
            ThreadPoolExecutor(max_workers=1) as gdelt_pool:
        rss_futs = [feed_pool.submit(feeds.fetch_rss_app, f["name"], f["outlet"], f["url"], window)
                    for f in sources["rss_app"]]
        gdelt_fut = gdelt_pool.submit(gdelt.fetch_all, sources.get("gdelt_domains", []), window)
        src_futs = [src_pool.submit(native.fetch_native, s, window) for s in sources.get("native_sites", [])]
        src_futs += [src_pool.submit(feeds.fetch_source, s, "secondary", window) for s in sources.get("secondary", [])]
        src_futs += [src_pool.submit(feeds.fetch_source, s, "outlet", window) for s in sources.get("outlets", [])]
        srch_futs = [search_pool.submit(search.news_search, label, q, ed, when_days, country, company, deadline)
                     for label, q, ed, country, company in searches]
        for f in as_completed(src_futs + srch_futs):
            other_results.append(f.result())
        other_results += gdelt_fut.result()
        for f in rss_futs:
            rss_results.append(f.result())

    outlet_of = {f["name"]: f["outlet"] for f in sources["rss_app"]}
    for r in rss_results:
        r.outlet = outlet_of.get(r.name, "")
    for r in other_results:
        if r.kind == "search" and r.name in site_outlet:
            r.outlet = site_outlet[r.name]

    # General-interest channels carry the whole site (sports, politics…): keep only plausibly relevant items.
    relevant = build_matcher(companies)
    prefilter_native = {s["outlet"] for s in sources.get("native_sites", []) if s.get("prefilter")}
    for r in other_results:
        if (r.kind == "native" and r.outlet in prefilter_native) or (r.kind == "gdelt" and r.note == "prefilter"):
            r.items = [c for c in r.items if relevant(c.title, c.url)]

    # Final pass: minutes have passed since the first attempts; give any still-stale RSS.app feed
    # two more clean cache-busted tries before calling it stuck.
    stale = [r for r in rss_results if not r.fresh]
    if stale:
        cfg = {f["name"]: f for f in sources["rss_app"]}
        with ThreadPoolExecutor(max_workers=10) as pool:
            redo = {r.name: pool.submit(feeds.fetch_rss_app, r.name, cfg[r.name]["outlet"], cfg[r.name]["url"],
                                        window, 2, (30.0,), prior=r) for r in stale}
        rss_results = [redo[r.name].result() if r.name in redo else r for r in rss_results]
    return rss_results, other_results


# ------------------------------------------------------------------ notes

def coverage_notes(window: Window, rss: list[SourceResult], others: list[SourceResult], companies: dict,
                   feed_state: dict, run_day: str, acc: dict | None = None) -> list[str]:
    notes: list[str] = []
    for r in rss:
        tries = len(r.attempts)
        if not r.reachable:
            errs = "; ".join(sorted({a.error for a in r.attempts if a.error}))[:200]
            streak = state.record(feed_state, r.name, True, None, run_day)
            notes.append(f"Feed RSS.app {r.name}: INACESSÍVEL em {tries} tentativas ({errs}). Execuções seguidas com problema: {streak}.")
        elif not r.fresh:
            streak = state.record(feed_state, r.name, True, r.newest, run_day)
            newest = r.newest.astimezone(BRT).strftime("%d/%m %H:%M") if r.newest else "sem datas"
            since = feed_state[r.name].get("stale_since")
            seen = accumulate.channel_newest(acc or {}, r.name)
            if seen and (not r.newest or seen > r.newest):
                notes.append(f"Feed RSS.app {r.name}: nas coletas ao longo da noite chegou a mostrar itens até "
                             f"{seen.astimezone(BRT):%d/%m %H:%M} BRT (guardados e usados no clipping).")
            notes.append(
                f"Feed RSS.app {r.name} só retornou itens até {newest} BRT após {tries} tentativas com cache-buster "
                f"distintos — possível feed travado no RSS.app, vale regenerar/checar "
                f"({streak} execução(ões) seguida(s) assim{', desde ' + since if since and streak > 1 else ''})."
            )
        else:
            state.record(feed_state, r.name, False, r.newest, run_day)

    notes += channel_matrix(window, rss, others)
    g = [r for r in others if r.kind == "gdelt"]
    if g and not any(r.reachable for r in g):
        notes.append("GDELT inacessível nesta execução (canal complementar; demais canais seguiram normalmente).")
    nat_down = [r.outlet for r in others if r.kind == "native" and not r.reachable]
    if nat_down:
        notes.append("Canais nativos (robots.txt/sitemap/RSS próprio) bloqueados para: " + ", ".join(nat_down) + ".")

    for r in others:
        if r.kind in ("search", "native", "gdelt", "accumulated"):
            continue
        if not r.reachable:
            notes.append(f"Fonte {r.name}: inacessível (feed e listagem falharam) — coberta só via busca site: no Google News.")
        elif r.fresh is False:
            newest = r.newest.astimezone(BRT).strftime("%d/%m %H:%M") if r.newest else "?"
            notes.append(f"Fonte {r.name}: acessível, mas o item mais recente é de {newest} BRT (fora da janela).")
        elif not r.items:
            notes.append(f"Fonte {r.name}: acessível, mas nenhum item extraído.")

    company_names = {c["name"] for c in companies["companies"]}
    skipped = [r.name for r in others if r.kind == "search" and r.note == "skipped_deadline"]
    if skipped:
        notes.append("PRAZO DE COLETA ATINGIDO — buscas NÃO executadas hoje: " + "; ".join(skipped))
    failed = [r.name for r in others if r.kind == "search" and not r.reachable and r.note != "skipped_deadline"]
    failed_cos = sorted({n.split(" [")[0] for n in failed if n.split(" [")[0] in company_names})
    if failed_cos:
        notes.append("Busca dedicada FALHOU (Google e Bing) para: " + ", ".join(failed_cos) + " — checar manualmente.")
    other_failed = [n for n in failed if n.split(" [")[0] not in company_names]
    if other_failed:
        notes.append(f"{len(other_failed)} buscas setoriais/site: falharam: " + "; ".join(other_failed[:12]))
    total_co = len({r.name.split(' [')[0] for r in others if r.kind == 'search' and r.reachable
                    and r.name.split(' [')[0] in company_names})
    notes.append(f"Buscas dedicadas executadas para {total_co}/{len(company_names)} empresas do universo de cobertura.")
    return notes


def channel_matrix(window: Window, rss: list[SourceResult], others: list[SourceResult]) -> list[str]:
    """Per RSS.app outlet: how many in-window items each independent channel delivered. Shows at a glance
    whether a stuck RSS.app feed actually cost coverage."""
    labels = [("rss_app", "RSS.app"), ("native", "sitemap/RSS próprio"), ("gdelt", "GDELT"),
              ("search", "Google site:"), ("accumulated", "coleta de hora em hora")]
    notes = []
    for outlet in dict.fromkeys(r.outlet for r in rss if r.outlet):
        parts = []
        for kind, label in labels:
            rs = [r for r in rss + others if r.kind == kind and (r.outlet == outlet or (
                kind == "accumulated"))]
            if not rs:
                continue
            items = [c for r in rs for c in r.items
                     if (kind != "accumulated" or c.source == outlet) and window.contains(c.feed_published)]
            if kind == "native" and rs and not any(r.reachable for r in rs):
                parts.append(f"{label} inacessível")
            else:
                parts.append(f"{label} {len(items)}")
        notes.append(f"Canais {outlet} (itens na janela): " + " · ".join(parts))
    return notes


# ------------------------------------------------------------------ main flow

def collect_only(now: datetime | None = None) -> int:
    """Hourly accumulator run: collect feeds/sitemaps/GDELT (no searches, no Claude) and store everything dated."""
    window = freshness_window(now)
    sources, companies = load_config()
    rss, others = collect(window, sources, companies, include_searches=False)
    data = accumulate.load()
    new = accumulate.add(data, rss + others, window.end)
    accumulate.save(data)
    fresh = {r.name: (r.newest.astimezone(BRT).strftime("%d/%m %H:%M") if r.newest else "-") for r in rss}
    log.info("Acumulador: %d itens novos guardados (total %d). RSS.app mais recente por feed: %s",
             new, len(data["items"]), fresh)
    return new


def run(dry_run: bool = False, now: datetime | None = None, out_dir: Path | None = None) -> Path:
    t0 = time.time()
    window = freshness_window(now)
    run_day = window.end.strftime("%Y-%m-%d")
    deadline = t0 + 60 * float(os.environ.get("CLIPPING_DEADLINE_MINUTES", "25"))
    sources, companies = load_config()
    report = RunReport(started=window.end.isoformat(), window=window.describe())
    log.info("Janela: %s", window.describe())

    # 1) collect from every source, every run
    collect_deadline = t0 + 60 * float(os.environ.get("COLLECT_DEADLINE_MINUTES", "12"))
    rss, others = collect(window, sources, companies, collect_deadline)
    acc = accumulate.load()
    accumulate.add(acc, rss + others, window.end)
    accumulate.save(acc)
    acc_items = accumulate.candidates(acc, window)
    others.append(SourceResult(name="Coleta de hora em hora (acumulado)", kind="accumulated", items=acc_items,
                               reachable=True, fresh=bool(acc_items)))
    report.notes.append(f"Coleta de hora em hora: {accumulate.runs_in_window(acc, window)} coletas dentro da janela, "
                        f"{len(acc_items)} itens datados guardados nelas.")
    all_results = rss + others
    raw = [c for r in all_results for c in r.items]
    report.sources = [{"name": r.name, "kind": r.kind, "reachable": r.reachable, "items": len(r.items),
                       "newest": r.newest.isoformat() if r.newest else None, "fresh": r.fresh,
                       "attempts": len(r.attempts)} for r in all_results]
    log.info("Coleta: %d itens brutos de %d fontes/buscas (%.0fs)", len(raw), len(all_results), time.time() - t0)

    # 2) dedupe + freshness prefilter on feed/search timestamps
    _name_outlets(raw, sources)
    cands = merge_duplicates(raw)
    fresh = []
    for c in cands:
        if c.feed_published is not None and not window.contains(c.feed_published):
            continue  # feed already says it's older than the window
        fresh.append(c)
    report.counts.update(raw=len(raw), unique=len(cands), fresh_by_feed=len(fresh))

    # 3) triage on headlines
    try:
        tri = editor.triage(fresh, window)
    except editor.EditorError as e:
        log.error("Triagem falhou: %s — seguindo com todos os itens", e)
        report.notes.append(f"Triagem automática falhou ({e}); todos os itens seguiram para a etapa seguinte.")
        tri = {c.id: {"keep": True, "country": c.country_hint or "NONE", "reason": "in_scope"} for c in fresh}
    kept = []
    for c in fresh:
        t = tri.get(c.id, {})
        if t.get("keep", True):
            if t.get("country") not in (None, "NONE"):
                c.triage_country = t["country"]
            kept.append(c)
        else:
            report.excluded.append({"title": c.title, "source": c.source, "reason": t.get("reason")})
    report.counts["triage_kept"] = len(kept)
    log.info("Triagem: %d de %d mantidos", len(kept), len(fresh))

    # 4) resolve Google News links, dedupe again, verify every article at page level
    search.decode_google_news(kept)
    kept = merge_duplicates(kept)
    verified_or_flagged = _verify_all(kept, window, deadline, report)

    # 5) corroborate blocked Brazilian items on FinsidersBrasil / SonhoSeguro (article pages there open fine)
    blocked_br = [c for c in verified_or_flagged
                  if c.status == UNVERIFIED and (c.triage_country or c.country_hint) == "BR"]
    if blocked_br and time.time() < deadline:
        extra = _corroborate(blocked_br, window)
        known = {norm_url(c.url) for c in verified_or_flagged}
        extra = [c for c in extra if norm_url(c.url) not in known]
        verified_or_flagged += [c for c in _verify_all(extra, window, deadline, report) if c.status == VERIFIED]

    usable = [c for c in verified_or_flagged if c.status in (VERIFIED, UNVERIFIED)]
    report.counts.update(
        verified=sum(c.status == VERIFIED for c in usable),
        unverified=sum(c.status == UNVERIFIED for c in usable),
    )

    # 6) write-up, de-duplication across outlets, final QC
    entries = _write_and_qc(usable, window, report)

    # 7) notes, render, deliver
    feed_state = state.load()
    report.notes = coverage_notes(window, rss, others, companies, feed_state, run_day, acc) + report.notes
    state.save(feed_state)
    excl = Counter(e["reason"] for e in report.excluded)
    report.notes.append(
        f"Estatísticas: {report.counts.get('raw', 0)} itens brutos → {report.counts.get('fresh_by_feed', 0)} na janela pelo feed "
        f"→ {report.counts.get('triage_kept', 0)} após triagem → {len(entries)} no clipping "
        f"({sum(not e.unverified for e in entries)} verificados, {sum(e.unverified for e in entries)} NÃO VERIFICADOS). "
        f"Exclusões: " + ", ".join(f"{k}={v}" for k, v in excl.most_common())
    )
    report.notes.append(f"Tempo total de execução: {(time.time() - t0) / 60:.1f} min.")

    subject = f"LatAm Financials Clipping — {window.end.strftime('%d/%m/%Y')}"
    text = render_text(entries, subject, window.describe(), report.notes)
    html_body = render_html(entries, subject, window.describe(), report.notes)

    out_dir = out_dir or ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"clipping-{run_day}"
    base.with_suffix(".txt").write_text(text, encoding="utf-8")
    base.with_suffix(".html").write_text(html_body, encoding="utf-8")
    base.with_suffix(".json").write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str),
                                         encoding="utf-8")
    if not dry_run:
        from . import mailer

        mailer.send(subject, text, html_body)
        log.info("E-mail enviado: %s", subject)
    print(text)
    return base.with_suffix(".txt")


def _verify_all(cands: list[Candidate], window: Window, deadline: float, report: RunReport) -> list[Candidate]:
    out: list[Candidate] = []
    skipped: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=int(os.environ.get("VERIFY_CONCURRENCY", "12"))) as pool:
        futs = {}
        for c in cands:
            if time.time() > deadline:
                skipped.append(c)
                continue
            futs[pool.submit(verify.verify, c, window)] = c
        for f in as_completed(futs):
            c = f.result()
            out.append(c)
            if c.status not in (VERIFIED, UNVERIFIED):
                report.excluded.append({"title": c.title, "source": c.source, "url": c.url,
                                        "reason": f"date:{c.status}", "note": c.verify_note})
    if skipped:
        report.notes.append(f"Prazo atingido: {len(skipped)} itens não chegaram a ser verificados e ficaram de fora: "
                            + "; ".join(c.title[:80] for c in skipped[:10]))
    return out


def _corroborate(blocked: list[Candidate], window: Window) -> list[Candidate]:
    when_days = 4 if window.monday else 2
    out: list[Candidate] = []

    def one(c: Candidate) -> list[Candidate]:
        q = f"(site:finsidersbrasil.com.br OR site:sonhoseguro.com.br) {keywords(c.headline)}"
        r = search.news_search(f"corroboração: {c.headline[:50]}", q, "BR", when_days, "BR", c.company_hint)
        return r.items

    with ThreadPoolExecutor(max_workers=4) as pool:
        for items in pool.map(one, blocked):
            out.extend(items)
    search.decode_google_news(out)
    return [c for c in merge_duplicates(out) if not c.needs_gnews_decode]


def _write_and_qc(usable: list[Candidate], window: Window, report: RunReport) -> list[Entry]:
    if not usable:
        return []
    by_id = {c.id: c for c in usable}
    try:
        written = editor.write_up(usable, window)
    except editor.EditorError as e:
        report.notes.append(f"ATENÇÃO: redação automática falhou ({e}); itens listados apenas com o título original.")
        written = {c.id: {"include": True, "country": c.triage_country or c.country_hint or "OTHER_LATAM",
                          "segment": "other", "takeaway_pt": "(resumo indisponível)", "title_en": c.headline,
                          "paragraph_en": "(summary unavailable — editor step failed)"} for c in usable}

    entries: list[Entry] = []
    for cid, w in written.items():
        c = by_id.get(cid)
        if c is None:
            continue
        if not w.get("include"):
            report.excluded.append({"title": c.headline, "source": c.source, "url": c.url,
                                    "reason": w.get("exclusion_reason"), "duplicate_of": w.get("duplicate_of")})
            continue
        country = w.get("country") or c.triage_country or "OTHER_LATAM"
        if country not in ("BR", "MX", "AR", "CO", "PE", "CL", "UY", "PA", "OTHER_LATAM"):
            country = "OTHER_LATAM"
        entries.append(Entry(
            id=cid, headline=c.headline, source=c.source, published=c.best_date, url=c.url,
            takeaway_pt=w.get("takeaway_pt", ""), title_en=w.get("title_en", ""),
            paragraph_en=w.get("paragraph_en", ""), unverified=c.status != VERIFIED,
            country=country, segment=w.get("segment") or "other",
        ))
    missing = [c for c in usable if c.id not in written]
    if missing:
        report.notes.append(f"{len(missing)} itens não retornaram da etapa de redação: "
                            + "; ".join(c.headline[:80] for c in missing[:10]))

    entries = deterministic_qc(entries, by_id, window, report)

    # Final QC re-scan by the editor over the assembled list
    qc_rows = [{
        "id": e.id, "status": "NAO_VERIFICADO" if e.unverified else "VERIFIED", "headline": e.headline,
        "outlet": e.source, "takeaway_pt": e.takeaway_pt, "title_en": e.title_en, "paragraph_en": e.paragraph_en,
        "source_material": (by_id[e.id].article_text[:4000] if not e.unverified else
                            (by_id[e.id].summary or "") + "\n" + by_id[e.id].article_text[:2000]),
    } for e in entries]
    try:
        qc = editor.final_qc(qc_rows, window)
    except editor.EditorError as e:
        report.notes.append(f"QC final automático falhou ({e}); lista enviada após checagens determinísticas apenas.")
        qc = {}
    final: list[Entry] = []
    for e in entries:
        v = qc.get(e.id)
        if v and v["verdict"] == "remove":
            report.excluded.append({"title": e.headline, "source": e.source, "url": e.url,
                                    "reason": f"qc:{v.get('problem', '')}"})
            continue
        if v and v["verdict"] == "fix":
            e.takeaway_pt = v.get("takeaway_pt") or e.takeaway_pt
            e.title_en = v.get("title_en") or e.title_en
            e.paragraph_en = v.get("paragraph_en") or e.paragraph_en
        final.append(e)
    return final


def deterministic_qc(entries: list[Entry], by_id: dict[str, Candidate], window: Window,
                     report: RunReport) -> list[Entry]:
    """Hard checks that never depend on model judgment."""
    out: list[Entry] = []
    seen_urls: set[str] = set()
    for e in entries:
        c = by_id[e.id]
        problem = None
        if c.status == VERIFIED and c.article_published and not c.article_date_only \
                and not window.contains(c.article_published):
            problem = "data verificada fora da janela"
        elif c.status == UNVERIFIED and not window.contains(c.feed_published) and not (
                c.article_published and window.contains(c.article_published)):
            problem = "item não verificado sem data de feed dentro da janela"
        elif c.status not in (VERIFIED, UNVERIFIED) or c.status == STALE:
            problem = f"status {c.status}"
        elif e.published is None:
            problem = "sem data"
        elif not (e.takeaway_pt.strip() and e.title_en.strip() and e.paragraph_en.strip()):
            problem = "formato incompleto"
        elif norm_url(e.url) in seen_urls:
            problem = "URL duplicada"
        if problem:
            report.excluded.append({"title": e.headline, "source": e.source, "url": e.url, "reason": f"qc:{problem}"})
            continue
        seen_urls.add(norm_url(e.url))
        out.append(e)
    return out
