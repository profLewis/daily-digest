#!/usr/bin/env python3
"""
Daily digest: pulls upcoming Calendar events + recent Gmail threads,
asks a local Ollama model to produce a clean digest (events from emails
not yet in calendar are flagged), and emails the result.

Usage:
    daily_digest.py            # full run: fetch, build, send, persist state
    daily_digest.py --dry-run  # fetch + build + write preview.html, no email

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
import platform
import signal
import smtplib
import time
import html as html_mod
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from email.mime.text import MIMEText
from pathlib import Path

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
    "gmail_address": _require_env("DIGEST_GMAIL_ADDRESS"),
    "gmail_app_pw": _require_env("DIGEST_GMAIL_APP_PW"),
    "recipient": os.environ.get("DIGEST_RECIPIENT")
                 or _require_env("DIGEST_GMAIL_ADDRESS"),
    # Which backend handles the digest generation. "ollama" (default) keeps
    # everything local; "openai_compatible" sends the prompt to a hosted
    # endpoint that speaks OpenAI's /v1/chat/completions schema (DeepSeek,
    # Moonshot/Kimi, Google Gemini's OpenAI-compat endpoint, Alibaba's
    # DashScope, etc.). Picking openai_compatible reverses the data-locality
    # guarantee the local Ollama path provides — every email subject,
    # calendar event and iMessage you feed in leaves your machine.
    "backend": os.environ.get("DIGEST_BACKEND", "ollama").lower(),
    "ollama_url": os.environ.get("OLLAMA_URL", "http://localhost:11434"),
    "ollama_model": os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
    "ollama_timeout": int(os.environ.get("OLLAMA_TIMEOUT", "600")),
    "openai_base_url": os.environ.get("OPENAI_BASE_URL", ""),
    "openai_model": os.environ.get("OPENAI_MODEL", ""),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "openai_timeout": int(os.environ.get("OPENAI_TIMEOUT", "300")),
    "calendar_days": int(os.environ.get("DIGEST_CAL_DAYS", "14")),
    # Calendar.app via AppleScript can be slow when many calendars are
    # synced (iCloud/Google/subscribed). Real-world timings: ~70s on a
    # quiet morning, 2-3 minutes when Calendar is busy syncing. Default
    # 600s gives plenty of headroom; bump higher with DIGEST_CAL_TIMEOUT
    # if you hit it.
    "calendar_timeout": int(os.environ.get("DIGEST_CAL_TIMEOUT", "600")),
    # Mail.app AppleScript walks every account's INBOX. Same story —
    # large mailboxes can take minutes.
    "mail_app_timeout": int(os.environ.get("DIGEST_MAIL_APP_TIMEOUT", "600")),
    "email_days": int(os.environ.get("DIGEST_EMAIL_DAYS", "3")),
    "keep_days": int(os.environ.get("DIGEST_KEEP_DAYS", "1")),
    "use_mail_app": os.environ.get("DIGEST_USE_MAIL_APP", "false").lower() == "true",
    "use_imessage": os.environ.get("DIGEST_USE_IMESSAGE", "false").lower() == "true",
    "imessage_days": int(os.environ.get("DIGEST_IMESSAGE_DAYS", "3")),
    "state_dir": Path(os.environ.get(
        "DIGEST_STATE_DIR",
        Path.home() / ".local" / "share" / "daily-digest")),
    "log_file": Path(os.environ.get(
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
    link: str          # calshow: URL that opens Calendar on that day
    color: str         # calendar colour as #rrggbb
    title_html: str = ""  # pre-rendered <a href=calshow:…>Title</a> for the model

@dataclass
class MailItem:
    subject: str
    sender: str
    date: str
    message_id: str
    snippet: str
    link_mail_app: str  # message:<id> — Apple Mail, if mailbox is set up there
    link_gmail_web: str # https://mail.google.com/mail/?authuser=…#search/rfc822msgid:…
    source: str = "gmail_imap"  # "gmail_imap" or "mail_app"
    subject_html: str = ""      # pre-rendered <a href=…>Subject</a> for the model

@dataclass
class ChatMessage:
    """iMessage or SMS pulled from Messages.app local store."""
    platform: str       # "imessage"
    sender: str         # phone number, email, or "me"
    date: str           # ISO 8601
    text: str           # body, truncated
    is_from_me: bool


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


def _clean_message_id(raw: str | None) -> str:
    """Strip whitespace, angle brackets, and surrounding quotes. Message-IDs
    arrive inconsistently from AppleScript vs IMAP — normalise once."""
    if not raw:
        return ""
    s = raw.strip().strip("<>").strip().strip('"').strip("'")
    return s


def _mailto_apple_link(mid: str) -> str:
    """`message:<Message-ID>` with the angle brackets URL-encoded and the
    id itself percent-encoded (safe=empty so '@' '+' '=' all get escaped).
    Empty string if mid is blank."""
    if not mid:
        return ""
    return f"message:%3C{urllib.parse.quote(mid, safe='')}%3E"


def _gmail_web_link(mid: str, account: str) -> str:
    """Open a specific Gmail message by Message-ID, regardless of which
    Google account the browser's session currently considers `u/0`.
    `authuser=<address>` forces the right account. Empty if mid is blank.

    Note: the colon after `rfc822msgid` MUST be a literal `:`, not `%3A`.
    Gmail's hash router (both desktop and the Gmail iOS app) does not
    percent-decode the fragment before matching, so the encoded form
    silently lands on the inbox instead of the message."""
    if not mid:
        return ""
    encoded_mid = urllib.parse.quote(mid, safe="")
    encoded_account = urllib.parse.quote(account, safe="")
    return (
        "https://mail.google.com/mail/"
        f"?authuser={encoded_account}"
        f"#search/rfc822msgid:{encoded_mid}"
    )


def _gcal_day_url(iso_start: str) -> str:
    """Google Calendar day-view URL for the date in `iso_start`. Works in
    mobile Safari and is intercepted by the Google Calendar iOS app if
    installed. Used as the primary calendar link because `calshow:` is
    macOS-only and does nothing on iPhone/iPad/web. Returns "" if the
    date can't be parsed."""
    try:
        d = dt.datetime.fromisoformat(iso_start.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"https://calendar.google.com/calendar/u/0/r/day/{d.year}/{d.month}/{d.day}"


def _build_subject_html(item: "MailItem", gmail_address: str) -> str:
    """Render the full anchor HTML for an email subject. For Gmail-IMAP
    sources the primary link is the Gmail web URL (works in any browser
    without Apple Mail); for Mail.app-sourced messages (or when we have
    no Gmail web link) the primary is the message: URL. If we have both,
    the alternate is appended as a small "(in Mail)" link."""
    subject_text = item.subject or "(no subject)"
    safe_subject = html_mod.escape(subject_text)
    gmail_url = _gmail_web_link(item.message_id, gmail_address) if item.message_id else ""
    apple_url = _mailto_apple_link(item.message_id)
    # Prefer the Gmail web link when present (most robust); else Apple Mail.
    if gmail_url:
        primary = gmail_url
        alt = apple_url if apple_url else ""
    elif apple_url:
        primary = apple_url
        alt = ""
    else:
        return f"<em>{safe_subject}</em>"  # no link at all
    out = f'<a href="{html_mod.escape(primary, quote=True)}">{safe_subject}</a>'
    if alt:
        # Labelled "Mac Mail" rather than just "Mail" so iPhone readers
        # know not to tap — the message: scheme is macOS-only in practice.
        out += (f' <a href="{html_mod.escape(alt, quote=True)}" '
                f'style="font-size:0.85em;color:#777">(in Mac Mail)</a>')
    return out


def _build_title_html(title: str, gcal_url: str, calshow: str) -> str:
    """Render the anchor HTML for a calendar event title.

    Primary link is the Google Calendar day view (universal: works on
    iPhone, iPad, and any browser). The macOS-only `calshow:` URL is
    appended as a small "(in Mac Cal)" alternate so desktop users still
    get one-click access to the native Calendar app."""
    safe = html_mod.escape(title or "(no title)")
    if gcal_url:
        out = f'<a href="{html_mod.escape(gcal_url, quote=True)}">{safe}</a>'
        if calshow:
            out += (f' <a href="{html_mod.escape(calshow, quote=True)}" '
                    f'style="font-size:0.85em;color:#777">(in Mac Cal)</a>')
        return out
    if calshow:
        return f'<a href="{html_mod.escape(calshow, quote=True)}">{safe}</a>'
    return f"<strong>{safe}</strong>"


def fetch_calendar(days: int) -> list[CalEvent]:
    log.info("reading calendar (%d days ahead, timeout=%ds)",
             days, CFG["calendar_timeout"])
    t0 = dt.datetime.now()
    try:
        res = subprocess.run(
            ["osascript", "-e", APPLESCRIPT_CAL, str(days)],
            capture_output=True, text=True, timeout=CFG["calendar_timeout"],
        )
    except subprocess.TimeoutExpired:
        log.error(
            "calendar AppleScript timed out after %ds. Calendar.app is slow "
            "or unresponsive. Try: open Calendar.app and let it finish syncing, "
            "or raise DIGEST_CAL_TIMEOUT in ~/.config/daily-digest/config.env.",
            CFG["calendar_timeout"],
        )
        return []
    elapsed = (dt.datetime.now() - t0).total_seconds()
    log.info("calendar: AppleScript returned in %.1fs", elapsed)
    if res.returncode != 0:
        log.error("osascript failed: %s", res.stderr)
        return []
    out: list[CalEvent] = []
    for line in res.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 8:
            continue
        title, start, end, ad, cal, loc, notes, color = parts[:8]
        calshow = _calshow_url(start.strip())
        gcal = _gcal_day_url(start.strip())
        # `link` keeps the universal URL (Google Calendar day view) so it's
        # tappable on iPhone too. The Mac-only calshow alternate is only
        # exposed through title_html.
        out.append(CalEvent(
            title=title.strip(),
            start=start.strip(),
            end=end.strip(),
            all_day=(ad.strip().lower() == "true"),
            calendar=cal.strip(),
            location=loc.strip(),
            notes=notes.strip()[:500],
            link=gcal or calshow,
            color=_rgb_to_hex(color.strip()),
            title_html=_build_title_html(title.strip(), gcal, calshow),
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
            mid = _clean_message_id(msg.get("Message-ID"))
            text = _extract_text(msg)[:1500]
            item = MailItem(
                subject=subject,
                sender=sender,
                date=iso,
                message_id=mid,
                snippet=" ".join(text.split())[:1000],
                link_mail_app=_mailto_apple_link(mid),
                link_gmail_web=_gmail_web_link(mid, CFG["gmail_address"]),
                source="gmail_imap",
            )
            item.subject_html = _build_subject_html(item, CFG["gmail_address"])
            out.append(item)
    log.info("gmail: parsed %d messages", len(out))
    return out


# ---------------------------------------------------------------------------
# Mail.app via AppleScript — reads every account Mail is logged into.
# Gated by DIGEST_USE_MAIL_APP=true. Requires Automation permission for
# Mail (granted by first-run dialog, or in System Settings → Privacy &
# Security → Automation).
# ---------------------------------------------------------------------------

APPLESCRIPT_MAIL = r"""
on run argv
    set daysBack to (item 1 of argv) as integer
    set sinceDate to (current date) - (daysBack * days)
    set SEP to "|§|"
    set output to ""
    tell application "Mail"
        repeat with a in (every account)
            set acctName to name of a
            try
                set inboxes to (every mailbox of a whose name is "INBOX")
                if (count of inboxes) is 0 then
                    set inboxes to (every mailbox of a whose name is "Inbox")
                end if
                repeat with mb in inboxes
                    try
                        set msgs to (messages of mb whose date received ≥ sinceDate)
                    on error
                        set msgs to {}
                    end try
                    repeat with m in msgs
                        try
                            set subj to subject of m
                        on error
                            set subj to ""
                        end try
                        try
                            set sndr to sender of m
                        on error
                            set sndr to ""
                        end try
                        try
                            set dStr to (date received of m as «class isot» as string)
                        on error
                            set dStr to ""
                        end try
                        try
                            set mid to message id of m
                        on error
                            set mid to ""
                        end try
                        try
                            set snip to content of m
                            if (count of characters of snip) > 1200 then
                                set snip to text 1 thru 1200 of snip
                            end if
                        on error
                            set snip to ""
                        end try
                        set subj to my clean(subj, SEP)
                        set sndr to my clean(sndr, SEP)
                        set snip to my clean(snip, SEP)
                        set output to output & acctName & SEP & subj & SEP & sndr & SEP & dStr & SEP & mid & SEP & snip & linefeed
                    end repeat
                end repeat
            end try
        end repeat
    end tell
    return output
end run

on clean(s, sep)
    set tids to AppleScript's text item delimiters
    try
        set AppleScript's text item delimiters to {return, linefeed, tab}
        set parts to text items of s
        set AppleScript's text item delimiters to " "
        set s to parts as text
        set AppleScript's text item delimiters to sep
        set parts to text items of s
        set AppleScript's text item delimiters to " "
        set s to parts as text
    end try
    set AppleScript's text item delimiters to tids
    return s
end clean
"""

def fetch_mail_app(days: int) -> list[MailItem]:
    log.info("mail.app: AppleScript read of inbox messages (last %d days, timeout=%ds)",
             days, CFG["mail_app_timeout"])
    t0 = dt.datetime.now()
    try:
        res = subprocess.run(
            ["osascript", "-e", APPLESCRIPT_MAIL, str(days)],
            capture_output=True, text=True, timeout=CFG["mail_app_timeout"],
        )
    except subprocess.TimeoutExpired:
        log.error(
            "mail.app AppleScript timed out after %ds. Either lower "
            "DIGEST_EMAIL_DAYS or raise DIGEST_MAIL_APP_TIMEOUT in "
            "~/.config/daily-digest/config.env.",
            CFG["mail_app_timeout"],
        )
        return []
    elapsed = (dt.datetime.now() - t0).total_seconds()
    log.info("mail.app: AppleScript returned in %.1fs", elapsed)
    if res.returncode != 0:
        log.error("mail.app osascript failed: %s", res.stderr.strip())
        return []
    out: list[MailItem] = []
    for line in res.stdout.splitlines():
        parts = line.split("|§|")
        if len(parts) < 6:
            continue
        _acct, subject, sender, date_iso, mid, snip = parts[:6]
        mid = _clean_message_id(mid)
        item = MailItem(
            subject=subject.strip(),
            sender=sender.strip(),
            date=date_iso.strip(),
            message_id=mid,
            snippet=" ".join(snip.split())[:1000],
            link_mail_app=_mailto_apple_link(mid),
            link_gmail_web="",
            source="mail_app",
        )
        item.subject_html = _build_subject_html(item, CFG["gmail_address"])
        out.append(item)
    log.info("mail.app: parsed %d messages across all accounts", len(out))
    return out


def merge_mail(primary: list[MailItem],
               secondary: list[MailItem]) -> list[MailItem]:
    """Deduplicate by Message-ID, preferring `primary` (carries Gmail web
    links). Messages without a Message-ID are all kept."""
    seen_ids = {m.message_id for m in primary if m.message_id}
    merged = list(primary)
    for m in secondary:
        if m.message_id and m.message_id in seen_ids:
            continue
        merged.append(m)
        if m.message_id:
            seen_ids.add(m.message_id)
    return merged


# ---------------------------------------------------------------------------
# iMessage / SMS via ~/Library/Messages/chat.db
# Gated by DIGEST_USE_IMESSAGE=true. Opt-in because it reads personal
# chat content. Requires Full Disk Access for whoever runs the script
# (/bin/bash under launchd; the terminal emulator when run manually).
# ---------------------------------------------------------------------------

_APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)

def fetch_imessages(days: int) -> list[ChatMessage]:
    db = Path.home() / "Library" / "Messages" / "chat.db"
    if not db.exists():
        log.warning("imessage: %s not found, skipping", db)
        return []
    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    since_ns = int((since_dt - _APPLE_EPOCH).total_seconds() * 1_000_000_000)
    log.info("imessage: reading chat.db (last %d days)", days)
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        log.error("imessage: cannot open chat.db (%s). Ensure Full Disk "
                  "Access is granted to whoever runs this script.", exc)
        return []
    out: list[ChatMessage] = []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT handle.id, message.date, message.text, message.is_from_me
            FROM message
            LEFT JOIN handle ON message.handle_id = handle.ROWID
            WHERE message.date >= ?
              AND message.text IS NOT NULL
              AND length(message.text) > 0
            ORDER BY message.date ASC
            """,
            (since_ns,),
        )
        for handle_id, mdate, text, is_from_me in cur.fetchall():
            try:
                sent = _APPLE_EPOCH + dt.timedelta(seconds=mdate / 1_000_000_000)
                iso = sent.isoformat()
            except (TypeError, OverflowError, ValueError):
                iso = ""
            out.append(ChatMessage(
                platform="imessage",
                sender=(handle_id or "") if not is_from_me else "me",
                date=iso,
                text=(text or "")[:1000],
                is_from_me=bool(is_from_me),
            ))
    finally:
        con.close()
    log.info("imessage: fetched %d messages", len(out))
    return out


# ---------------------------------------------------------------------------
# Local model (Ollama): produce the digest
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You produce a crisp daily digest for the user's morning.
Input: calendar events for the next N days, recent emails, recent chat
messages (iMessage/SMS, may be empty), and yesterday's digest.

Tasks:
1. List all upcoming calendar events grouped by day (today first).
2. Scan emails AND chat messages for event-like content (invitations,
   bookings, appointments, reservations, flights, deliveries, deadlines,
   RSVPs). For each one, decide if it is already represented in the
   calendar list. If yes, skip. If no, surface it under "Possible events
   from email/messages not yet in calendar". Note the source next to each
   surfaced item (e.g. "(email from X)" or "(iMessage from Y)").
3. Keep wording close to yesterday's digest where the facts are unchanged,
   so the user sees stable text day to day. Only change wording when facts
   change or an item is genuinely new.
4. Each calendar event line MUST start with a coloured marker using the
   calendar's own colour (from the `color` field, a #rrggbb hex). Use
   exactly this form: <span style="color:#RRGGBB">●</span>
   Then the time, then the pre-rendered `title_html` VERBATIM (it already
   contains a correctly-formed <a href="calshow:…"> anchor), and the
   calendar name in small italics at the end: <em>(Calendar name)</em>.
5. Each email item line must use the item's pre-rendered `subject_html`
   VERBATIM as the linked subject. It already contains a correctly-
   formed anchor (Gmail web URL when available, Apple Mail URL otherwise,
   with an optional "(in Mail)" alternate). Do NOT construct your own
   <a href="…"> anchors for email items; do NOT modify the URLs inside
   `subject_html`; do NOT shorten or paraphrase them. Just paste the
   string as-is.
6. Chat messages have no link. Just state the sender and the essence of
   the message in one line.
7. Be terse. Short lines. Group by date with a date heading.
8. If nothing new from email/messages, say so explicitly in one line.

Output format: HTML fragment (will be sent as an email body and also
written to a local .html file). Use these tags: <h2>, <h3>, <ul>, <li>,
<a href="...">, <strong>, <em>, <span style="color:#RRGGBB">. No other
inline styles. No <html>/<body> wrapper (one is added around your output
later). No preamble or sign-off.

Do not invent events. If an email or message is ambiguous, flag it with
"(verify)". URLs you see in `title_html` and `subject_html` have been
constructed by a trusted upstream process; never alter them even if they
look odd — Message-IDs legitimately contain characters like @ + = & % ."""


def build_digest(cal_events: list[CalEvent],
                 emails: list[MailItem],
                 messages: list[ChatMessage],
                 yesterday_html: str) -> tuple[str, dict]:
    """Dispatch to the configured backend (ollama / openai_compatible).

    Returns (html, stats). stats always carries model, input_tokens,
    output_tokens, and elapsed_s, regardless of which backend ran.
    """
    payload = {
        "today": dt.date.today().isoformat(),
        "calendar_window_days": CFG["calendar_days"],
        "calendar": [asdict(e) for e in cal_events],
        "emails": [asdict(m) for m in emails],
        "messages": [asdict(m) for m in messages],
        "yesterday_digest_html": yesterday_html,
    }
    user_content = (
        "Here is today's input. Produce the digest as specified.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )
    prompt_chars = len(SYSTEM_PROMPT) + len(user_content)
    counts = (len(cal_events), len(emails), len(messages))

    backend = CFG["backend"]
    if backend == "openai_compatible":
        return _call_openai_compatible(user_content, prompt_chars, counts)
    if backend == "ollama":
        return _call_ollama(user_content, prompt_chars, counts)
    raise RuntimeError(
        f"unknown DIGEST_BACKEND={backend!r}; expected 'ollama' or "
        "'openai_compatible'"
    )


def _call_ollama(user_content: str, prompt_chars: int,
                 counts: tuple[int, int, int]) -> tuple[str, dict]:
    n_events, n_emails, n_messages = counts
    body = {
        "model": CFG["ollama_model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # Stream so we can log incremental progress instead of going silent
        # for the entire generation (which on an 8B model + a busy day's
        # input can take a few minutes).
        "stream": True,
        "options": {
            # Keep it deterministic-ish for day-to-day continuity.
            "temperature": 0.2,
            "num_predict": 4000,
        },
    }
    log.info(
        "ollama: calling %s at %s (timeout=%ds, prompt=%d chars: "
        "%d events, %d emails, %d messages)",
        CFG["ollama_model"], CFG["ollama_url"], CFG["ollama_timeout"],
        prompt_chars, n_events, n_emails, n_messages,
    )
    req = urllib.request.Request(
        f"{CFG['ollama_url']}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = dt.datetime.now()
    chunks: list[str] = []
    final_obj: dict = {}
    first_token_at: float | None = None
    last_heartbeat = t0
    lines_seen = 0
    bytes_seen = 0
    HEARTBEAT_EVERY_S = 10.0

    log.info("ollama: opening POST %s/api/chat", CFG['ollama_url'].rstrip('/'))
    try:
        with urllib.request.urlopen(req, timeout=CFG["ollama_timeout"]) as resp:
            te = resp.getheader("Transfer-Encoding") or "(none)"
            cl = resp.getheader("Content-Length") or "(none)"
            log.info(
                "ollama: connection open (HTTP %s, Transfer-Encoding=%s, "
                "Content-Length=%s) — reading stream…",
                resp.status, te, cl,
            )
            # Explicit readline() loop instead of `for raw in resp` so we
            # can track bytes/lines and detect a silent empty response.
            # NDJSON: one JSON object per line. Non-final lines carry an
            # incremental message.content; the final line has done=true and
            # the timing/token stats but no content.
            while True:
                raw = resp.readline()
                if not raw:
                    break
                lines_seen += 1
                bytes_seen += len(raw)
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ollama: skipping non-JSON line: %r", raw[:200])
                    continue
                token = (obj.get("message") or {}).get("content") or ""
                if token:
                    if first_token_at is None:
                        first_token_at = (dt.datetime.now() - t0).total_seconds()
                        log.info("ollama: first token after %.1fs — generating…",
                                 first_token_at)
                    chunks.append(token)
                if obj.get("done"):
                    final_obj = obj
                    break
                now = dt.datetime.now()
                if (now - last_heartbeat).total_seconds() >= HEARTBEAT_EVERY_S:
                    elapsed_s = (now - t0).total_seconds()
                    chars_so_far = sum(len(c) for c in chunks)
                    rate = chars_so_far / elapsed_s if elapsed_s > 0 else 0
                    log.info("ollama: streaming… %d chars in %.0fs (~%.0f chars/s)",
                             chars_so_far, elapsed_s, rate)
                    last_heartbeat = now
            log.info("ollama: stream ended (%d lines, %d bytes read)",
                     lines_seen, bytes_seen)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"ollama request failed: {exc}. Is 'ollama serve' running at "
            f"{CFG['ollama_url']}, and is the model '{CFG['ollama_model']}' "
            f"pulled? Try:  ollama pull {CFG['ollama_model']}"
        ) from exc
    elapsed = (dt.datetime.now() - t0).total_seconds()
    if not chunks:
        log.error(
            "ollama: stream produced 0 content tokens in %.1fs "
            "(read %d lines / %d bytes from server). The model may have "
            "rejected the prompt as too long, or the server returned an "
            "error response. Try a smaller prompt (lower DIGEST_EMAIL_DAYS) "
            "or a model with more context.",
            elapsed, lines_seen, bytes_seen,
        )

    html = "".join(chunks).strip()
    stats = {
        "model": CFG["ollama_model"],
        "input_tokens": int(final_obj.get("prompt_eval_count", 0) or 0),
        "output_tokens": int(final_obj.get("eval_count", 0) or 0),
        "elapsed_s": elapsed,
    }
    # load_duration / prompt_eval_duration come back in nanoseconds; convert
    # to seconds for human-readable logging. They explain where the time
    # went — useful when the run feels slow.
    load_s = (final_obj.get("load_duration", 0) or 0) / 1e9
    prompt_eval_s = (final_obj.get("prompt_eval_duration", 0) or 0) / 1e9
    eval_s = (final_obj.get("eval_duration", 0) or 0) / 1e9
    log.info(
        "ollama: done — model=%s prompt_eval=%d (%.1fs) eval=%d (%.1fs) "
        "load=%.1fs total=%.1fs output=%d chars",
        stats["model"], stats["input_tokens"], prompt_eval_s,
        stats["output_tokens"], eval_s, load_s, elapsed, len(html),
    )
    return html, stats


def _call_openai_compatible(user_content: str, prompt_chars: int,
                            counts: tuple[int, int, int]) -> tuple[str, dict]:
    """Call any OpenAI-compatible /v1/chat/completions endpoint.

    Works for DeepSeek, Moonshot/Kimi, Google Gemini's OpenAI-compat
    endpoint, Alibaba DashScope, and others that implement the OpenAI
    chat-completions schema (including SSE streaming with
    `data: {...}` lines terminated by `data: [DONE]`).
    """
    n_events, n_emails, n_messages = counts
    if not CFG["openai_api_key"]:
        raise RuntimeError(
            "DIGEST_BACKEND=openai_compatible but OPENAI_API_KEY is empty. "
            "run.sh should pull the key from Keychain entry 'daily-digest-openai'."
        )
    if not CFG["openai_base_url"] or not CFG["openai_model"]:
        raise RuntimeError(
            "DIGEST_BACKEND=openai_compatible requires OPENAI_BASE_URL and "
            "OPENAI_MODEL in ~/.config/daily-digest/config.env."
        )

    # Loud, every-run reminder that personal content is leaving the host.
    log.warning(
        "BACKEND=openai_compatible — sending %d chars of email/calendar/"
        "message content to %s (model=%s). This data leaves your machine. "
        "Set DIGEST_BACKEND=ollama to keep everything local.",
        prompt_chars, CFG["openai_base_url"], CFG["openai_model"],
    )

    body = {
        "model": CFG["openai_model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "stream": True,
        # Many providers honour stream_options.include_usage; the rest
        # ignore it harmlessly.
        "stream_options": {"include_usage": True},
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    log.info(
        "openai: calling %s at %s (timeout=%ds, prompt=%d chars: "
        "%d events, %d emails, %d messages)",
        CFG["openai_model"], CFG["openai_base_url"], CFG["openai_timeout"],
        prompt_chars, n_events, n_emails, n_messages,
    )
    endpoint = f"{CFG['openai_base_url'].rstrip('/')}/chat/completions"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CFG['openai_api_key']}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    t0 = dt.datetime.now()
    chunks: list[str] = []
    usage: dict = {}
    first_token_at: float | None = None
    last_heartbeat = t0
    lines_seen = 0
    bytes_seen = 0
    HEARTBEAT_EVERY_S = 10.0

    log.info("openai: opening POST %s", endpoint)
    try:
        with urllib.request.urlopen(req, timeout=CFG["openai_timeout"]) as resp:
            te = resp.getheader("Transfer-Encoding") or "(none)"
            ct = resp.getheader("Content-Type") or "(none)"
            log.info(
                "openai: connection open (HTTP %s, Transfer-Encoding=%s, "
                "Content-Type=%s) — reading SSE stream…",
                resp.status, te, ct,
            )
            while True:
                raw = resp.readline()
                if not raw:
                    break
                lines_seen += 1
                bytes_seen += len(raw)
                line = raw.strip()
                if not line:
                    continue
                # SSE protocol: ignore everything that isn't a data: line
                # (comments start with ':', event/id lines aren't relevant).
                if not line.startswith(b"data:"):
                    continue
                payload_raw = line[len(b"data:"):].lstrip()
                if payload_raw == b"[DONE]":
                    break
                try:
                    obj = json.loads(payload_raw)
                except json.JSONDecodeError:
                    log.warning("openai: skipping non-JSON SSE: %r",
                                payload_raw[:200])
                    continue
                # Token text lives in choices[0].delta.content per the
                # chat.completion.chunk schema.
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    token = delta.get("content") or ""
                    if token:
                        if first_token_at is None:
                            first_token_at = (dt.datetime.now() - t0).total_seconds()
                            log.info("openai: first token after %.1fs — generating…",
                                     first_token_at)
                        chunks.append(token)
                # Final chunk often carries usage; OpenAI sends a
                # zero-choice chunk with usage when include_usage=True.
                if obj.get("usage"):
                    usage = obj["usage"]
                now = dt.datetime.now()
                if (now - last_heartbeat).total_seconds() >= HEARTBEAT_EVERY_S:
                    elapsed_s = (now - t0).total_seconds()
                    chars_so_far = sum(len(c) for c in chunks)
                    rate = chars_so_far / elapsed_s if elapsed_s > 0 else 0
                    log.info("openai: streaming… %d chars in %.0fs (~%.0f chars/s)",
                             chars_so_far, elapsed_s, rate)
                    last_heartbeat = now
            log.info("openai: stream ended (%d lines, %d bytes read)",
                     lines_seen, bytes_seen)
    except urllib.error.HTTPError as exc:
        # HTTPError carries the response body, which usually says exactly
        # what's wrong (bad key, unknown model, rate limit). Don't lose it.
        try:
            body_txt = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body_txt = ""
        raise RuntimeError(
            f"openai-compatible request failed: HTTP {exc.code} {exc.reason}"
            + (f" — {body_txt}" if body_txt else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"openai-compatible request failed: {exc}. Check OPENAI_BASE_URL "
            f"({CFG['openai_base_url']}) is reachable."
        ) from exc

    elapsed = (dt.datetime.now() - t0).total_seconds()
    if not chunks:
        log.error(
            "openai: stream produced 0 content tokens in %.1fs "
            "(read %d lines / %d bytes). Check API key, model name, and "
            "rate-limit / quota status with the provider.",
            elapsed, lines_seen, bytes_seen,
        )

    html = "".join(chunks).strip()
    stats = {
        "model": CFG["openai_model"],
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "elapsed_s": elapsed,
    }
    log.info(
        "openai: done — model=%s prompt_tokens=%d completion_tokens=%d "
        "total=%.1fs output=%d chars",
        stats["model"], stats["input_tokens"], stats["output_tokens"],
        elapsed, len(html),
    )
    return html, stats


# ---------------------------------------------------------------------------
# State + send
# ---------------------------------------------------------------------------

def _build_calendar_legend(cal_events: list[CalEvent]) -> str:
    """Render a small legend box listing each calendar present in the
    digest with its colour swatch — like a map key. Rendered in Python
    rather than asked of the model so the visual is deterministic."""
    seen: dict[str, str] = {}
    for e in cal_events:
        if e.calendar and e.calendar not in seen:
            seen[e.calendar] = e.color or "#808080"
    if not seen:
        return ""
    items = "".join(
        f'<li style="margin:2px 0;line-height:1.4">'
        f'<span style="color:{colour};font-size:1.2em">●</span>&nbsp;{name}'
        f'</li>'
        for name, colour in sorted(seen.items(), key=lambda kv: kv[0].lower())
    )
    return (
        '\n<div style="border:1px solid #ccc;border-radius:6px;'
        'padding:10px 14px;margin-top:20px;font-size:0.9em;'
        'background:#fafafa;display:inline-block">'
        '<strong style="display:block;margin-bottom:6px">Calendars</strong>'
        f'<ul style="list-style:none;padding:0;margin:0">{items}</ul>'
        '</div>\n'
    )


REPO_URL = "https://github.com/profLewis/daily-digest"

def _build_footer() -> str:
    """Provenance footer: which machine, which user, which repo. Lets the
    recipient verify the source and distinguish digests from multiple
    machines if they run it on more than one."""
    host = platform.node() or "unknown-host"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "?"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    return (
        '\n<hr style="border:none;border-top:1px solid #eee;margin-top:18px">\n'
        '<div style="font-size:0.8em;color:#888;margin-top:8px;line-height:1.4">'
        f'Generated {now} on <strong>{host}</strong> '
        f'(user <code>{user}</code>) by '
        f'<a href="{REPO_URL}" style="color:#888">daily-digest</a>. '
        'Review the code before trusting anything it says.'
        '</div>\n'
    )


def _wrap_html(fragment: str) -> str:
    """Wrap the model's HTML fragment in a minimal document so browsers
    and mail clients render UTF-8 correctly. Without <meta charset>
    browsers fall back to Latin-1 and em-dashes render as â€"."""
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


def save_today(html_for_continuity: str, html_for_archive: str) -> None:
    """yesterday.html stores the model's raw fragment (fed back verbatim
    tomorrow). The dated archive is a wrapped, human-friendly copy."""
    (CFG["state_dir"] / "yesterday.html").write_text(
        html_for_continuity, encoding="utf-8")
    (CFG["state_dir"] / f"digest-{dt.date.today().isoformat()}.html").write_text(
        _wrap_html(html_for_archive), encoding="utf-8")


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

def _check_model_freshness() -> None:
    """Warn if the active Ollama model hasn't been refreshed in a while.

    Non-fatal. Hits Ollama's /api/show for the local manifest's
    `modified_at` timestamp (= when you last pulled the tag). If it's
    older than DIGEST_MODEL_STALE_DAYS (default 30), emit a warning
    line pointing at update-model.sh. All errors swallowed — this is
    advisory, never the reason a run fails. No-op for non-ollama
    backends (hosted providers manage versioning on their end)."""
    if CFG["backend"] != "ollama":
        return
    threshold_days = int(os.environ.get("DIGEST_MODEL_STALE_DAYS", "30"))
    try:
        req = urllib.request.Request(
            f"{CFG['ollama_url'].rstrip('/')}/api/show",
            data=json.dumps({"name": CFG["ollama_model"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.debug("model freshness check skipped: %s", exc)
        return

    modified_str = data.get("modified_at") or ""
    if not modified_str:
        return
    try:
        modified_at = dt.datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
    except ValueError:
        return
    if modified_at.tzinfo is None:
        modified_at = modified_at.replace(tzinfo=dt.timezone.utc)
    age_days = (dt.datetime.now(dt.timezone.utc) - modified_at).days
    script_dir = Path(__file__).parent
    if age_days >= threshold_days:
        log.warning(
            "model %s was last refreshed %d days ago (>= %d). "
            "Run %s/update-model.sh to pull upstream fixes, or let the "
            "weekly LaunchAgent handle it.",
            CFG["ollama_model"], age_days, threshold_days, script_dir,
        )
    else:
        log.info("model %s last refreshed %d days ago (threshold %d)",
                 CFG["ollama_model"], age_days, threshold_days)


def _acquire_single_instance_lock():
    """Stop two copies of daily-digest running at once. Gmail throttles to
    ~15 concurrent IMAP connections per account, and overlapping runs can
    also corrupt yesterday.html.

    Returns:
      - file handle on success (keep it alive; closing releases the lock).
      - the integer PID of the holding process if another run has the lock.
      - 0 if the lock is held but the holder's PID can't be parsed
        (stale-but-alive corner case; treat as busy with unknown PID).

    The file is opened without truncation so the previous holder's PID
    survives a failed lock attempt — we need it to be able to kill the
    holder if the user asks us to.
    """
    lockfile = CFG["state_dir"] / "daily-digest.lock"
    fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o600)
    lf = os.fdopen(fd, "r+")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lf.seek(0)
        existing = lf.read().strip()
        lf.close()
        try:
            return int(existing)
        except ValueError:
            return 0
    # Got the lock — overwrite with our own PID.
    lf.seek(0); lf.truncate()
    lf.write(f"{os.getpid()}\n"); lf.flush()
    return lf


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by someone else. Either way,
        # it's there — we shouldn't pretend it isn't.
        return True
    return True


def _handle_existing_run(other_pid: int, interactive: bool) -> "int | object":
    """Decide what to do about an in-progress run.

    Returns either a held lock file (success — we killed the holder and
    grabbed the lock) or an integer exit code to return from main().
    """
    if not _process_alive(other_pid):
        # Stale lock — the previous run died without releasing it.
        # Just take it.
        log.warning("found stale lock from PID %d (process gone); reclaiming",
                    other_pid)
        try:
            (CFG["state_dir"] / "daily-digest.lock").unlink()
        except FileNotFoundError:
            pass
        new_lock = _acquire_single_instance_lock()
        if isinstance(new_lock, int):
            log.error("could not acquire lock after reclaiming stale lock")
            return 4
        return new_lock

    if not interactive:
        # Non-interactive (launchd, cron) — never kill a running peer
        # silently. Exit and let the previous run finish.
        log.error("another daily-digest instance is already running "
                  "(PID %d); exiting (run interactively to kill it)",
                  other_pid)
        return 4

    # Interactive: ask the user.
    print(f"\nAnother daily-digest run is already in progress (PID {other_pid}).",
          file=sys.stderr)
    try:
        ans = input("Kill it and start a new run? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return 4
    if ans not in ("y", "yes"):
        log.info("leaving PID %d alone; exiting", other_pid)
        return 4

    log.info("sending SIGTERM to PID %d", other_pid)
    try:
        os.kill(other_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # Give it up to 10s to release. AppleScript subprocesses sometimes
    # take a couple of seconds to wind down; SMTP/IMAP closes are fast.
    for _ in range(20):
        time.sleep(0.5)
        retry = _acquire_single_instance_lock()
        if not isinstance(retry, int):
            log.info("lock acquired after killing PID %d", other_pid)
            return retry

    log.warning("PID %d still holding lock after 10s; sending SIGKILL",
                other_pid)
    try:
        os.kill(other_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(1)
    retry = _acquire_single_instance_lock()
    if isinstance(retry, int):
        log.error("could not acquire lock even after SIGKILL of PID %d",
                  other_pid)
        return 4
    return retry


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="build the digest and save it, but don't send email")
    args = ap.parse_args()

    lock = _acquire_single_instance_lock()
    if isinstance(lock, int):
        # Lock is held by another process. PID is in `lock` (0 = unknown).
        result = _handle_existing_run(lock, sys.stdin.isatty())
        if isinstance(result, int):
            return result
        lock = result

    _check_model_freshness()

    rc = 0
    stats: dict = {}
    cal_n = mail_n = mail_app_n = imsg_n = 0
    emailed = False

    try:
        cal = fetch_calendar(CFG["calendar_days"])
        cal_n = len(cal)

        gmail_items = fetch_gmail(CFG["email_days"])
        mail_n = len(gmail_items)

        mail_app_items: list[MailItem] = []
        if CFG["use_mail_app"]:
            mail_app_items = fetch_mail_app(CFG["email_days"])
            mail_app_n = len(mail_app_items)
        mail = merge_mail(gmail_items, mail_app_items)

        messages: list[ChatMessage] = []
        if CFG["use_imessage"]:
            messages = fetch_imessages(CFG["imessage_days"])
            imsg_n = len(messages)

        yesterday = load_yesterday()
        html, stats = build_digest(cal, mail, messages, yesterday)
        if not html:
            log.error("empty digest from local model, aborting")
            rc = 1
            return rc

        # Append a colour legend and provenance footer (rendered in
        # Python for determinism). yesterday.html keeps the model's raw
        # output only, so tomorrow's continuity context isn't polluted
        # with the legend and footer markup.
        html_with_legend = html + _build_calendar_legend(cal) + _build_footer()

        if args.dry_run:
            preview = CFG["state_dir"] / "preview.html"
            preview.write_text(_wrap_html(html_with_legend), encoding="utf-8")
            log.info("dry run — preview at %s", preview)
            print(f"\nPreview written: {preview}")
            print(f"Open it:         open {preview}")
            return 0

        # Clean up old digests before sending today's, so tomorrow's
        # search window won't include today's freshly-sent copy.
        try:
            trash_old_digests(CFG["keep_days"])
        except Exception:
            log.exception("digest cleanup failed (non-fatal, continuing)")

        save_today(html, html_with_legend)
        send_email(html_with_legend)
        emailed = True
        return 0

    except Exception:
        log.exception("digest run failed")
        rc = 2
        return rc
    finally:
        elapsed = stats.get("elapsed_s")
        elapsed_str = f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "n/a"
        log.info(
            "run summary: calendar_events=%d gmail_imap=%d mail_app=%d "
            "imessages=%d model=%s prompt_eval=%d eval=%d elapsed=%s "
            "emailed=%s dry_run=%s exit=%d",
            cal_n, mail_n, mail_app_n, imsg_n,
            stats.get("model", CFG.get("openai_model") or CFG["ollama_model"]),
            stats.get("input_tokens", 0), stats.get("output_tokens", 0),
            elapsed_str, emailed, args.dry_run, rc,
        )


if __name__ == "__main__":
    sys.exit(main())
