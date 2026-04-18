#!/usr/bin/env python3
"""
Daily digest: pulls upcoming Calendar events + recent Gmail threads,
asks Claude to produce a clean digest (events from emails not yet in
calendar are flagged), and emails the result.

Usage:
    daily_digest.py              # full run: fetch, build, send, persist state
    daily_digest.py --dry-run    # fetch + build + write preview.html, no email

Designed for macOS, runs under launchd at 02:00 local time.
See README.md for install and configuration.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import smtplib
import ssl
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, asdict
from email.mime.text import MIMEText
from pathlib import Path

import anthropic


# ---------------------------------------------------------------------------
# Config — everything comes from environment variables so nothing sensitive
# lives in this file. See install.sh and ~/.config/daily-digest/config.env.
# ---------------------------------------------------------------------------

def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        print(f"ERROR: required environment variable {key} is not set.",
              file=sys.stderr)
        print("Run install.sh or source ~/.config/daily-digest/config.env first.",
              file=sys.stderr)
        sys.exit(2)
    return val


CFG = {
    "gmail_address":     _require_env("DIGEST_GMAIL_ADDRESS"),
    "gmail_app_pw":      _require_env("DIGEST_GMAIL_APP_PW"),
    "recipient":         os.environ.get("DIGEST_RECIPIENT")
                         or _require_env("DIGEST_GMAIL_ADDRESS"),
    "anthropic_key":     _require_env("ANTHROPIC_API_KEY"),
    "anthropic_model":   os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"),
    "calendar_days":     int(os.environ.get("DIGEST_CAL_DAYS", "14")),
    "email_days":        int(os.environ.get("DIGEST_EMAIL_DAYS", "3")),
    "state_dir":         Path(os.environ.get(
                            "DIGEST_STATE_DIR",
                            Path.home() / ".local" / "share" / "daily-digest")),
    "log_file":          Path(os.environ.get(
                            "DIGEST_LOG_FILE",
                            Path.home() / "Library" / "Logs" / "daily-digest.log")),
}

CFG["state_dir"].mkdir(parents=True, exist_ok=True)
CFG["log_file"].parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(CFG["log_file"]), logging.StreamHandler()],
)
log = logging.getLogger("daily-digest")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class CalEvent:
    title: str
    start: str
    end: str
    all_day: bool
    calendar: str
    location: str
    notes: str
    link: str             # calshow: URL that opens Calendar on that day

@dataclass
class MailItem:
    subject: str
    sender: str
    date: str
    message_id: str
    snippet: str
    link_mail_app: str    # message://<id>
    link_gmail_web: str   # https://mail.google.com/mail/u/0/#search/rfc822msgid:<id>


# ---------------------------------------------------------------------------
# Calendar via AppleScript — reads the unified store (iCloud + Google + any
# subscribed calendars) in one pass. No OAuth needed.
# ---------------------------------------------------------------------------

APPLESCRIPT_CAL = r"""
on run argv
    set daysAhead to (item 1 of argv) as integer
    set startDate to current date
    set endDate to startDate + (daysAhead * days)

    set output to ""
    tell application "Calendar"
        repeat with c in calendars
            set calName to name of c
            try
                set evs to (every event of c whose start date ≥ startDate and start date ≤ endDate)
            on error
                set evs to {}
            end try
            repeat with e in evs
                set t to summary of e
                set sd to start date of e
                set ed to end date of e
                try
                    set loc to location of e
                on error
                    set loc to ""
                end try
                try
                    set desc to description of e
                on error
                    set desc to ""
                end try
                try
                    set ad to allday event of e
                on error
                    set ad to false
                end try
                set output to output & t & "|" & (sd as «class isot» as string) ¬
                    & "|" & (ed as «class isot» as string) ¬
                    & "|" & (ad as string) ¬
                    & "|" & calName ¬
                    & "|" & loc ¬
                    & "|" & desc & linefeed
            end repeat
        end repeat
    end tell
    return output
end run
"""


def _calshow_url(iso_start: str) -> str:
    """calshow:<seconds-since-2001-01-01> opens Calendar on that day."""
    try:
        d = dt.datetime.fromisoformat(iso_start.replace("Z", "+00:00"))
    except ValueError:
        return ""
    epoch = dt.datetime(2001, 1, 1, tzinfo=d.tzinfo) if d.tzinfo else dt.datetime(2001, 1, 1)
    return f"calshow:{int((d - epoch).total_seconds())}"


def fetch_calendar(days: int) -> list[CalEvent]:
    log.info("reading calendar (%d days ahead)", days)
    res = subprocess.run(
        ["osascript", "-e", APPLESCRIPT_CAL, str(days)],
        capture_output=True, text=True, timeout=180,
    )
    if res.returncode != 0:
        log.error("osascript failed: %s", res.stderr)
        return []
    out: list[CalEvent] = []
    for line in res.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 7:
            continue
        title, start, end, ad, cal, loc, notes = parts[:7]
        out.append(CalEvent(
            title=title.strip(),
            start=start.strip(),
            end=end.strip(),
            all_day=(ad.strip().lower() == "true"),
            calendar=cal.strip(),
            location=loc.strip(),
            notes=notes.strip()[:500],
            link=_calshow_url(start.strip()),
        ))
    log.info("got %d calendar events", len(out))
    return out


# ---------------------------------------------------------------------------
# Gmail via IMAP
# ---------------------------------------------------------------------------

def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    decoded = email.header.decode_header(raw)
    return "".join(
        (b.decode(enc or "utf-8", errors="replace") if isinstance(b, bytes) else b)
        for b, enc in decoded
    )


def _extract_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""


def fetch_gmail(days: int) -> list[MailItem]:
    log.info("reading gmail (last %d days)", days)
    since = (dt.date.today() - dt.timedelta(days=days)).strftime("%d-%b-%Y")

    out: list[MailItem] = []
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as M:
        M.login(CFG["gmail_address"], CFG["gmail_app_pw"])
        # [Gmail]/All Mail includes archived + sent, useful for "did I get an
        # invite I haven't yet moved into the calendar".
        M.select('"[Gmail]/All Mail"', readonly=True)
        typ, data = M.search(None, f'(SINCE "{since}")')
        if typ != "OK":
            return []
        ids = data[0].split()
        for msg_id in ids[-200:]:   # cap for safety
            typ, raw = M.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            subject = _decode_header(msg.get("Subject"))
            sender = _decode_header(msg.get("From"))
            date_hdr = msg.get("Date", "")
            try:
                iso = email.utils.parsedate_to_datetime(date_hdr).isoformat()
            except (TypeError, ValueError):
                iso = ""
            mid = (msg.get("Message-ID") or "").strip("<>")
            text = _extract_text(msg)[:1500]
            out.append(MailItem(
                subject=subject,
                sender=sender,
                date=iso,
                message_id=mid,
                snippet=" ".join(text.split())[:1000],
                link_mail_app=f"message://%3C{urllib.parse.quote(mid)}%3E" if mid else "",
                link_gmail_web=f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{urllib.parse.quote(mid)}" if mid else "",
            ))
    log.info("got %d emails", len(out))
    return out


# ---------------------------------------------------------------------------
# Claude: produce the digest
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You produce a crisp daily digest for the user's morning.

Input: calendar events for the next N days, recent Gmail threads, and
yesterday's digest (may be empty).

Tasks:
1. List all upcoming calendar events grouped by day (today first).
2. Scan emails for event-like content (invitations, bookings, appointments,
   reservations, flights, deliveries, deadlines, RSVPs). For each one,
   decide if it is already represented in the calendar list. If yes, skip.
   If no, surface it under "Possible events from email not yet in calendar".
3. Keep wording close to yesterday's digest where the facts are unchanged,
   so the user sees stable text day to day. Only change wording when facts
   change or an item is genuinely new.
4. Each line must include its link in markdown-style format: <a href="...">title</a>.
   For calendar items use the calshow: link provided.
   For email items use the message:// link (Apple Mail) as primary and
   the Gmail web link in parentheses as fallback.
5. Be terse. Use short lines. Group by date with a date heading.
6. If nothing new from email, say so explicitly in one line.

Output format: HTML fragment (will be sent as an email body). Use simple
tags: <h2>, <h3>, <ul>, <li>, <a href="...">, <strong>, <em>. No CSS, no
inline styles, no <html>/<body> wrapper. No preamble or sign-off.

Do not invent events. If an email is ambiguous, flag it with "(verify)"."""


def build_digest(cal_events: list[CalEvent],
                 emails: list[MailItem],
                 yesterday_html: str) -> str:
    client = anthropic.Anthropic(api_key=CFG["anthropic_key"])

    payload = {
        "today": dt.date.today().isoformat(),
        "calendar_window_days": CFG["calendar_days"],
        "calendar": [asdict(e) for e in cal_events],
        "emails":   [asdict(m) for m in emails],
        "yesterday_digest_html": yesterday_html,
    }

    msg = client.messages.create(
        model=CFG["anthropic_model"],
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                "Here is today's input. Produce the digest as specified.\n\n"
                f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
            ),
        }],
    )
    return "".join(
        block.text for block in msg.content if getattr(block, "type", "") == "text"
    ).strip()


# ---------------------------------------------------------------------------
# State + send
# ---------------------------------------------------------------------------

def _wrap_html(fragment: str) -> str:
    """Wrap Claude's HTML fragment in a minimal document so browsers and
    mail clients render UTF-8 correctly. Without <meta charset> browsers
    fall back to Latin-1 and em-dashes render as â€"."""
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<title>Daily digest</title>'
        '</head><body>\n'
        f'{fragment}\n'
        '</body></html>\n'
    )


def load_yesterday() -> str:
    f = CFG["state_dir"] / "yesterday.html"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def save_today(html: str) -> None:
    # yesterday.html is a fragment so tomorrow's run can feed it straight
    # back to Claude; the dated archive is wrapped for human browsing.
    (CFG["state_dir"] / "yesterday.html").write_text(html, encoding="utf-8")
    (CFG["state_dir"] / f"digest-{dt.date.today().isoformat()}.html").write_text(
        _wrap_html(html), encoding="utf-8")


def send_email(html_body: str) -> None:
    today = dt.date.today().strftime("%A %-d %B %Y")
    msg = MIMEText(_wrap_html(html_body), "html", "utf-8")
    msg["Subject"] = f"Daily digest — {today}"
    msg["From"] = CFG["gmail_address"]
    msg["To"] = CFG["recipient"]

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(CFG["gmail_address"], CFG["gmail_app_pw"])
        s.send_message(msg)
    log.info("sent digest to %s", CFG["recipient"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="build the digest and save it, but don't send email")
    args = ap.parse_args()

    try:
        cal = fetch_calendar(CFG["calendar_days"])
        mail = fetch_gmail(CFG["email_days"])
        yesterday = load_yesterday()
        html = build_digest(cal, mail, yesterday)
        if not html:
            log.error("empty digest from Claude, aborting")
            return 1

        if args.dry_run:
            preview = CFG["state_dir"] / "preview.html"
            preview.write_text(_wrap_html(html), encoding="utf-8")
            log.info("dry run — preview at %s", preview)
            print(f"\nPreview written: {preview}")
            print(f"Open it:        open {preview}")
            return 0

        save_today(html)
        send_email(html)
        return 0
    except Exception:
        log.exception("digest run failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
