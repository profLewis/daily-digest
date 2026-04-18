# daily-digest

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

## What it does, step by step

1. **Read the calendar.** An AppleScript pulls every event from the local macOS Calendar app for the next *N* days (default 14). Because it reads the unified store, iCloud, Google, Exchange, and subscribed calendars all come through in one pass — no per-provider OAuth.
2. **Read recent Gmail.** Connects to `imap.gmail.com` over SSL using an app password, opens `[Gmail]/All Mail`, and fetches the last *M* days of messages (default 3). Captures subject, sender, date, a plain-text snippet, and the `Message-ID` so the digest can link back to each thread in Mail.app (`message://…`) or Gmail on the web.
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

## Configuration

Edit `~/.config/daily-digest/config.env`:

| Variable | Default | Meaning |
|---|---|---|
| `DIGEST_GMAIL_ADDRESS` | — | Gmail account to read (and send from) |
| `DIGEST_RECIPIENT` | same as above | Where the digest is emailed |
| `DIGEST_CAL_DAYS` | `14` | Days of calendar to include |
| `DIGEST_EMAIL_DAYS` | `3` | Days of Gmail to scan |
| `DIGEST_KEEP_DAYS` | `1` | Days of past digests to keep in your inbox — see below |
| `ANTHROPIC_MODEL` | `claude-opus-4-7` | Model. `claude-sonnet-4-6` is cheaper and fast enough. |

Changes take effect on the next run — no reinstall.

### Auto-deleting old digests

Each run begins by moving *its own* prior digest emails to Gmail Trash, so the inbox doesn't fill up. Matching is strict: messages only get trashed if they're **from your own address** AND have a subject starting with `Daily digest —`. Nothing else is touched. Gmail empties Trash automatically after 30 days, so old digests are recoverable for a month.

- `DIGEST_KEEP_DAYS=1` (default) — keep only today's digest
- `DIGEST_KEEP_DAYS=7` — keep a week's worth
- `DIGEST_KEEP_DAYS=0` (or any non-positive value) — disable cleanup

### Don't run multiple instances

Gmail's IMAP server allows roughly 15 concurrent connections per account, and overlapping runs can also corrupt `yesterday.html`. The script takes an `fcntl` file lock on `~/.local/share/daily-digest/daily-digest.lock` at startup and exits immediately with code `4` if another copy is already running. So `launchctl start com.user.dailydigest` while a dry-run is still in progress is safe — the second invocation just quits. If you see `[ALERT] Too many simultaneous connections. (Failure)` during install, wait a minute for Gmail to time out the stale connections and try again.

## Waking the Mac at 02:00

`launchd` will not wake a sleeping Mac on its own. If you want the run to happen exactly at 02:00, schedule a wake:

```bash
sudo pmset repeat wake MTWRFSU 01:59:00
```

Check with `pmset -g sched`; cancel with `sudo pmset repeat cancel`. If you skip this, the job simply runs on next wake, which is usually fine.

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

```bash
cd ~/daily-digest
./uninstall.sh           # remove agent, config, Keychain items
./uninstall.sh --purge   # also delete state (archived digests, yesterday.html)
```

The repo files themselves are left alone; delete the directory manually if you want it gone.

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

One Claude API call per day. Inputs are typically 20–60k tokens (mostly email bodies); outputs are a few hundred. Pennies a day on Opus, fractions of a penny on Sonnet.

## License

MIT — see [LICENSE](./LICENSE).
