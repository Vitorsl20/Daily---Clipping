"""Read back the morning edition from Gmail (IMAP, same App Password used to send).

The 6h complement needs to know exactly what went out at 05:17 — which items, and which were
NÃO VERIFICADO — without any extra credential. It finds the email by subject and parses the plain-text body,
whose line 1 format is fixed by render.Entry.line1()."""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from email.header import decode_header, make_header

from .render import FLAG
from .timewin import BRT, MONTHS_EN

log = logging.getLogger(__name__)

_LINE1 = re.compile(
    r'^"(?P<headline>.+)" \((?P<source>[^()]+), (?P<date>\d{2}/\d{2}/\d{4})\) — (?P<url>https?://\S+) — (?P<rest>.*)$'
)


@dataclass
class SentItem:
    headline: str
    source: str
    published: date
    url: str
    takeaway: str
    unverified: bool


def parse_edition(text: str) -> list[SentItem]:
    items = []
    for line in text.splitlines():
        m = _LINE1.match(line.strip())
        if not m:
            continue
        rest = m["rest"]
        unverified = rest.endswith(FLAG)
        takeaway = rest[: -len(FLAG)] if unverified else rest
        d, mo, y = map(int, m["date"].split("/"))
        items.append(SentItem(m["headline"], m["source"], date(y, mo, d), m["url"], takeaway.strip(), unverified))
    return items


def _all_mail_box(imap: imaplib.IMAP4_SSL) -> str:
    """Gmail's 'All Mail' has a localized name ("Todos os e-mails"); find it by its \\All flag."""
    typ, boxes = imap.list()
    for raw in boxes or []:
        line = raw.decode("utf-8", "replace")
        if "\\All" in line:
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                return m.group(1)
    return "INBOX"


def _plain_body(msg: email.message.Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def find_edition(subject: str, day: date) -> tuple[list[SentItem], datetime | None] | None:
    """Return (items, sent_at) of the email with exactly this subject sent on `day`, or None if not found."""
    user, password = os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"]
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
    try:
        imap.login(user, password)
        imap.select(f'"{_all_mail_box(imap)}"', readonly=True)
        since = f"{day.day:02d}-{MONTHS_EN[day.month - 1]}-{day.year}"
        typ, data = imap.search(None, "SINCE", since, "SUBJECT", '"Clipping"')
        ids = (data[0] or b"").split() if typ == "OK" else []
        for mid in reversed(ids):
            typ, msg_data = imap.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subj = str(make_header(decode_header(msg.get("Subject", "")))).strip()
            if subj != subject:
                continue
            sent_at = None
            try:
                sent_at = email.utils.parsedate_to_datetime(msg.get("Date")).astimezone(BRT)
            except (TypeError, ValueError):
                pass
            return parse_edition(_plain_body(msg)), sent_at
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass
