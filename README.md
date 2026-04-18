# daily-digest

> ### 🖥 macOS only
>
> This repo is written **for macOS** (tested on macOS 13+). It uses AppleScript, `launchd`, the macOS Keychain, and the Apple Mail/Messages local stores — none of which exist on Linux or Windows. A Linux port would need: `launchd` → `systemd --user` timer or `cron`; Keychain → `secret-tool` or `pass`; AppleScript calendar → CalDAV/ICS or a Google Calendar API client; Apple Mail → drop entirely or replace with IMAP accounts; iMessage → drop entirely. The Gmail IMAP + Anthropic + SMTP paths are already portable. If you do port it, please open an issue so others can find your fork.

> # 🛑 STOP — READ THIS FIRST
>
> **This is an exploratory / educational project.** Its purpose is to explore how to wire together the Claude API, Gmail (IMAP + SMTP), the macOS Calendar, Apple Mail, and iMessage into a single daily briefing. It is **not** a polished product, it has **not** been security-audited by anyone other than its author, and it **will change without notice**.
>
> Before you run a single line of it:
>
> 1. **Verify the source.** Only run copies cloned directly from `https://github.com/profLewis/daily-digest`. A fork or a downloaded zip from anywhere else may contain modifications you haven't inspected. Check `git remote -v` after cloning.
> 2. **Read the code.** It is small on purpose — about 600 lines of Python and three short shell scripts. Read `daily_digest.py`, `install.sh`, `run.sh`, and `uninstall.sh` end-to-end before running `install.sh`.
> 3. **Read the [Privacy statement](#privacy-what-leaves-your-machine) carefully.** This tool sends your email content, calendar events, and (if you opt in) iMessage conversations to the Anthropic Claude API. That data leaves your machine. If that isn't acceptable to you, don't use this.
> 4. **Know how to remove it.** See [Uninstall](#uninstall) below — it is a single command.
>
> If any of steps 1–4 feels like too much, **don't install this**. Use a hosted calendar digest service instead.

> ## 💳 Cost warning
>
> This tool makes **one Anthropic Claude API call per run** (normally one per day). Anthropic bills per token. The bill is small for most people — typically a few cents a day on Opus, a fraction of a cent on Sonnet — but **it is not zero**, and it scales with how many events and how much email you have. Set a spending cap in your [Anthropic Console](https://console.anthropic.com/settings/limits) before you leave the job running unattended. Every run writes `anthropic: usage model=… input_tokens=… output_tokens=…` to `~/Library/Logs/daily-digest.log` so you can see exactly what you're spending — see [Cost](#cost) below for details.

> ## ⚠️ Security warning — read before you install
>
> This code integrates four sensitive things on your machine:
>
> - **Anthropic Claude API** — outbound HTTPS, using your API key
> - **Your Gmail account** — IMAP read and SMTP send, using a Gmail app password
> - **Your local macOS Calendar** — every calendar attached to the Calendar app (iCloud, Google, Exchange, subscribed feeds)
> - **Your macOS login Keychain** — where the Gmail and Anthropic secrets are stored
>
> It is designed to do *only* those things. But you are running code from a public repository on your own hardware, with credentials that can read your mail, send mail as you, and spend money on the Anthropic API.
>
> **Never run `install.sh` without reading the source first.** The repo is small on purpose — about 400 lines of Python plus three short shell scripts. Read them end-to-end and satisfy yourself that nothing in there:
>
> - exfiltrates data to any host other than `imap.gmail.com`, `smtp.gmail.com`, and `api.anthropic.com`
> - writes anywhere outside `~/.config/daily-digest/`, `~/.local/share/daily-digest/`, `~/Library/Logs/`, and `~/Library/LaunchAgents/`
> - invokes unexpected binaries (only `python3`, `osascript`, `security`, `launchctl`, `pip`, and `sed` should appear)
> - does anything with secrets other than store them in the Keychain and read them back at run time
>
> No automated tool can prove arbitrary code is benign — that's an undecidable problem in the general case. Static analysis can only catch *known bad patterns*. See [Auditing the code](#auditing-the-code) below for what actually helps.

A macOS background job that emails you a tidy HTML summary of your day each morning.

Every night at 02:00 it wakes up, reads your Calendar and the last few days of Gmail, asks Claude to turn them into a clean digest, and sends the result to your inbox. Events that appear in email but aren't yet on your calendar are surfaced separately so nothing slips through.

**Need to get rid of it right now?** Run `~/daily-digest/uninstall.sh` (or `./uninstall.sh --purge` to also wipe archived digests). Full details in the [Uninstall](#uninstall) section.

## Privacy — what leaves your machine

> ⚠️ **This is the single most important section. Read it in full before deciding to use the tool.** If you're not comfortable with anything here, don't install.

Every run opens exactly three outbound network connections:

- **`imap.gmail.com:993` and `smtp.gmail.com:465`** — Google. Reading and sending your own Gmail. Google already has this data.
- **`api.anthropic.com:443`** — Anthropic's Claude API. **Your message contents, calendar events, and (if enabled) iMessage conversations are sent here as part of every request.**

Nothing else leaves the machine. No telemetry, no analytics, no third-party services.

### What gets sent to Anthropic

Every run sends one `messages.create` request containing a JSON payload Claude can read. That payload includes:

| Data | Source | What's included |
|---|---|---|
| Calendar events | macOS Calendar (all attached accounts) | Title, start/end, calendar name, location, up to 500 chars of notes |
| Gmail messages | `[Gmail]/All Mail`, last 3 days by default | Subject, sender, date, Message-ID, first ~1000 chars of body |
| Mail.app messages *(if `DIGEST_USE_MAIL_APP=true`)* | Every account Mail.app is logged into | As above, per account |
| iMessage/SMS *(if `DIGEST_USE_IMESSAGE=true`)* | `~/Library/Messages/chat.db`, last 3 days | Sender handle (phone/email), date, up to 1000 chars of message text |
| Yesterday's digest | Local file | Full HTML from yesterday's run (contains event titles + email subjects) |

Claude's HTML response comes back and gets emailed to you and saved locally.

### What Anthropic does with that data

Per Anthropic's published policy (as at the time of writing — **verify yourself at the links below**, not from this README):

1. **Not used to train models.** For API traffic, Anthropic's commercial terms state inputs and outputs are not used to train their models. This is different from the consumer `claude.ai` product, which has different defaults.
2. **Retained up to 30 days** for abuse monitoring and Trust & Safety review, then deleted. Enterprise customers may negotiate zero-retention agreements; standard API access has this retention.
3. **May be reviewed by humans if flagged** by automated moderation classifiers. For a calendar/email digest this is unlikely to trigger but it is how the policy works.
4. **Runs on AWS/GCP infrastructure.** Your API traffic transits those providers' networks (TLS in transit).
5. **Metadata visible to you** in the Anthropic Console — token counts per call, but not content.

**Authoritative sources** — read these before trusting the summary above:

- [https://www.anthropic.com/legal/privacy](https://www.anthropic.com/legal/privacy)
- [https://www.anthropic.com/legal/commercial-terms](https://www.anthropic.com/legal/commercial-terms)
- [https://privacy.anthropic.com/](https://privacy.anthropic.com/) — data handling, sub-processors
- [https://trust.anthropic.com/](https://trust.anthropic.com/)

### Consent considerations

If you enable iMessage scanning, you are sending the text of conversations from other people — **who have not consented to have their words sent to an AI provider** — to Anthropic. That's a judgement call you should make deliberately. Same applies to email senders and Mail.app inbox content, but message conversations are generally held to a higher privacy bar than email.

### Kill switches

- **Stop the daily run:** `launchctl unload ~/Library/LaunchAgents/com.user.dailydigest.plist`
- **Disable iMessage scanning:** edit `~/.config/daily-digest/config.env`, set `DIGEST_USE_IMESSAGE="false"`
- **Disable Mail.app reading:** same file, set `DIGEST_USE_MAIL_APP="false"`
- **Full removal:** `~/daily-digest/uninstall.sh --purge` — see [Uninstall](#uninstall)

## What it does, step by step

1. **Read the calendar.** An AppleScript pulls every event from the local macOS Calendar app for the next *N* days (default 14). Because it reads the unified store, iCloud, Google, Exchange, and subscribed calendars all come through in one pass — no per-provider OAuth.
2. **Read recent Gmail.** Connects to `imap.gmail.com` over SSL using an app password, opens `[Gmail]/All Mail`, and fetches the last *M* days of messages (default 3). Captures subject, sender, date, a plain-text snippet, and the `Message-ID` so the digest can link back to each thread in Mail.app (`message://…`) or Gmail on the web.
   - **Optional — Mail.app accounts (opt-in).** Set `DIGEST_USE_MAIL_APP=true` to also pull recent inbox messages from every account Mail.app is logged into (iCloud, work, Exchange, any IMAP account). See [Mail.app](#optional-mailapp-other-email-accounts).
   - **Optional — iMessage/SMS (opt-in).** Set `DIGEST_USE_IMESSAGE=true` to scan recent iMessage and SMS conversations for event-like content. Privacy-sensitive — see the [iMessage opt-in](#optional-imessagesms-scanning) section below.
3. **Feed yesterday's digest back in.** The previous morning's rendered HTML is stored on disk and passed to Claude as context. The system prompt tells Claude to keep wording close to yesterday's where the underlying facts haven't changed, so the digest reads as a stable daily document rather than a freshly-paraphrased one each morning.
4. **Ask Claude to produce the digest.** One call to the Anthropic API. Claude groups events by day, then scans emails for event-like content (invitations, bookings, flights, deliveries, deadlines) and flags anything that isn't already on the calendar under "Possible events from email not yet in calendar". Ambiguous items get a `(verify)` tag rather than being invented.
5. **Render and send.** The response is an HTML fragment with clickable links to each event (`calshow:` URLs open Calendar on that day; `message://` URLs open the thread in Mail). It goes out via Gmail SMTP (SSL, port 465) to whichever address you configured.
6. **Persist state.** Today's digest is written to `~/.local/share/daily-digest/yesterday.html` (for tomorrow's continuity) and archived as `digest-YYYY-MM-DD.html`.

All of this is orchestrated by a `launchd` agent, so it survives reboots and doesn't need you to be logged into a terminal.

## Prerequisites

- macOS 13 or later
- Python 3.9+
- A Gmail account with 2FA on — [generate an app password](https://myaccount.google.com/apppasswords)
- An [Anthropic API key](https://console.anthropic.com/)

## Install

Set `YOU` to your GitHub username first, then copy-paste:

```bash
export YOU=your-github-username
git clone https://github.com/${YOU}/daily-digest.git ~/daily-digest
cd ~/daily-digest
./install.sh
```

The installer walks you through everything:

1. Checks for `python3` and installs the `anthropic` package if needed.
2. Prompts for your Gmail address, app password, and Anthropic key.
3. Validates each — a real IMAP login, a real API ping — before storing anything.
4. Writes the secrets to your login Keychain (never to disk in plaintext).
5. Writes non-secret config to `~/.config/daily-digest/config.env`.
6. Installs a `launchd` agent at `~/Library/LaunchAgents/com.user.dailydigest.plist` set to fire at 02:00 daily.
7. Runs a dry-run so macOS raises its Calendar permission prompts — accept them.

After install, open **System Settings → Privacy & Security → Full Disk Access** and tick `/bin/bash`. Without this, the 02:00 run can't read Calendar when you're not actively logged in. The installer reminds you.

## Running it manually

```bash
launchctl start com.user.dailydigest    # fire the scheduled job now
~/daily-digest/run.sh                   # equivalent: runs directly
~/daily-digest/run.sh --dry-run         # build the digest, save preview, don't email
```

A dry run writes `~/.local/share/daily-digest/preview.html`. Open it in a browser to see what would have been sent.

**Try an optional source for one run only** (without changing config):

```bash
DIGEST_USE_IMESSAGE=true ~/daily-digest/run.sh --dry-run
DIGEST_USE_MAIL_APP=true ~/daily-digest/run.sh --dry-run
```

That way you can see what iMessage or Mail.app content would appear in the digest before committing to having it in every run.

## Configuration

Edit `~/.config/daily-digest/config.env`:

| Variable | Default | Meaning |
|---|---|---|
| `DIGEST_GMAIL_ADDRESS` | — | Gmail account to read (and send from) |
| `DIGEST_RECIPIENT` | same as above | Where the digest is emailed |
| `DIGEST_CAL_DAYS` | `14` | Days of calendar to include |
| `DIGEST_EMAIL_DAYS` | `3` | Days of Gmail to scan |
| `DIGEST_KEEP_DAYS` | `1` | Days of past digests to keep in your inbox — see below |
| `DIGEST_USE_MAIL_APP` | `false` | Also scan Mail.app inboxes (all accounts Mail is logged into) |
| `DIGEST_USE_IMESSAGE` | `false` | Also scan iMessage/SMS — see [iMessage opt-in](#optional-imessagesms-scanning) |
| `DIGEST_IMESSAGE_DAYS` | `3` | How far back to scan iMessage/SMS |
| `ANTHROPIC_MODEL` | `claude-opus-4-7` | Model. `claude-sonnet-4-6` is cheaper and fast enough. |

Changes take effect on the next run — no reinstall.

### Optional: Mail.app (other email accounts)

By default the tool only reads Gmail over IMAP. If you use **Apple Mail** with additional accounts (iCloud, work Exchange, extra IMAP accounts), set `DIGEST_USE_MAIL_APP=true` and the script will also read the Inbox of every account Mail.app is logged into — no per-account credentials needed. First run triggers an "allow Automation" prompt for Mail. On large mailboxes this can add 30–120 seconds to each run.

### Optional: iMessage/SMS scanning

> ⚠️ **Privacy-sensitive.** Enabling this sends the text of your recent iMessage and SMS conversations to the Claude API as part of the digest input. Only do this if you're comfortable with that.

Set `DIGEST_USE_IMESSAGE=true` to have the script read the last `DIGEST_IMESSAGE_DAYS` days (default 3) of messages from `~/Library/Messages/chat.db` — the same store Messages.app uses. Claude then surfaces anything that looks like a calendar item (appointments, bookings, RSVPs, flights) that isn't already on your calendar. Nothing is written back to Messages; the script is read-only against `chat.db`.

Requirements:

- **Full Disk Access** for `/bin/bash` (and for your terminal, if you want to test it manually). System Settings → Privacy & Security → Full Disk Access.
- This is off by default — the installer asks you explicitly.

**Enable for the scheduled 02:00 run (edit config):**

```bash
$EDITOR ~/.config/daily-digest/config.env
# set DIGEST_USE_IMESSAGE="true", save
```

Config changes take effect on the next run — no reinstall needed. To verify the change is live:

```bash
grep DIGEST_USE_IMESSAGE ~/.config/daily-digest/config.env
```

**Try it for a single manual run, without changing the config:**

```bash
DIGEST_USE_IMESSAGE=true ~/daily-digest/run.sh --dry-run
open ~/.local/share/daily-digest/preview.html
```

**Disable it later:**

```bash
$EDITOR ~/.config/daily-digest/config.env    # set DIGEST_USE_IMESSAGE="false"
```

### Auto-deleting old digests

Each run begins by moving *its own* prior digest emails to Gmail Trash, so the inbox doesn't fill up. Matching is strict: messages only get trashed if they're **from your own address** AND have a subject starting with `Daily digest —`. Nothing else is touched. Gmail empties Trash automatically after 30 days, so old digests are recoverable for a month.

- `DIGEST_KEEP_DAYS=1` (default) — keep only today's digest
- `DIGEST_KEEP_DAYS=7` — keep a week's worth
- `DIGEST_KEEP_DAYS=0` (or any non-positive value) — disable cleanup

### Don't run multiple instances

Gmail's IMAP server allows roughly 15 concurrent connections per account, and overlapping runs can also corrupt `yesterday.html`. The script takes an `fcntl` file lock on `~/.local/share/daily-digest/daily-digest.lock` at startup and exits immediately with code `4` if another copy is already running. So `launchctl start com.user.dailydigest` while a dry-run is still in progress is safe — the second invocation just quits. If you see `[ALERT] Too many simultaneous connections. (Failure)` during install, wait a minute for Gmail to time out the stale connections and try again.

## Checking what's already scheduled on this Mac

Before (or after) installing, it's worth knowing what else is running on a timer so the 02:00 digest doesn't land on top of a backup or another cron. Useful commands:

**`launchd` (user-scope agents — the mechanism this tool uses):**

```bash
# List all loaded launchd jobs for your account:
launchctl list | grep -v com.apple

# Show every user LaunchAgent plist and the time each is set to fire:
for pl in ~/Library/LaunchAgents/*.plist; do
  label=$(/usr/libexec/PlistBuddy -c "Print :Label" "$pl" 2>/dev/null)
  hh=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Hour" "$pl" 2>/dev/null)
  mm=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Minute" "$pl" 2>/dev/null)
  [ -n "$hh" ] && printf "%02d:%02d  %s\n" "$hh" "$mm" "$label"
done | sort
```

**`launchd` (system-scope daemons — Apple and admin jobs):**

```bash
ls /Library/LaunchDaemons/ /Library/LaunchAgents/ 2>/dev/null
```

**`cron` (less common on modern macOS but still honoured):**

```bash
crontab -l           # your user's crontab
sudo crontab -l      # root's crontab
```

**`pmset` (system-wide wake / sleep schedule):**

```bash
pmset -g sched             # show current repeat wake/sleep/shutdown
pmset -g                   # full power-management settings
sudo pmset repeat cancel   # remove any repeat schedule
```

The installer runs a summary of the first three for you automatically and flags anything scheduled within ±15 minutes of your chosen time.

## Choosing the run time

The installer asks what time you'd like the digest to fire (default 02:00) and shows any launchd agents / crontab entries / pmset wake schedules already set on the account so you can avoid a clash. If you pick a time that falls within 15 minutes of another scheduled job it warns you **and automatically suggests a clash-free slot** — press Enter to accept, or say No to keep your original pick (at your own risk).

Re-running `install.sh` keeps the previous time as the default rather than resetting you to 02:00.

To change the time later: re-run `./install.sh` — it reads the existing plist, shows your current time as the default, and you just type the new one.

## Waking the Mac

`launchd` will not wake a sleeping Mac on its own. If you want the run to happen exactly at your scheduled time, set a one-minute-earlier wake. For the default 02:00 schedule:

```bash
sudo pmset repeat wake MTWRFSU 01:59:00
```

For any other schedule, use `HH:MM:00` one minute before your launchd time. The installer prints the exact command at the end.

Check with `pmset -g sched`; cancel with `sudo pmset repeat cancel`. `pmset` only stores one repeat schedule, so setting this overwrites any existing one. If you skip it entirely, the job runs on next wake — usually fine.

## How continuity works

Each successful run writes its HTML to `~/.local/share/daily-digest/yesterday.html`. The next run feeds that file back to Claude as "yesterday's digest", and the system prompt instructs Claude to preserve wording where facts are unchanged. This means a standing meeting doesn't get re-paraphrased every morning — stable wording makes it obvious at a glance what's actually new.

## Files

In the repo:

```
daily-digest-repo/
├── daily_digest.py                        # main script
├── run.sh                                 # wrapper: sources config + Keychain secrets
├── install.sh                             # interactive installer
├── uninstall.sh                           # cleanup
├── requirements.txt                       # Python deps (just `anthropic`)
├── com.user.dailydigest.plist.template    # launchd template
├── LICENSE                                # MIT
└── README.md
```

Runtime files written outside the repo:

```
~/.config/daily-digest/config.env           # non-secret config
~/Library/LaunchAgents/com.user.dailydigest.plist
~/.local/share/daily-digest/                # state (yesterday.html, archives, preview.html)
~/Library/Logs/daily-digest.log             # main log
~/Library/Logs/daily-digest.stdout.log      # launchd stdout
~/Library/Logs/daily-digest.stderr.log      # launchd stderr
```

Secrets live only in the login Keychain:

```
daily-digest-gmail       # Gmail app password
daily-digest-anthropic   # Anthropic API key
```

## Auditing the code

No library can *prove* this or any code only does what it claims — by Rice's theorem, that's undecidable. What automated tools **can** do is flag known bad patterns. Combine a few of them and you cover most of what matters for a small repo like this:

| Tool | What it checks | Install |
|---|---|---|
| [`bandit`](https://bandit.readthedocs.io/) | Python security anti-patterns: `shell=True`, hardcoded passwords, insecure SSL, `eval`, etc. | `pipx install bandit` |
| [`semgrep`](https://semgrep.dev/) | Configurable pattern matching; rich community rule-sets for secrets exfiltration, dodgy imports. | `pipx install semgrep` |
| [`pip-audit`](https://pypi.org/project/pip-audit/) | Checks `requirements.txt` against the CVE database. | `pipx install pip-audit` |
| [`shellcheck`](https://www.shellcheck.net/) | Common shell-script mistakes and injection risks. | `brew install shellcheck` |

Run all four from the repo root:

```bash
bandit -r daily_digest.py
semgrep --config=auto .
pip-audit -r requirements.txt
shellcheck install.sh run.sh uninstall.sh
```

### The irreducible manual check

Tools won't catch a deliberately malicious but syntactically innocent line. Before you run `install.sh`, eyeball these specifically:

1. **Every outbound network call.** In `daily_digest.py` they're easy to enumerate — look for `imaplib.IMAP4_SSL`, `smtplib.SMTP_SSL`, and `anthropic.Anthropic()`. Those are the only three network destinations. Confirm no `urllib`, `requests`, `socket`, or `http` imports have appeared.
2. **Every `subprocess` call.** There is exactly one: `subprocess.run(["osascript", ...])` to read the calendar. It takes no user-controlled input and runs a hard-coded AppleScript. Confirm no `shell=True` anywhere.
3. **Every write to disk.** All writes go through `CFG["state_dir"]` and `CFG["log_file"]`, both of which resolve to `~/.local/share/daily-digest/` and `~/Library/Logs/` respectively. Confirm no other `open(..., "w")` or `Path.write_text` exists.
4. **The three shell scripts.** `install.sh` does Keychain writes, config writes, and a `launchctl load`. `run.sh` reads Keychain and execs Python. `uninstall.sh` reverses it. None should download anything from the internet.

A one-liner that surfaces the network, subprocess, and filesystem surface area of the Python for a quick visual check:

```bash
grep -nE 'imaplib|smtplib|anthropic|subprocess|socket|urllib|requests|http\.|open\(|write_text|mkdir' daily_digest.py
```

If the output looks longer or stranger than you'd expect, stop and investigate.

## Uninstall

**Quick removal** — stops the scheduled run, deletes config, and removes Keychain secrets:

```bash
~/daily-digest/uninstall.sh
```

**Full removal** — also wipes archived digests and `yesterday.html`:

```bash
~/daily-digest/uninstall.sh --purge
```

**Then delete the repo itself** (the uninstaller doesn't touch it, in case you want to reinstall):

```bash
rm -rf ~/daily-digest
```

### What exactly gets removed

The uninstaller cleans up:

| Target | Removed? | Notes |
|---|---|---|
| `~/Library/LaunchAgents/com.user.dailydigest.plist` | ✅ always | Stops the 02:00 run |
| Any running `daily_digest.py` process | ✅ always | `pkill`'d so nothing is mid-write when we remove state |
| `~/.config/daily-digest/` | ✅ always | Config file |
| `daily-digest.lock` in state dir | ✅ always | Instance lockfile |
| Keychain item `daily-digest-gmail` | ✅ always | Your Gmail app password |
| Keychain item `daily-digest-anthropic` | ✅ always | Your Anthropic API key |
| `~/.local/share/daily-digest/` | Only with `--purge` | Archived digests, `yesterday.html`, `preview.html` |
| `~/Library/Logs/daily-digest*.log` | Only with `--purge` | Main log + launchd stdout/stderr logs |
| `~/daily-digest/` (the cloned repo) | ❌ never | `rm -rf ~/daily-digest` removes it |

### What the uninstaller CANNOT remove for you — do these manually

macOS and third parties own these; no script can touch them. The uninstaller prints this list at the end of its run too, so you won't miss it:

- **macOS Automation permissions** → System Settings → Privacy & Security → Automation → remove any `Calendar` or `Mail` entries for `bash`, `osascript`, or your terminal.
- **Full Disk Access** → System Settings → Privacy & Security → Full Disk Access → remove `/bin/bash` if you added it (needed for the 02:00 launchd run), and your terminal if you added it (needed to test iMessage reading manually).
- **Scheduled wake** → if you ran `sudo pmset repeat wake …` per the README, reverse it with `sudo pmset repeat cancel`.
- **Upstream credentials** — the Keychain copies are gone, but the originals still exist:
    - Revoke the Gmail app password: [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
    - Revoke the Anthropic API key: [https://console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)
- **Python package** — the installer `pip install`'d `anthropic` (user site). Uninstall with `pip3 uninstall anthropic` if nothing else needs it.
- **Delivered digest emails** — any that are still in your Gmail stay there. Use Gmail's search for `from:me subject:"Daily digest"` to find and delete them.

### What the uninstaller does NOT touch (intentionally)

- **Your mail, calendars, or messages.** The tool only ever reads them; uninstalling has nothing to reverse.
- **Anthropic billing history.** Past API usage is immutable in your Anthropic Console.

## Troubleshooting

**`osascript is not allowed to send Apple events to Calendar`** → System Settings → Privacy & Security → Automation. Tick Calendar under bash/osascript.

**IMAP login fails** → verify the app password is still in the Keychain:
```bash
security find-generic-password -s daily-digest-gmail -w
```
If it doesn't match your current Google app password, re-run `install.sh`.

**Runs fine manually but silently fails at 02:00 under launchd** → almost always Full Disk Access. Check `~/Library/Logs/daily-digest.stderr.log`.

**Empty digest** → check `~/Library/Logs/daily-digest.log`. Usually a rate limit or a rotated API key.

**Want to see what Claude is being sent** → run with `--dry-run` and inspect `~/.local/share/daily-digest/preview.html`; the log file shows the event and email counts that were included.

## Cost

One Claude API call per run (normally one per day). Inputs are typically 20–60k tokens (mostly email bodies); outputs are a few hundred. Pennies a day on Opus, fractions of a penny on Sonnet.

Every API-touching step writes one line to `~/Library/Logs/daily-digest.log` so you can audit usage and spot runaway runs:

| Log line | What it means |
|---|---|
| `gmail: IMAP login as you@… (last N days)` | IMAP connection opens |
| `gmail: N messages since …, fetching last M` | IMAP SEARCH + FETCH count |
| `gmail: parsed M messages` | IMAP fetch complete |
| `anthropic: calling MODEL (1 messages.create, max_tokens=4000)` | Claude API request outbound |
| `anthropic: usage model=… input_tokens=… output_tokens=… cache_read=… cache_creation=…` | Claude API response, exact token counts |
| `anthropic: estimated cost USD $0.NNNN (list price; actual bill in console)` | Estimated cost for this call, computed from published list prices |
| `smtp: sending digest to … (N bytes)` | SMTP send opens |
| `smtp: sent` | SMTP send complete |
| `trashing digests from … before …` / `trashed N old digest(s)` | IMAP cleanup pass |
| `run summary: calendar_events=… emails_scanned=… anthropic_in=… anthropic_out=… estimated_cost_usd=$… emailed=… exit=…` | End-of-run one-liner with totals; emitted whether the run succeeds or fails |

The cost estimate uses published list prices baked into the script (`MODEL_PRICING_USD_PER_MTOKEN` in `daily_digest.py`). If you change models or Anthropic changes pricing, bump that table — the code will fall back to `estimated_cost_usd=unknown` rather than quote a stale number. These estimates are indicative only; the authoritative figure is in your [Anthropic Console usage page](https://console.anthropic.com/settings/usage).

A week's worth of cost at a glance:

```bash
grep 'run summary' ~/Library/Logs/daily-digest.log | tail -7
```

To monitor live, `tail -f ~/Library/Logs/daily-digest.log`. To see a week's worth of token usage at a glance:

```bash
grep 'anthropic: usage' ~/Library/Logs/daily-digest.log | tail -7
```

Cap spending at the source: set a monthly limit in the [Anthropic Console](https://console.anthropic.com/settings/limits).

## License

MIT — see [LICENSE](./LICENSE).
