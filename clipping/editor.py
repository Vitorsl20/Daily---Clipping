"""Editorial steps powered by Claude: triage (scope/crypto/competitor research), write-up (bilingual
format + de-duplication) and the final QC re-scan."""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import llm
from .models import VERIFIED, Candidate
from .timewin import BRT, Window

log = logging.getLogger(__name__)

TRIAGE_EFFORT = os.environ.get("CLAUDE_TRIAGE_EFFORT", "low")
WRITE_EFFORT = os.environ.get("CLAUDE_WRITE_EFFORT", "medium")
RULES = (Path(__file__).resolve().parent.parent / "prompts" / "editorial_rules.md").read_text(encoding="utf-8")
# Lean by default: how much article text goes to the model per item.
ARTICLE_CHARS = int(os.environ.get("ARTICLE_CHARS", "3000"))
QC_CHARS = int(os.environ.get("QC_CHARS", "1800"))
PARALLEL = int(os.environ.get("CLAUDE_PARALLEL", "2"))

COUNTRIES = ["BR", "MX", "AR", "CO", "PE", "CL", "UY", "PA", "OTHER_LATAM"]
SEGMENTS = ["banks", "fintechs", "insurers", "exchanges_capital_markets", "regulation_credit", "other"]
DROP_REASONS = ["out_of_sector", "out_of_region", "crypto", "competitor_research", "political",
                "not_news", "old_news", "duplicate", "other"]


class EditorError(RuntimeError):
    pass


def _call(task: str, payload: str, schema: dict, effort: str, step: str = "write") -> dict:
    try:
        return llm.complete(RULES, f"{task}\n\n<data>\n{payload}\n</data>", schema, step, effort)
    except (llm.LLMError, ValueError) as e:
        raise EditorError(str(e)) from e


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def _fmt_dt(dt) -> str:
    return dt.astimezone(BRT).strftime("%d/%m/%Y %H:%M BRT") if dt else "desconhecida"


# ------------------------------------------------------------------ triage

TRIAGE_SCHEMA = _obj({
    "items": {"type": "array", "items": _obj({
        "id": {"type": "string"},
        "keep": {"type": "boolean"},
        "reason": {"type": "string", "enum": ["in_scope"] + DROP_REASONS},
        "country": {"type": "string", "enum": COUNTRIES + ["NONE"]},
    })},
})

TRIAGE_TASK = """Triage these candidate headlines for today's clipping (window: {window}).
For EVERY id, decide keep=true if the headline plausibly fits the rules (LatAm financial sector, not crypto-focused,
not competitor/sell-side research, not politics unrelated to financial regulation). This is a first pass on headlines
only — when a headline is plausibly in scope but unclear, keep it (the article will be read later). Drop clear misses.
Return one entry per id, with the country of the institution/market."""


def triage(cands: list[Candidate], window: Window, chunk: int = 120) -> dict[str, dict]:
    def run(batch: list[Candidate]) -> list[dict]:
        rows = [{
            "id": c.id, "headline": c.title, "outlet": c.source, "feed_date": _fmt_dt(c.feed_published),
            "snippet": c.summary[:160], "search_hint": c.company_hint or "",
        } for c in batch]
        out = _call(TRIAGE_TASK.format(window=window.describe()),
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), TRIAGE_SCHEMA, TRIAGE_EFFORT,
                    "triage")
        return out["items"]

    batches = [cands[i:i + chunk] for i in range(0, len(cands), chunk)]
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        for items in ex.map(run, batches):
            for it in items:
                results[it["id"]] = it
    # Anything the model silently skipped is kept (inclusive default) and re-judged at write-up.
    for c in cands:
        results.setdefault(c.id, {"id": c.id, "keep": True, "reason": "in_scope", "country": c.country_hint or "NONE"})
    return results


# ------------------------------------------------------------------ write-up

WRITE_SCHEMA = _obj({
    "items": {"type": "array", "items": _obj({
        "id": {"type": "string"},
        "include": {"type": "boolean"},
        "exclusion_reason": {"type": "string", "enum": ["none"] + DROP_REASONS},
        "duplicate_of": {"type": "string"},
        "country": {"type": "string", "enum": COUNTRIES},
        "segment": {"type": "string", "enum": SEGMENTS},
        "takeaway_pt": {"type": "string"},
        "title_en": {"type": "string"},
        "paragraph_en": {"type": "string"},
    })},
})

WRITE_TASK = """Today's window: {window}. Below are the articles that survived date verification.
status=VERIFIED means the article page was opened and its date confirmed; you have its text.
status=NAO_VERIFICADO means the page could not be opened (or carried no date) — write ONLY from the headline/snippet
(and article text if any was provided), short, and invent nothing.

For EVERY id return an entry:
- include=false with exclusion_reason when it fails the rules (sector scope, crypto, competitor research, politics,
  not real news, recycled old news) or when it is a duplicate of another entry (exclusion_reason=duplicate,
  duplicate_of=<kept id>). Otherwise include=true, exclusion_reason=none, duplicate_of="".
- For included items fill country, segment, takeaway_pt, title_en, paragraph_en following the writing rules.
  For excluded items you may leave the text fields empty."""


def _article_block(c: Candidate) -> dict:
    status = "VERIFIED" if c.status == VERIFIED else "NAO_VERIFICADO"
    text = c.article_text[:ARTICLE_CHARS] if c.article_text else ""
    return {
        "id": c.id, "status": status, "headline": c.headline, "outlet": c.source,
        "published": _fmt_dt(c.best_date), "url": c.url, "feed_snippet": c.summary[:400],
        "article_text": text, "country_hint": c.triage_country or c.country_hint or "",
    }


ALREADY_SENT_TASK = """
These stories were ALREADY SENT in this morning's edition (one per line). Any entry below that covers the same
story as one of them (even from another outlet) must be excluded with exclusion_reason=duplicate and
duplicate_of="sent". Entries whose status is VERIFIED and whose url equals a morning item are NOT duplicates:
they are re-verifications and must be included.
<already_sent>
{lines}
</already_sent>"""


def write_up(cands: list[Candidate], window: Window, already_sent: list[str] | None = None,
             chunk: int = 40) -> dict[str, dict]:
    """Single call when possible so duplicates can be detected across the whole list."""
    task = WRITE_TASK.format(window=window.describe())
    if already_sent:
        task += ALREADY_SENT_TASK.format(lines="\n".join(already_sent))

    def run(batch: list[Candidate]) -> list[dict]:
        payload = "\n".join(json.dumps(_article_block(c), ensure_ascii=False) for c in batch)
        return _call(task, payload, WRITE_SCHEMA, WRITE_EFFORT, "write")["items"]

    out: dict[str, dict] = {}
    batches = [cands[i:i + chunk] for i in range(0, len(cands), chunk)] or []
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        for items in ex.map(run, batches):
            for it in items:
                out[it["id"]] = it
    return out


# ------------------------------------------------------------------ final QC re-scan

QC_SCHEMA = _obj({
    "items": {"type": "array", "items": _obj({
        "id": {"type": "string"},
        "verdict": {"type": "string", "enum": ["ok", "remove", "fix"]},
        "problem": {"type": "string"},
        "takeaway_pt": {"type": "string"},
        "title_en": {"type": "string"},
        "paragraph_en": {"type": "string"},
    })},
})

QC_TASK = """FINAL QC PASS. This is the assembled clipping, about to be emailed. Re-scan EVERY item once more against the
full rule set, as an explicit check independent of earlier decisions:
1. Sector scope (LatAm financials only; politics only if about financial regulation / central-bank policy).
2. Crypto de-emphasis.
3. Competitor-research exclusion (no third-party/sell-side analyst views, targets, ratings, forecasts).
4. Duplicates of the same story.
5. Fidelity: the English paragraph and takeaway must contain only facts present in the source material given
   (headline/snippet/article text). NAO_VERIFICADO paragraphs must be short and built only from what was given.
6. Format: takeaway_pt in Portuguese, one line; title_en is a faithful translation; paragraph is neutral, 3–5 tight
   sentences for VERIFIED items.
verdict=ok when fine; verdict=remove (with problem) when it breaks rules 1–4; verdict=fix (with problem and corrected
takeaway_pt/title_en/paragraph_en) when only the text needs correcting. For ok/remove leave the text fields empty."""


def final_qc(entries: list[dict], window: Window) -> dict[str, dict]:
    if not entries:
        return {}
    payload = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    out = _call(QC_TASK + f"\nWindow: {window.describe()}", payload, QC_SCHEMA, WRITE_EFFORT, "qc")
    return {it["id"]: it for it in out["items"]}
