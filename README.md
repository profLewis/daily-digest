# daily-digest

> ### 🖥 macOS only
> This repo is written **for macOS** (tested on macOS 13+). It uses AppleScript,
> `launchd`, the macOS Keychain, and the Apple Mail/Messages local stores —
> none of which exist on Linux or Windows. A Linux port would need:
> `launchd` → `systemd --user` timer or `cron`; Keychain → `secret-tool` or
> `pass`; AppleScript calendar → CalDAV/ICS or a Google Calendar API client;
> Apple Mail → drop entirely or replace with IMAP accounts; iMessage →
> drop entirely. The Gmail IMAP, Ollama, and SMTP paths are already portable.
> If you do port it, please open an issue so others can find your fork.

> # 🛑 Read this before you run it
>
> **This is an exploratory / educational project.** Its purpose is to wire
> together a local LLM (via Ollama), Gmail (IMAP + SMTP), the macOS
> Calendar, Apple Mail, and iMessage into a single daily briefing. It is
> **not** a polished product, it has **not** been security-audited by
> anyone other than its author, and it **will change without notice**.
>
> Before you run a single line of it:
>
> 1. **Verify the source.** Only run copies cloned directly from
>    `https://github.com/profLewis/daily-digest`. A fork or downloaded zip
>    from anywhere else may contain modifications you haven't inspected.
>    Check `git remote -v` after cloning.
> 2. **Read the code.** It is small on purpose — about 700 lines of Python
>    and three short shell scripts. Read `daily_digest.py`, `install.sh`,
>    `run.sh`, and `uninstall.sh` end-to-end before running `install.sh`.
> 3. **Know how to remove it.** See [Uninstall](#uninstall) below — it is
>    a single command.

A macOS background job that emails you a tidy HTML summary of your day
each morning.

Every night at 02:00 it wakes up, reads your Calendar and the last few
days of Gmail, asks a **local Ollama model** to turn them into a clean
digest, and sends the result to your inbox. Events that appear in email
but aren't yet on your calendar are surfaced separately so nothing slips
through.

**Need to get rid of it right now?** Run `~/daily-digest/uninstall.sh`
(or `./uninstall.sh --purge` to also wipe archived digests). Full details
in the [Uninstall](#uninstall) section.

## What leaves your machine

With the default configuration (Ollama at `http://localhost:11434`), the
only outbound network traffic is to Google:

* **`imap.gmail.com:993`** and **`smtp.gmail.com:465`** — reading and
  sending your own Gmail. Google already has this data.

The LLM call goes to **`localhost:11434`**, which is the Ollama daemon
on the same Mac. Nothing in your calendar, email, or messages is sent
to any third party.

If you point `OLLAMA_URL` at a different host on your LAN (e.g. a
beefier Mac in the same house), model inputs travel across your local
network in plain HTTP. Don't do that over the open internet without
tunnelling.

## What it does, step by step

1. **Read the calendar.** An AppleScript pulls every event from the local
   macOS Calendar app for the next *N* days (default 14). Because it reads
   the unified store, iCloud, Google, Exchange, and subscribed calendars
   all come through in one pass — no per-provider OAuth.
2. **Read recent Gmail.** Connects to `imap.gmail.com` over SSL using an
   app password, opens `[Gmail]/All Mail`, and fetches the last *M* days
   of messages (default 3). Captures subject, sender, date, a plain-text
   snippet, and the `Message-ID` so the digest can link back to each
   thread in Mail.app (`message://…`) or Gmail on the web.
   * **Optional — Mail.app accounts (opt-in).** Set `DIGEST_USE_MAIL_APP=true`
     to also pull recent inbox messages from every account Mail.app is
     logged into (iCloud, work, Exchange, any IMAP account). See
     [Mail.app](#optional-mailapp-other-email-accounts).
   * **Optional — iMessage/SMS (opt-in).** Set `DIGEST_USE_IMESSAGE=true`
     to scan recent iMessage and SMS conversations for event-like content.
     See the [iMessage opt-in](#optional-imessagesms-scanning) section below.
3. **Feed yesterday's digest back in.** The previous morning's rendered
   HTML is stored on disk and passed to the model as context. The system
   prompt tells it to keep wording close to yesterday's where the
   underlying facts haven't changed, so the digest reads as a stable
   daily document rather than a freshly-paraphrased one each morning.
4. **Ask the local model to produce the digest.** One HTTP POST to
   `OLLAMA_URL/api/chat`. The model groups events by day, then scans
   emails for event-like content (invitations, bookings, flights,
   deliveries, deadlines) and flags anything that isn't already on the
   calendar under "Possible events from email not yet in calendar".
   Ambiguous items get a `(verify)` tag rather than being invented.
5. **Render and send.** The response is an HTML fragment with clickable
   links to each event (`calshow:` URLs open Calendar on that day;
   `message://` URLs open the thread in Mail). It goes out via Gmail SMTP
   (SSL, port 465) to whichever address you configured.
6. **Persist state.** Today's digest is written to
   `~/.local/share/daily-digest/yesterday.html` (for tomorrow's
   continuity) and archived as `digest-YYYY-MM-DD.html`.

All of this is orchestrated by a `launchd` agent, so it survives reboots
and doesn't need you to be logged into a terminal.

## Prerequisites

* macOS 13 or later
* Python 3.9+ (stdlib only — no pip install needed)
* A Gmail account with 2FA on — [generate an app password](https://myaccount.google.com/apppasswords)
* [Homebrew](https://brew.sh/) — used by the installer to install and
  auto-start Ollama. If Homebrew isn't present the installer will stop
  and ask you to install it (or to install Ollama manually from
  <https://ollama.com/>).
* **Ollama is handled automatically** by `install.sh`: if the `ollama`
  binary isn't on your PATH it runs `brew install ollama`, starts the
  daemon via `brew services start ollama` (which also makes it survive
  reboots), and pulls whichever model tag you choose. `llama3.1:8b` is
  the default (4.7 GB on disk, ~8 GB RAM active). Larger models (70B)
  produce better digests but need 40+ GB of unified memory; smaller
  models (3B / phi3) are faster but miss more events.

## Install

Set `YOU` to your GitHub username first, then copy-paste:

```
export YOU=your-github-username
git clone https://github.com/${YOU}/daily-digest.git ~/daily-digest
cd ~/daily-digest
./install.sh
```

The installer walks you through everything:

1. Checks for `python3`, `osascript`, `launchctl`.
2. Prompts for your Gmail address, app password, Ollama URL, and model tag.
3. Validates the IMAP login.
4. **Handles Ollama end-to-end**: if the daemon isn't reachable at your
   chosen `OLLAMA_URL` (and the URL is local), it:
   - checks for Homebrew and dies with a pointer to <https://brew.sh> if
     it's missing;
   - runs `brew install ollama` if the binary isn't already on PATH;
   - starts the daemon via `brew services start ollama` (which creates
     a user LaunchAgent so it auto-starts on every login/reboot);
   - waits up to 30s for the daemon to come up;
   - runs `ollama pull $OLLAMA_MODEL` if that tag isn't already present
     (this can take minutes — models are multiple GB);
   - smoke-tests a tiny chat completion so you know generation works
     before storing anything.
5. Writes the Gmail app password to your login Keychain (never to disk
   in plaintext).
6. Writes non-secret config to `~/.config/daily-digest/config.env`.
7. Installs a `launchd` agent at `~/Library/LaunchAgents/com.user.dailydigest.plist`
   set to fire at 02:00 daily (you can pick a different time).
8. Runs a dry-run so macOS raises its Calendar permission prompts —
   accept them.

After install, open **System Settings → Privacy & Security → Full Disk
Access** and tick `/bin/bash`. Without this, the 02:00 run can't read
Calendar when you're not actively logged in. The installer reminds you.

## Running it manually

```
launchctl start com.user.dailydigest    # fire the scheduled job now
~/daily-digest/run.sh                   # equivalent: runs directly
~/daily-digest/run.sh --dry-run         # build the digest, save preview, don't email
```

A dry run writes `~/.local/share/daily-digest/preview.html`. Open it in
a browser to see what would have been sent.

**Try an optional source for one run only** (without changing config):

```
DIGEST_USE_IMESSAGE=true ~/daily-digest/run.sh --dry-run
DIGEST_USE_MAIL_APP=true ~/daily-digest/run.sh --dry-run
```

That way you can see what iMessage or Mail.app content would appear in
the digest before committing to having it in every run.

## Configuration

Edit `~/.config/daily-digest/config.env`:

| Variable | Default | Meaning |
|---|---|---|
| `DIGEST_GMAIL_ADDRESS` | — | Gmail account to read (and send from) |
| `DIGEST_RECIPIENT` | same as above | Where the digest is emailed |
| `DIGEST_CAL_DAYS` | `14` | Days of calendar to include |
| `DIGEST_EMAIL_DAYS` | `3` | Days of Gmail to scan |
| `DIGEST_KEEP_DAYS` | `1` | Days of past digests to keep in your inbox |
| `DIGEST_USE_MAIL_APP` | `false` | Also scan Mail.app inboxes (all accounts Mail is logged into) |
| `DIGEST_USE_IMESSAGE` | `false` | Also scan iMessage/SMS — see [iMessage opt-in](#optional-imessagesms-scanning) |
| `DIGEST_IMESSAGE_DAYS` | `3` | How far back to scan iMessage/SMS |
| `OLLAMA_URL` | `http://localhost:11434` | Where the Ollama daemon is listening |
| `OLLAMA_MODEL` | `llama3.1:8b` | Ollama model tag. Try `llama3.1:70b` if you have the RAM. |
| `OLLAMA_TIMEOUT` | `600` | Seconds to wait for a completion |

Changes take effect on the next run — no reinstall.

### Optional: Mail.app (other email accounts)

By default the tool only reads Gmail over IMAP. If you use **Apple Mail**
with additional accounts (iCloud, work Exchange, extra IMAP accounts),
set `DIGEST_USE_MAIL_APP=true` and the script will also read the Inbox
of every account Mail.app is logged into — no per-account credentials
needed. First run triggers an "allow Automation" prompt for Mail. On
large mailboxes this can add 30–120 seconds to each run.

### Optional: iMessage/SMS scanning

Set `DIGEST_USE_IMESSAGE=true` to have the script read the last
`DIGEST_IMESSAGE_DAYS` days (default 3) of messages from
`~/Library/Messages/chat.db` — the same store Messages.app uses. The
local model then surfaces anything that looks like a calendar item
(appointments, bookings, RSVPs, flights) that isn't already on your
calendar. Nothing is written back to Messages; the script is read-only
against `chat.db`.

It's still a judgement call: other people message you in confidence,
and enabling this means their words get read by the tool (and, if
`OLLAMA_URL` is remote, travel across your LAN to wherever Ollama is
running). With the default localhost URL, nothing leaves the machine.

Requirements:

* **Full Disk Access** for `/bin/bash` (and for your terminal, if you
  want to test it manually). System Settings → Privacy & Security →
  Full Disk Access.
* This is off by default — the installer asks you explicitly.

**Enable for the scheduled 02:00 run (edit config):**
```
$EDITOR ~/.config/daily-digest/config.env
# set DIGEST_USE_IMESSAGE="true", save
```

Config changes take effect on the next run — no reinstall needed.

**Try it for a single manual run, without changing the config:**
```
DIGEST_USE_IMESSAGE=true ~/daily-digest/run.sh --dry-run
open ~/.local/share/daily-digest/preview.html
```

**Disable it later:**
```
$EDITOR ~/.config/daily-digest/config.env    # set DIGEST_USE_IMESSAGE="false"
```

### Auto-deleting old digests

Each run begins by moving *its own* prior digest emails to Gmail Trash,
so the inbox doesn't fill up. Matching is strict: messages only get
trashed if they're **from your own address** AND have a subject starting
with `Daily digest —`. Nothing else is touched. Gmail empties Trash
automatically after 30 days, so old digests are recoverable for a month.

* `DIGEST_KEEP_DAYS=1` (default) — keep only today's digest
* `DIGEST_KEEP_DAYS=7` — keep a week's worth
* `DIGEST_KEEP_DAYS=0` (or any non-positive value) — disable cleanup

### Don't run multiple instances

Gmail's IMAP server allows roughly 15 concurrent connections per account,
and overlapping runs can also corrupt `yesterday.html`. The script takes
an `fcntl` file lock on `~/.local/share/daily-digest/daily-digest.lock`
at startup and exits immediately with code `4` if another copy is
already running. So `launchctl start com.user.dailydigest` while a
dry-run is still in progress is safe — the second invocation just
quits. If you see `[ALERT] Too many simultaneous connections. (Failure)`
during install, wait a minute for Gmail to time out the stale
connections and try again.

## Making sure Ollama is running at run time

The default installer handles this for you: `install.sh` runs
`brew services start ollama`, which registers a user LaunchAgent at
`~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`. That agent restarts
the daemon on every login and reboot, so the digest job at 02:00 will
find Ollama listening on `localhost:11434`. Verify with:

```
brew services list
```

If you see `ollama  started  <you>  ~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`
you're all set.

**If you installed Ollama some other way** (e.g. the `Ollama.app` from
<https://ollama.com/>, not Homebrew), the installer falls back to a
best-effort `nohup ollama serve &` for the current session only — it
won't persist. You have two options:

1. **Use the GUI app.** Open `Ollama.app` once and tick "Open at Login"
   in the menu-bar icon's settings. The daemon then starts with your
   account.
2. **Run it as its own LaunchAgent.** Example:

   ```
   cat > ~/Library/LaunchAgents/com.user.ollama.plist <<'EOF'
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key>          <string>com.user.ollama</string>
     <key>ProgramArguments</key>
     <array>
       <string>/opt/homebrew/bin/ollama</string>
       <string>serve</string>
     </array>
     <key>RunAtLoad</key>      <true/>
     <key>KeepAlive</key>      <true/>
     <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/ollama.log</string>
     <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/ollama.err.log</string>
   </dict>
   </plist>
   EOF
   launchctl load ~/Library/LaunchAgents/com.user.ollama.plist
   ```

   Fix the path to `ollama` — it may be `/usr/local/bin/ollama` on Intel
   Homebrew; run `which ollama` to check.

If Ollama isn't running when the digest job fires, the run aborts with a
clear `ollama request failed` message in `~/Library/Logs/daily-digest.log`.

## Checking what's already scheduled on this Mac

Before (or after) installing, it's worth knowing what else is running on
a timer so the 02:00 digest doesn't land on top of a backup or another
cron. Useful commands:

**`launchd` (user-scope agents — the mechanism this tool uses):**

```
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
```
ls /Library/LaunchDaemons/ /Library/LaunchAgents/ 2>/dev/null
```

**`cron` (less common on modern macOS but still honoured):**
```
crontab -l           # your user's crontab
sudo crontab -l      # root's crontab
```

**`pmset` (system-wide wake / sleep schedule):**
```
pmset -g sched             # show current repeat wake/sleep/shutdown
pmset -g                   # full power-management settings
sudo pmset repeat cancel   # remove any repeat schedule
```

The installer runs a summary of the first three for you automatically
and flags anything scheduled within ±15 minutes of your chosen time.

## Choosing the run time

The installer asks what time you'd like the digest to fire (default
02:00) and shows any launchd agents / crontab entries / pmset wake
schedules already set on the account so you can avoid a clash. If you
pick a time that falls within 15 minutes of another scheduled job it
warns you **and automatically suggests a clash-free slot** — press Enter
to accept, or say No to keep your original pick (at your own risk).

Re-running `install.sh` keeps the previous time as the default rather
than resetting you to 02:00.

To change the time later: re-run `./install.sh` — it reads the existing
plist, shows your current time as the default, and you just type the new
one.

## Waking the Mac

`launchd` will not wake a sleeping Mac on its own. If you want the run
to happen exactly at your scheduled time, set a one-minute-earlier wake.
For the default 02:00 schedule:

```
sudo pmset repeat wake MTWRFSU 01:59:00
```

For any other schedule, use `HH:MM:00` one minute before your launchd
time. The installer prints the exact command at the end.

Check with `pmset -g sched`; cancel with `sudo pmset repeat cancel`.
`pmset` only stores one repeat schedule, so setting this overwrites any
existing one. If you skip it entirely, the job runs on next wake —
usually fine.

## How continuity works

Each successful run writes its HTML to
`~/.local/share/daily-digest/yesterday.html`. The next run feeds that
file back to the model as "yesterday's digest", and the system prompt
instructs it to preserve wording where facts are unchanged. This means
a standing meeting doesn't get re-paraphrased every morning — stable
wording makes it obvious at a glance what's actually new.

Note: small local models are noticeably worse than frontier cloud models
at holding to this instruction. Expect more day-to-day wording jitter
than you'd see with a larger model. Raising the model size (e.g. from
8B to 70B) improves stability markedly, at the cost of memory and time.

## Files

In the repo:

```
daily-digest-repo/
├── daily_digest.py                        # main script
├── run.sh                                 # wrapper: sources config + Keychain secret
├── install.sh                             # interactive installer
├── uninstall.sh                           # cleanup
├── requirements.txt                       # (empty — stdlib only)
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

The only secret in the login Keychain:
```
daily-digest-gmail       # Gmail app password
```

## Auditing the code

No library can *prove* this or any code only does what it claims — by
Rice's theorem, that's undecidable. What automated tools **can** do is
flag known bad patterns. Combine a few of them and you cover most of
what matters for a small repo like this:

| Tool | What it checks | Install |
|---|---|---|
| [`bandit`](https://bandit.readthedocs.io/) | Python security anti-patterns: `shell=True`, hardcoded passwords, insecure SSL, `eval`, etc. | `pipx install bandit` |
| [`semgrep`](https://semgrep.dev/) | Configurable pattern matching; rich community rule-sets for secrets exfiltration, dodgy imports. | `pipx install semgrep` |
| [`shellcheck`](https://www.shellcheck.net/) | Common shell-script mistakes and injection risks. | `brew install shellcheck` |

Run all three from the repo root:
```
bandit -r daily_digest.py
semgrep --config=auto .
shellcheck install.sh run.sh uninstall.sh
```

### The irreducible manual check

Tools won't catch a deliberately malicious but syntactically innocent
line. Before you run `install.sh`, eyeball these specifically:

1. **Every outbound network call.** In `daily_digest.py` they're easy to
   enumerate — look for `imaplib.IMAP4_SSL`, `smtplib.SMTP_SSL`, and
   `urllib.request.urlopen(...)` to `OLLAMA_URL`. Those are the only
   three destinations. Confirm no `requests` or `http.client` imports
   have appeared pointing anywhere else.
2. **Every `subprocess` call.** There is exactly one pattern:
   `subprocess.run(["osascript", ...])` used twice — once for Calendar,
   once for Mail.app. Both take no user-controlled input and run
   hard-coded AppleScripts. Confirm no `shell=True` anywhere.
3. **Every write to disk.** All writes go through `CFG["state_dir"]`
   and `CFG["log_file"]`, both of which resolve to
   `~/.local/share/daily-digest/` and `~/Library/Logs/` respectively.
   Confirm no other `open(..., "w")` or `Path.write_text` exists.
4. **The three shell scripts.** `install.sh` does Keychain writes,
   config writes, and a `launchctl load`. `run.sh` reads Keychain and
   execs Python. `uninstall.sh` reverses it. None should download
   anything from the internet.

A one-liner that surfaces the network, subprocess, and filesystem
surface area of the Python for a quick visual check:
```
grep -nE 'imaplib|smtplib|urlopen|subprocess|socket|requests|http\.|open\(|write_text|mkdir' daily_digest.py
```
If the output looks longer or stranger than you'd expect, stop and
investigate.

## Uninstall

**Quick removal** — stops the scheduled run, deletes config, and removes
the Keychain secret:
```
~/daily-digest/uninstall.sh
```

**Full removal** — also wipes archived digests and `yesterday.html`:
```
~/daily-digest/uninstall.sh --purge
```

**Then delete the repo itself** (the uninstaller doesn't touch it, in
case you want to reinstall):
```
rm -rf ~/daily-digest
```

### What exactly gets removed

The uninstaller cleans up:

| Target | Removed? | Notes |
|---|---|---|
| `~/Library/LaunchAgents/com.user.dailydigest.plist` | ✅ always | Stops the scheduled run |
| Any running `daily_digest.py` process | ✅ always | `pkill`'d so nothing is mid-write when we remove state |
| `~/.config/daily-digest/` | ✅ always | Config file |
| `daily-digest.lock` in state dir | ✅ always | Instance lockfile |
| Keychain item `daily-digest-gmail` | ✅ always | Your Gmail app password |
| Keychain item `daily-digest-anthropic` | ✅ if present | Obsolete; cleaned up for anyone upgrading from an older version |
| `~/.local/share/daily-digest/` | Only with `--purge` | Archived digests, `yesterday.html`, `preview.html` |
| `~/Library/Logs/daily-digest*.log` | Only with `--purge` | Main log + launchd stdout/stderr logs |
| `~/daily-digest/` (the cloned repo) | ❌ never | `rm -rf ~/daily-digest` removes it |

### What the uninstaller CANNOT remove for you — do these manually

macOS and third parties own these; no script can touch them. The
uninstaller prints this list at the end of its run too, so you won't
miss it:

* **macOS Automation permissions** → System Settings → Privacy &
  Security → Automation → remove any `Calendar` or `Mail` entries for
  `bash`, `osascript`, or your terminal.
* **Full Disk Access** → System Settings → Privacy & Security → Full
  Disk Access → remove `/bin/bash` if you added it (needed for the
  02:00 launchd run), and your terminal if you added it (needed to
  test iMessage reading manually).
* **Scheduled wake** → if you ran `sudo pmset repeat wake …` per the
  README, reverse it with `sudo pmset repeat cancel`.
* **Gmail app password** — the Keychain copy is gone, but the password
  itself still exists on Google's side. Revoke at
  <https://myaccount.google.com/apppasswords>.
* **Ollama** — left in place. To remove: quit the app and drag it to
  Trash; `rm -rf ~/.ollama` removes the pulled model weights (often
  many GB).
* **Delivered digest emails** — any that are still in your Gmail stay
  there. Use Gmail's search for `from:me subject:"Daily digest"` to find
  and delete them.

### What the uninstaller does NOT touch (intentionally)

* **Your mail, calendars, or messages.** The tool only ever reads them;
  uninstalling has nothing to reverse.

## Troubleshooting

**`osascript is not allowed to send Apple events to Calendar`** →
System Settings → Privacy & Security → Automation. Tick Calendar under
bash/osascript.

**IMAP login fails** → verify the app password is still in the Keychain:
```
security find-generic-password -s daily-digest-gmail -w
```
If it doesn't match your current Google app password, re-run
`install.sh`.

**`ollama request failed: …`** → the Ollama daemon isn't reachable.
Check it's running (`pgrep -lf 'ollama serve'` or open Ollama.app),
check the URL in config.env matches where it's listening (`curl
$OLLAMA_URL/api/tags` should return JSON), and check the model tag is
pulled (`ollama list`). Cold-start on a large model can take minutes;
raise `OLLAMA_TIMEOUT` if needed.

**Runs fine manually but silently fails at 02:00 under launchd** →
almost always one of: Full Disk Access not granted to `/bin/bash`,
or Ollama not running because you haven't set it to auto-start. Check
`~/Library/Logs/daily-digest.stderr.log`.

**Empty digest** → check `~/Library/Logs/daily-digest.log`. Usually a
malformed model response (some small models generate preambles before
the HTML, which gets stripped down to nothing). Try a bigger model,
or lower `temperature` further in `build_digest`.

**Digest is sloppier than you'd expect** → that's the local-model
quality floor. See the known-issues list below.

**Want to see what the model is being sent** → run with `--dry-run` and
inspect `~/.local/share/daily-digest/preview.html`; the log file shows
the event and email counts that were included.

## Resource use

No per-run monetary cost with a local model — but CPU / GPU / RAM time
is real. Rough numbers on Apple Silicon (M1/M2/M3 Pro class):

* `llama3.1:8b` — 10–40 seconds per run, ~6 GB RAM active during
  generation.
* `llama3.1:70b` — 1–4 minutes per run, ~40 GB unified memory, much
  better digest quality.
* `phi3:mini` — 3–10 seconds per run, ~3 GB RAM, visibly rougher output.

Each API-touching step writes one line to
`~/Library/Logs/daily-digest.log`:

| Log line | What it means |
|---|---|
| `gmail: IMAP login as you@… (last N days)` | IMAP connection opens |
| `gmail: N messages since …, fetching last M` | IMAP SEARCH + FETCH count |
| `gmail: parsed M messages` | IMAP fetch complete |
| `ollama: calling MODEL at URL (timeout=…s)` | Ollama chat request outbound |
| `ollama: usage model=… prompt_eval=… eval=… elapsed=…s` | Ollama response, token counts + wall time |
| `smtp: sending digest to … (N bytes)` | SMTP send opens |
| `smtp: sent` | SMTP send complete |
| `trashing digests from … before …` / `trashed N old digest(s)` | IMAP cleanup pass |
| `run summary: calendar_events=… emails_scanned=… model=… prompt_eval=… eval=… elapsed=… emailed=… exit=…` | End-of-run one-liner with totals; emitted whether the run succeeds or fails |

To monitor live:
```
tail -f ~/Library/Logs/daily-digest.log
```

A week's worth of timings at a glance:
```
grep 'run summary' ~/Library/Logs/daily-digest.log | tail -7
```

## Known issues with the local-model setup

Moving from a frontier cloud model to a local 8B-class model is the
right trade if privacy matters, but the behavioural gap is real. Worth
knowing before you rely on the digest:

1. **Event extraction is lossier.** Small models miss more ambiguous
   calendar-worthy emails than Claude or GPT-4 do. Structured invites
   (Calendly, Eventbrite, airline confirmations) are usually fine;
   free-text ("lunch Thursday?") is where you'll notice.
2. **HTML-format discipline is weaker.** The system prompt demands very
   specific markup (coloured bullets, `title_html` pasted verbatim,
   `subject_html` unmodified). Small models sometimes paraphrase URLs,
   drop the colour spans, or invent their own anchors. Output may need
   a manual glance before you trust it.
3. **Day-to-day wording is less stable.** The "keep yesterday's wording"
   instruction lands harder on larger models. Expect more cosmetic
   jitter from run to run.
4. **Context length matters.** Calendar + 3 days of Gmail + 3 days of
   iMessage + yesterday's digest easily reaches 20–60k tokens.
   `llama3.1` handles 128k; many other models quietly truncate at 8k or
   32k. Check the model card for the model you pull.
5. **Ollama must be running when the job fires.** `install.sh` arranges
   this via `brew services start ollama`, which persists across reboots.
   If you installed Ollama some other way, see [Making sure Ollama is
   running at run time](#making-sure-ollama-is-running-at-run-time). If
   the daemon is stopped when 02:00 comes, the run aborts with a clear
   error; no digest is sent.
6. **No hard memory cap.** A misbehaving model or a very long input can
   push RAM hard. On a machine with 16 GB, `llama3.1:70b` will swap
   painfully; stick to the 8B tier unless you have 32+ GB.
7. **`ollama serve` binds to 127.0.0.1 by default** — it's not exposed
   on your LAN. Keep it that way unless you know what you're doing;
   `OLLAMA_HOST=0.0.0.0` would put your local model on the local
   network with no auth.

None of these are showstoppers — the digest is still useful — but set
expectations accordingly.

## License

MIT — see [LICENSE](LICENSE).
