# daily-digest

A macOS launchd job that wakes up each morning, reads your Calendar and recent Gmail, asks Claude to produce a tidy HTML digest (flagging events mentioned in email that aren't yet in your calendar), and emails it to you.

- **Calendar source:** local macOS Calendar via AppleScript — picks up iCloud, Google, Exchange, and subscribed feeds in one pass. No OAuth.
- **Email source:** Gmail over IMAP with an app-specific password.
- **Analysis:** Anthropic Claude API.
- **Delivery:** SMTP back to your Gmail (or any address).
- **Continuity:** yesterday's digest is fed into today's run so wording stays stable day to day.

## Install

```bash
git clone https://github.com/YOU/daily-digest.git ~/daily-digest
cd ~/daily-digest
./install.sh
```

The installer will:

1. Check for `python3` and install the `anthropic` package.
2. Prompt for your Gmail address, app password, and Anthropic API key.
3. Validate each one (IMAP login, API ping).
4. Store the secrets in your login Keychain.
5. Write a small config file at `~/.config/daily-digest/config.env`.
6. Install a launchd agent that runs daily at 02:00.
7. Kick off a test run so macOS prompts for Calendar permissions.

You'll get a handful of "osascript wants to access Calendar" prompts the first time — accept them. Then open **System Settings → Privacy & Security → Full Disk Access** and tick `/bin/bash` so launchd can read unattended at 02:00. The installer will remind you.

## Prerequisites

- macOS (tested 13+)
- Python 3.9+
- A Gmail account with 2FA enabled — [generate an app password](https://myaccount.google.com/apppasswords) before running the installer
- An [Anthropic API key](https://console.anthropic.com/)

## Run it manually

Useful for testing or a fresh digest on demand:

```bash
launchctl start com.user.dailydigest   # fires the scheduled job now
```

or

```bash
~/daily-digest/run.sh   # runs the script directly; same thing
```

Dry-run (builds the digest, writes it to disk, **doesn't send the email**):

```bash
~/daily-digest/run.sh --dry-run
```

Output goes to `~/.local/share/daily-digest/preview.html` — open it in a browser.

## Configuration

Edit `~/.config/daily-digest/config.env` to tweak:

| Variable | Default | Meaning |
|---|---|---|
| `DIGEST_GMAIL_ADDRESS` | — | Gmail account to read (and send from, unless `DIGEST_RECIPIENT` is set) |
| `DIGEST_RECIPIENT` | same as above | Where the digest gets emailed |
| `DIGEST_CAL_DAYS` | `14` | Calendar window |
| `DIGEST_EMAIL_DAYS` | `3` | How far back to scan Gmail |
| `ANTHROPIC_MODEL` | `claude-opus-4-7` | Model to use. `claude-sonnet-4-6` is cheaper/faster. |

Changes take effect on the next run — no reinstall needed.

## Wake the Mac at 02:00

`launchd` **will not wake a sleeping Mac**. If you want the Mac to wake itself, schedule it separately:

```bash
sudo pmset repeat wake MTWRFSU 01:59:00
```

Check with `pmset -g sched`, cancel with `sudo pmset repeat cancel`.

If you skip this, the job just fires on next wake, which is usually fine.

## Uninstall

```bash
cd ~/daily-digest
./uninstall.sh
```

Removes the launchd agent, the config file, and the Keychain entries. Your local state (`~/.local/share/daily-digest/`) is kept unless you pass `--purge`.

## How it stays sensible day-to-day

Each run writes today's rendered digest to `~/.local/share/daily-digest/yesterday.html`. The next run feeds that file back to Claude as "yesterday's digest" and the system prompt tells it to keep wording close to yesterday's when the facts haven't changed. This stops the digest from re-phrasing the same events every morning, which otherwise makes it hard to tell what's actually new.

## Files

```
daily-digest/
├── daily_digest.py            # main script
├── run.sh                     # wrapper that sources config + Keychain secrets
├── install.sh                 # interactive installer
├── uninstall.sh               # cleanup
├── requirements.txt           # Python deps
└── com.user.dailydigest.plist.template   # launchd template
```

Runtime files written outside the repo:

```
~/.config/daily-digest/config.env           # non-secret config
~/Library/LaunchAgents/com.user.dailydigest.plist
~/.local/share/daily-digest/                # state (yesterday.html, archives)
~/Library/Logs/daily-digest.log             # main log
```

Secrets live only in the login Keychain, never on disk.

## Troubleshooting

**`osascript is not allowed to send Apple events to Calendar`**  → System Settings → Privacy & Security → Automation. Tick Calendar under bash / osascript.

**IMAP login fails** → verify the app password:
```bash
security find-generic-password -s daily-digest-gmail -w
```
If it doesn't match what's in your Google account, re-run `install.sh`.

**Runs manually, silently fails under launchd** → 99% of the time this is Full Disk Access. Check `~/Library/Logs/daily-digest.stderr.log`.

**Empty digest** → check `~/Library/Logs/daily-digest.log`. Usually a rate limit or a key that's been rotated.

## Cost

One Claude API call per day. Input is typically 20–60k tokens; output is a few hundred. Pennies a day on Opus, fractions of a penny on Sonnet.

## License

MIT — see [LICENSE](./LICENSE).
