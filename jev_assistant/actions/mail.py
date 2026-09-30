"""Reads the Gmail inbox over IMAP, so Jev can judge which recent emails matter without a browser.

Needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env: an app password (Google Account → Security →
2-Step Verification → App passwords), not your Google password. Read-only: the inbox is opened
with EXAMINE and bodies are fetched with PEEK, so nothing is marked as read.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import html
import imaplib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime

HOST = "imap.gmail.com"
MAX_MAILS = 30           # newest first; Jev judges them all in one request
PEEK_BYTES = 16384       # headers + the start of the text is enough to judge a mail
PREVIEW_CHARS = 400


@dataclass
class Mail:
    sender: str
    subject: str
    received: datetime
    preview: str
    unread: bool
    gmail_important: bool   # Gmail's own importance marker, one more piece of evidence for Jev

    def line(self) -> str:
        now = datetime.now().astimezone()
        when = self.received.strftime("%I:%M %p").lstrip("0") if self.received.date() == now.date() \
            else self.received.strftime("%d %b, %I:%M %p")
        return f"{self.sender} — {self.subject or '(no subject)'} ({when})"


def address() -> str:
    return os.environ.get("GMAIL_ADDRESS", "").strip()


def configured() -> bool:
    return bool(address() and os.environ.get("GMAIL_APP_PASSWORD", "").strip())


async def recent(hours: float, sender: str = "") -> list[Mail]:
    """Inbox mail from the last `hours`, optionally from one sender, newest first."""
    return await asyncio.to_thread(_recent, hours, sender)


def _recent(hours: float, sender: str) -> list[Mail]:
    query = f"after:{int(time.time() - hours * 3600)}"
    if sender := re.sub(r'["\\]', "", sender).strip():
        query += f' from:"{sender}"' if " " in sender else f" from:{sender}"
    with imaplib.IMAP4_SSL(HOST, timeout=15) as imap:
        imap.login(address(), os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        imap.select("INBOX", readonly=True)
        # X-GM-RAW: Gmail's own search syntax, precise to the second with after:<epoch>
        status, data = imap.uid("SEARCH", "X-GM-RAW", '"' + query.replace('"', '\\"') + '"')
        uids = data[0].split()[-MAX_MAILS:] if status == "OK" and data and data[0] else []
        if not uids:
            return []
        status, data = imap.uid("FETCH", b",".join(uids).decode(),
                                f"(FLAGS X-GM-LABELS BODY.PEEK[]<0.{PEEK_BYTES}>)")
    mails = [_parse(meta, raw) for meta, raw in (d for d in data if isinstance(d, tuple))]
    return sorted((m for m in mails if m), key=lambda m: m.received, reverse=True)


def _parse(meta: bytes, raw: bytes) -> Mail | None:
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        name, addr = parseaddr(str(msg.get("From", "")))
        received = parsedate_to_datetime(str(msg["Date"])).astimezone() if msg["Date"] else datetime.now().astimezone()
    except Exception:  # a malformed header shouldn't hide the rest of the inbox
        return None
    return Mail(sender=name or addr, subject=" ".join(str(msg.get("Subject", "")).split()), received=received,
                preview=_preview(msg), unread=b"\\Seen" not in meta, gmail_important=b"\\Important" in meta)


def _preview(msg) -> str:
    try:  # the body was cut at PEEK_BYTES, so decoding may fail part-way: best effort
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
    except Exception:
        return ""
    if body.get_content_subtype() == "html":
        text = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return " ".join(text.split())[:PREVIEW_CHARS]


if __name__ == "__main__":  # python -m jev_assistant.actions.mail : lists the last day's inbox
    from dotenv import load_dotenv

    from ..config import ROOT
    load_dotenv(ROOT / ".env")
    for m in asyncio.run(recent(24)):
        print(("* " if m.gmail_important else "  ") + m.line())
