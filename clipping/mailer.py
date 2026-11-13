"""Send the clipping through Gmail SMTP (use a Google App Password, not the account password)."""
from __future__ import annotations

import os
import smtplib
import time
from email.message import EmailMessage


def send(subject: str, text: str, html_body: str | None = None) -> None:
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("CLIPPING_TO") or sender

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    last: Exception | None = None
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
                smtp.login(sender, password)
                smtp.send_message(msg)
            return
        except (smtplib.SMTPException, OSError) as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Falha ao enviar e-mail: {last}")
