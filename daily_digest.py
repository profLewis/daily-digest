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
import fcntl
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
    "keep_days":         int(os.environ.get("DIGEST_KEEP_DAYS", "1")),
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
    color: str            # calendar colour as #rrggbb

@dataclass
class MailItem:
    subject: str
    sender: str
    date: str
    message_id: str
    snippet: str
    link_mail_app: str    # message:<id> — Apple Mail, if Gmail is set up there
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
            -- Calendar colour: {r,g,b} 0-65535 per channel. Not all calendar
            -- kinds expose it; fall back to grey.
            try
                set cc to color of c
                set rC to ((item 1 of cc) div 256)
                set gC to ((item 2 of cc) div 256)
                set bC to ((item 3 of cc) div 256)
                set colStr to (rC as string) & "," & (gC as string) & "," & (bC as string)
            on error
                set colStr to "128,128,128"
            end try
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
                    & "|" & desc ¬
                    & "|" & colStr & linefeed
            end repeat
        end repeat
    end tell
    return output
end run
"""


def _rgb_to_hex(triplet: str) -> str:
    """Convert 'r,g,b' (0-255 each) to '#rrggbb'. Returns #808080 on junk."""
    try:
        r, g, b = (max(0, min(255, int(v))) for v in triplet.split(","))
        return f"#{r:02x}{g:02x}{b:02x}"
    except (ValueError, AttributeError):
        return "#808080"


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
        if len(parts) < 8:
            continue
        title, start, end, ad, cal, loc, notes, color = parts[:8]
        out.append(CalEvent(
            title=title.strip(),
            start=start.strip(),
            end=end.strip(),
            all_day=(ad.strip().lower() == "true"),
            calendar=cal.strip(),
            location=loc.strip(),
            notes=notes.strip()[:500],
            link=_calshow_url(start.strip()),
            color=_rgb_to_hex(color.strip()),
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
    log.info("gmail: IMAP login as %s (last %d days)", CFG["gmail_address"], days)
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
        matched = ids[-200:]
        log.info("gmail: %d messages since %s, fetching last %d",
                 len(ids), since, len(matched))
        for msg_id in matched:
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
                link_mail_app=f"message:%3C{urllib.parse.quote(mid)}%3E" if mid else "",
                link_gmail_web=f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{urllib.parse.quote(mid)}" if mid else "",
            ))
    log.info("gmail: parsed %d messages", len(out))
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
4. Each calendar event line MUST start with a coloured marker using the
   calendar's own colour (from the `color` field, a #rrggbb hex). Use
   exactly this form: <span style="color:#RRGGBB">●</span>
   Then the time, the event title wrapped in <a href="CALSHOW_LINK">…</a>,
   and the calendar name in small italics at the end: <em>(Calendar name)</em>.
5. Each email item line must link the subject. Use the Gmail web link as
   the primary <a href="...">…</a>; append the Apple Mail link in small
   parentheses as "(open in Mail)" — it only works if Gmail is configured
   in Apple Mail, so it is a fallback not the default.
6. Be terse. Short lines. Group by date with a date heading.
7. If nothing new from email, say so explicitly in one line.

Output format: HTML fragment (will be sent as an email body and also
written to a local .html file). Use these tags: <h2>, <h3>, <ul>, <li>,
<a href="...">, <strong>, <em>, <span style="color:#RRGGBB">. No other
inline styles. No <html>/<body> wrapper (one is added around your output
later). No preamble or sign-off.

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

    log.info("anthropic: calling %s (1 messages.create, max_tokens=4000)",
             CFG["anthropic_model"])
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

    usage = getattr(msg, "usage", None)
    if usage is not None:
        log.info(
            "anthropic: usage model=%s input_tokens=%d output_tokens=%d "
            "cache_read=%d cache_creation=%d",
            CFG["anthropic_model"],
            getattr(usage, "input_tokens", 0),
            getattr(usage, "output_tokens", 0),
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
    else:
        log.warning("anthropic: no usage returned on response")

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


def trash_old_digests(keep_days: int) -> None:
    """Move prior daily-digest emails (sent by us to ourselves) older than
    `keep_days` days from today into Gmail Trash. keep_days=1 means keep
    only today's; keep_days<1 disables cleanup entirely.

    Only touches messages whose Subject matches our fixed pattern AND whose
    From is our own address — nothing else in the mailbox is at risk.
    Gmail empties Trash automatically after 30 days."""
    if keep_days < 1:
        log.info("digest cleanup disabled (DIGEST_KEEP_DAYS=%d)", keep_days)
        return

    cutoff = dt.date.today() - dt.timedelta(days=keep_days - 1)
    before = cutoff.strftime("%d-%b-%Y")
    addr = CFG["gmail_address"]

    log.info("trashing digests from %s before %s", addr, before)
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as M:
        M.login(addr, CFG["gmail_app_pw"])
        # All Mail is writable and contains sent items as well as received,
        # so we find our own outgoing digests there.
        M.select('"[Gmail]/All Mail"')
        typ, data = M.uid(
            "SEARCH", None,
            "FROM", f'"{addr}"',
            "SUBJECT", '"Daily digest"',
            "BEFORE", f'"{before}"',
        )
        if typ != "OK" or not data or not data[0]:
            log.info("no old digests to trash")
            return
        uids = data[0].split()
        if not uids:
            log.info("no old digests to trash")
            return

        uid_str = b",".join(uids).decode()
        try:
            M.uid("MOVE", uid_str, '"[Gmail]/Trash"')
        except imaplib.IMAP4.error as e:
            log.warning("IMAP MOVE failed (%s); falling back to COPY+DELETE", e)
            M.uid("COPY", uid_str, '"[Gmail]/Trash"')
            M.uid("STORE", uid_str, "+FLAGS", "\\Deleted")
            M.expunge()
        log.info("trashed %d old digest(s)", len(uids))


def send_email(html_body: str) -> None:
    today = dt.date.today().strftime("%A %-d %B %Y")
    msg = MIMEText(_wrap_html(html_body), "html", "utf-8")
    msg["Subject"] = f"Daily digest — {today}"
    msg["From"] = CFG["gmail_address"]
    msg["To"] = CFG["recipient"]

    log.info("smtp: sending digest to %s (%d bytes)",
             CFG["recipient"], len(msg.as_bytes()))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(CFG["gmail_address"], CFG["gmail_app_pw"])
        s.send_message(msg)
    log.info("smtp: sent")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _acquire_single_instance_lock():
    """Stop two copies of daily-digest running at once. Gmail throttles to
    ~15 concurrent IMAP connections per account, and overlapping runs can
    also corrupt yesterday.html. Returns the held file handle — keep it
    alive for the duration of the run; Python closing it releases the lock.
    Returns None if another instance is already running."""
    lockfile = CFG["state_dir"] / "daily-digest.lock"
    try:
        lf = open(lockfile, "w")
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    lf.write(f"{os.getpid()}\n")
    lf.flush()
    return lf


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="build the digest and save it, but don't send email")
    args = ap.parse_args()

    lock = _acquire_single_instance_lock()
    if lock is None:
        log.error("another daily-digest instance is already running; exiting")
        return 4

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

        # Clean up old digests before sending today's, so tomorrow's
        # search window won't include today's freshly-sent copy.
        try:
            trash_old_digests(CFG["keep_days"])
        except Exception:
            log.exception("digest cleanup failed (non-fatal, continuing)")

        save_today(html)
        send_email(html)
        return 0
    except Exception:
        log.exception("digest run failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
