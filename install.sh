#!/bin/bash
# install.sh — one-shot interactive installer for daily-digest.
# Safe to re-run: each step checks for existing state before acting.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/daily-digest"
CONFIG_FILE="$CONFIG_DIR/config.env"
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/daily-digest"
LAUNCHD_LABEL="com.user.dailydigest"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
LOG_DIR="$HOME/Library/Logs"

# Colours (only if stdout is a tty)
if [[ -t 1 ]]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
    B=""; G=""; Y=""; R=""; N=""
fi

say()  { echo "${B}==>${N} $*"; }
ok()   { echo "    ${G}✓${N} $*"; }
warn() { echo "    ${Y}!${N} $*"; }
die()  { echo "${R}ERROR:${N} $*" >&2; exit 1; }


# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
say "Checking prerequisites"

[[ "$(uname)" == "Darwin" ]] || die "This installer is macOS-only."
command -v security   >/dev/null || die "macOS 'security' command missing (very weird)."
command -v osascript  >/dev/null || die "macOS 'osascript' command missing."
command -v launchctl  >/dev/null || die "macOS 'launchctl' command missing."

PYTHON="${DIGEST_PYTHON:-$(command -v python3 || true)}"
[[ -n "$PYTHON" ]] || die "python3 not found. Install Python 3.9+ first (brew install python)."
PY_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "python3 is $PYTHON ($PY_VER)"

"$PYTHON" -c "import anthropic" 2>/dev/null && ok "anthropic package already installed" || {
    say "Installing anthropic package"
    "$PYTHON" -m pip install --user --quiet anthropic || die "pip install failed."
    ok "installed"
}


# ---------------------------------------------------------------------------
# 2. Gather credentials
# ---------------------------------------------------------------------------
say "Credentials"

# Gmail address
default_gmail="$(security find-generic-password -s daily-digest-gmail -g 2>&1 | awk -F'"' '/"acct"/ {print $4}' || true)"
read -r -p "  Gmail address${default_gmail:+ [$default_gmail]}: " GMAIL
GMAIL="${GMAIL:-$default_gmail}"
[[ "$GMAIL" =~ ^[^@]+@[^@]+\.[^@]+$ ]] || die "That doesn't look like an email address."

# Gmail app password
existing_pw="$(security find-generic-password -a "$GMAIL" -s daily-digest-gmail -w 2>/dev/null || true)"
if [[ -n "$existing_pw" ]]; then
    read -r -p "  Gmail app password already in Keychain — reuse it? [Y/n] " reuse
    if [[ "$reuse" =~ ^[Nn] ]]; then
        existing_pw=""
    fi
fi
if [[ -z "$existing_pw" ]]; then
    echo "  Generate one at https://myaccount.google.com/apppasswords (requires 2FA)."
    read -r -s -p "  Gmail app password (hidden): " APP_PW; echo
    [[ -n "$APP_PW" ]] || die "App password can't be empty."
    # Strip any spaces Google inserts for readability:
    APP_PW="${APP_PW// /}"
else
    APP_PW="$existing_pw"
fi

# Anthropic API key
existing_key="$(security find-generic-password -s daily-digest-anthropic -w 2>/dev/null || true)"
if [[ -n "$existing_key" ]]; then
    read -r -p "  Anthropic API key already in Keychain — reuse it? [Y/n] " reuse
    if [[ "$reuse" =~ ^[Nn] ]]; then
        existing_key=""
    fi
fi
if [[ -z "$existing_key" ]]; then
    read -r -s -p "  Anthropic API key (hidden): " API_KEY; echo
    [[ "$API_KEY" =~ ^sk-ant- ]] || warn "Key doesn't start with 'sk-ant-' — double-check it."
else
    API_KEY="$existing_key"
fi


# ---------------------------------------------------------------------------
# 3. Validate
# ---------------------------------------------------------------------------
say "Validating credentials"

# IMAP login
"$PYTHON" - <<PY || die "Gmail IMAP login failed — check address / app password."
import imaplib, sys
try:
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as M:
        M.login("$GMAIL", "$APP_PW")
        M.select('"[Gmail]/All Mail"', readonly=True)
except Exception as e:
    sys.stderr.write(str(e) + "\n"); sys.exit(1)
PY
ok "Gmail IMAP login works"

# Anthropic ping
"$PYTHON" - <<PY || die "Anthropic API test failed — check the key."
import os, sys
os.environ["ANTHROPIC_API_KEY"] = "$API_KEY"
try:
    import anthropic
    c = anthropic.Anthropic()
    r = c.messages.create(model="claude-haiku-4-5-20251001",
                          max_tokens=10,
                          messages=[{"role":"user","content":"ping"}])
except Exception as e:
    sys.stderr.write(str(e) + "\n"); sys.exit(1)
PY
ok "Anthropic API key works"


# ---------------------------------------------------------------------------
# 4. Store secrets in Keychain
# ---------------------------------------------------------------------------
say "Storing secrets in login Keychain"

# -U updates if it already exists
security add-generic-password -U \
    -a "$GMAIL" -s "daily-digest-gmail" \
    -T "" -w "$APP_PW" \
    -j "Gmail IMAP app password used by daily-digest"
ok "stored Gmail app password"

security add-generic-password -U \
    -a "api-key" -s "daily-digest-anthropic" \
    -T "" -w "$API_KEY" \
    -j "Anthropic API key used by daily-digest"
ok "stored Anthropic API key"


# ---------------------------------------------------------------------------
# 5. Optional data sources
# ---------------------------------------------------------------------------
say "Optional data sources"
echo ""
echo "  Beyond Gmail, daily-digest can also draw from:"
echo "    (a) every account Apple Mail is logged into"
echo "    (b) your iMessage and SMS conversations"
echo "  Both are OFF by default. You can change either answer later by"
echo "  editing ${B}$CONFIG_FILE${N}, or re-running this installer."
echo ""

# If re-running the installer, pick up the existing choice as the default
# so users aren't forced to re-decide every time.
prev_mail="false"; prev_imsg="false"
if [[ -f "$CONFIG_FILE" ]]; then
    prev_mail="$(awk -F'"' '/^DIGEST_USE_MAIL_APP=/{print $2}' "$CONFIG_FILE" 2>/dev/null)"
    prev_imsg="$(awk -F'"' '/^DIGEST_USE_IMESSAGE=/{print $2}' "$CONFIG_FILE" 2>/dev/null)"
    [[ -z "$prev_mail" ]] && prev_mail="false"
    [[ -z "$prev_imsg" ]] && prev_imsg="false"
fi
_default_label() { [[ "$1" == "true" ]] && echo "Y/n" || echo "y/N"; }
_parse_yn() {
    # $1 = user input, $2 = previous value ('true'/'false')
    local input="$1" prev="$2"
    if [[ -z "$input" ]]; then
        echo "$prev"
    elif [[ "$input" =~ ^[Yy] ]]; then
        echo "true"
    else
        echo "false"
    fi
}

# --- (a) Mail.app ---------------------------------------------------------
echo "  ${B}(a) Mail.app — other email accounts${N}  (current: $prev_mail)"
cat <<'EOF'
      What it does:
        - Reads the Inbox of every account Mail.app is logged into
          (iCloud, work Exchange, any extra IMAP/POP account).
        - Merges those messages with your Gmail IMAP fetch; duplicates
          (same Message-ID in both) are removed so Claude sees each
          email once.
        - Apple Mail holds the credentials; no per-account passwords
          are needed here.
      What it sends to Claude:
        - Subject, sender, date, Message-ID, first ~1000 chars of body
          for each recent inbox message across all accounts.
      Permission needed:
        - "Mail" under System Settings → Privacy & Security → Automation
          (macOS asks on first run; click Allow).
      Performance:
        - Adds ~30–120s per run depending on mailbox sizes. If you have
          huge inboxes, consider lowering DIGEST_EMAIL_DAYS.
      Turn off later:
        - Edit config.env → set DIGEST_USE_MAIL_APP="false".
EOF
read -r -p "      Enable Mail.app reading? [$(_default_label "$prev_mail")] " yn_mail
USE_MAIL_APP="$(_parse_yn "$yn_mail" "$prev_mail")"
echo "      ${G}→${N} DIGEST_USE_MAIL_APP=$USE_MAIL_APP"
echo ""

# --- (b) iMessage / SMS ---------------------------------------------------
echo "  ${B}(b) iMessage / SMS — PRIVACY-SENSITIVE${N}  (current: $prev_imsg)"
cat <<'EOF'
      What it does:
        - Reads ~/Library/Messages/chat.db (the same SQLite database
          Messages.app uses) in read-only mode. Nothing is written.
        - Pulls the text of every message sent/received in the last
          DIGEST_IMESSAGE_DAYS days (default: 3).
        - Passes those messages to Claude so event-like content
          (appointments, RSVPs, flights, deliveries) that arrived by
          message but isn't on your calendar still gets surfaced.
      What it sends to Claude:
        - Sender (phone/email handle), date, and message text for every
          recent message — including things you might consider private.
        - This leaves your machine and goes to the Anthropic API over
          HTTPS. Anthropic's retention terms apply.
      Permission needed:
        - Full Disk Access for whoever runs the script:
            • /bin/bash — required for the scheduled 02:00 run
            • Your terminal (Terminal.app, iTerm2, ...) — if you run
              the script manually and want chat.db to be readable
          System Settings → Privacy & Security → Full Disk Access → "+"
      Scope controls:
        - DIGEST_IMESSAGE_DAYS (default 3) sets how far back to read.
      Try before you commit:
        - You can say No here and still test it as a one-off with:
            DIGEST_USE_IMESSAGE=true ~/daily-digest/run.sh --dry-run
          The preview lands at ~/.local/share/daily-digest/preview.html
      Turn off later:
        - Edit config.env → set DIGEST_USE_IMESSAGE="false".
EOF
read -r -p "      Enable iMessage/SMS scanning? [$(_default_label "$prev_imsg")] " yn_imsg
USE_IMESSAGE="$(_parse_yn "$yn_imsg" "$prev_imsg")"
echo "      ${G}→${N} DIGEST_USE_IMESSAGE=$USE_IMESSAGE"
if [[ "$USE_IMESSAGE" == "true" ]]; then
    warn "iMessage scanning is enabled. Personal chat content will be sent"
    warn "to the Anthropic API. You can disable it any time by editing"
    warn "$CONFIG_FILE."
fi
echo ""


# ---------------------------------------------------------------------------
# 6. Write config
# ---------------------------------------------------------------------------
say "Writing config to $CONFIG_FILE"

mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
chmod 700 "$CONFIG_DIR"

cat > "$CONFIG_FILE" <<EOF
# daily-digest config — edit freely. Non-secret values only.
# Generated by install.sh on $(date).

DIGEST_GMAIL_ADDRESS="$GMAIL"
DIGEST_PYTHON="$PYTHON"

# Where to send the digest (defaults to DIGEST_GMAIL_ADDRESS).
# DIGEST_RECIPIENT="you@example.com"

# How many days of calendar to include:
DIGEST_CAL_DAYS="14"

# How many days of email to scan:
DIGEST_EMAIL_DAYS="3"

# How many days of past digests to keep in Gmail. Each run trashes older
# copies (moves to [Gmail]/Trash, which Google purges after 30 days).
# 1 = keep only today's digest (recommended). Set to 0 or negative to disable.
DIGEST_KEEP_DAYS="1"

# Also read Mail.app inboxes (covers any non-Gmail accounts added to
# Apple Mail). Requires Automation permission for Mail on first run.
DIGEST_USE_MAIL_APP="$USE_MAIL_APP"

# PRIVACY-SENSITIVE: also scan iMessage/SMS for calendar-like items.
# Reads ~/Library/Messages/chat.db; requires Full Disk Access. Sends
# message text to Claude. Opt in deliberately.
DIGEST_USE_IMESSAGE="$USE_IMESSAGE"
DIGEST_IMESSAGE_DAYS="3"

# Claude model. 'claude-opus-4-7' is most capable; 'claude-sonnet-4-6' is
# cheaper and fast enough.
ANTHROPIC_MODEL="claude-opus-4-7"
EOF
chmod 600 "$CONFIG_FILE"
ok "config written (use_mail_app=$USE_MAIL_APP use_imessage=$USE_IMESSAGE)"


# ---------------------------------------------------------------------------
# 7. Make scripts executable
# ---------------------------------------------------------------------------
chmod +x "$REPO_DIR/run.sh" "$REPO_DIR/daily_digest.py" "$REPO_DIR/uninstall.sh"
ok "run.sh, daily_digest.py, uninstall.sh marked executable"


# ---------------------------------------------------------------------------
# 8. Pick a run time + check for conflicts with other scheduled jobs
# ---------------------------------------------------------------------------
say "Scheduled run time"

DEFAULT_HOUR=2
DEFAULT_MINUTE=0

# If we're re-installing, keep the previous schedule as the default rather
# than forcing the user back to 02:00.
if [[ -f "$LAUNCHD_PLIST" ]]; then
    prev_hour=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Hour" "$LAUNCHD_PLIST" 2>/dev/null || echo "")
    prev_min=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Minute" "$LAUNCHD_PLIST" 2>/dev/null || echo "")
    [[ -n "$prev_hour" ]] && DEFAULT_HOUR="$prev_hour"
    [[ -n "$prev_min"  ]] && DEFAULT_MINUTE="$prev_min"
fi

# --- Show what's already scheduled, so the user can avoid clashes ---
echo "  Currently scheduled jobs on this user account:"
found_any=false
# Other LaunchAgents belonging to this user.
shopt -s nullglob
for pl in "$HOME/Library/LaunchAgents"/*.plist; do
    [[ "$pl" == "$LAUNCHD_PLIST" ]] && continue
    label=$(/usr/libexec/PlistBuddy -c "Print :Label" "$pl" 2>/dev/null || echo "?")
    h=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Hour"   "$pl" 2>/dev/null || true)
    m=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Minute" "$pl" 2>/dev/null || true)
    if [[ -n "$h" && -n "$m" ]]; then
        printf "    • %s — LaunchAgent %s\n" "$(printf '%02d:%02d' "$h" "$m")" "$label"
        found_any=true
    fi
done
shopt -u nullglob
# User crontab.
if crontab -l 2>/dev/null | grep -vE '^\s*(#|$)' | head -20 | awk 'NF>=5' | while read -r min hr rest; do
    case "$min" in *[!0-9]*) continue;; esac
    case "$hr"  in *[!0-9]*) continue;; esac
    printf "    • %02d:%02d — crontab: %s\n" "$hr" "$min" "$rest"
done | grep -q .; then
    found_any=true
fi
# pmset wake schedule (system-wide).
pmset_line=$(pmset -g sched 2>/dev/null | awk '/wake/{print; exit}')
if [[ -n "$pmset_line" ]]; then
    echo "    • system wake: $pmset_line"
    found_any=true
fi
$found_any || echo "    (none found — you'll be the first)"
echo ""

# Collect busy times once: minute-of-day for every other LaunchAgent
# scheduled with a StartCalendarInterval. We'll use this both to flag
# clashes and to propose a clash-free alternative.
busy_mins=()
shopt -s nullglob
for pl in "$HOME/Library/LaunchAgents"/*.plist; do
    [[ "$pl" == "$LAUNCHD_PLIST" ]] && continue
    h=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Hour"   "$pl" 2>/dev/null || true)
    m=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Minute" "$pl" 2>/dev/null || true)
    [[ -n "$h" && -n "$m" ]] && busy_mins+=( "$(( 10#$h * 60 + 10#$m )):$(/usr/libexec/PlistBuddy -c "Print :Label" "$pl" 2>/dev/null || echo "?")" )
done
shopt -u nullglob

_clash_labels() {   # $1 = candidate minute-of-day; prints any clashes
    local cand=$1 entry other_m lbl diff
    for entry in "${busy_mins[@]}"; do
        other_m=${entry%%:*}; lbl=${entry#*:}
        diff=$(( cand - other_m )); diff=${diff#-}
        if (( diff <= 15 )); then
            printf '      - LaunchAgent at %02d:%02d: %s\n' \
                "$(( other_m / 60 ))" "$(( other_m % 60 ))" "$lbl"
        fi
    done
}

_next_free_slot() {   # $1 = starting minute-of-day; prints first clash-free slot
    local start=$1 cand i
    for (( i=0; i<96; i++ )); do   # search up to 24h in 15-min steps
        cand=$(( (start + i * 15) % 1440 ))
        if [[ -z "$(_clash_labels "$cand")" ]]; then
            echo "$cand"; return 0
        fi
    done
    echo "$start"
}

# --- If the starting default clashes, shift it to the next free slot
#     BEFORE showing it to the user. The user can still override. ---
default_mins=$(( DEFAULT_HOUR * 60 + DEFAULT_MINUTE ))
default_clash="$(_clash_labels "$default_mins")"
if [[ -n "$default_clash" ]]; then
    warn "default $(printf '%02d:%02d' "$DEFAULT_HOUR" "$DEFAULT_MINUTE") clashes with:"
    printf '%s' "$default_clash"
    shifted=$(_next_free_slot $(( (default_mins + 30) % 1440 )))
    DEFAULT_HOUR=$(( shifted / 60 ))
    DEFAULT_MINUTE=$(( shifted % 60 ))
    ok "suggesting $(printf '%02d:%02d' "$DEFAULT_HOUR" "$DEFAULT_MINUTE") — press Enter to accept, or type another time"
fi

input_default=$(printf '%02d:%02d' "$DEFAULT_HOUR" "$DEFAULT_MINUTE")
while true; do
    read -r -p "  What time should the digest run? [HH:MM, default $input_default] " t_input
    t_input="${t_input:-$input_default}"
    if [[ ! "$t_input" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
        warn "expected HH:MM (e.g. 02:00, 07:30)"; continue
    fi
    HOUR=$((10#${BASH_REMATCH[1]}))
    MINUTE=$((10#${BASH_REMATCH[2]}))
    if (( HOUR < 0 || HOUR > 23 || MINUTE < 0 || MINUTE > 59 )); then
        warn "hour must be 0-23, minute must be 0-59"; continue
    fi

    chosen_mins=$(( HOUR * 60 + MINUTE ))
    clash="$(_clash_labels "$chosen_mins")"
    if [[ -n "$clash" ]]; then
        warn "another scheduled job fires within ±15 minutes of $(printf '%02d:%02d' "$HOUR" "$MINUTE"):"
        printf '%s' "$clash"
        # Auto-suggest the next free slot from chosen+30 min so we don't
        # just re-hit the clashing window.
        suggested=$(_next_free_slot $(( (chosen_mins + 30) % 1440 )))
        sug_str=$(printf '%02d:%02d' "$(( suggested / 60 ))" "$(( suggested % 60 ))")
        read -r -p "      Use $sug_str instead? [Y/n] " accept
        if [[ -z "$accept" || "$accept" =~ ^[Yy] ]]; then
            HOUR=$(( suggested / 60 )); MINUTE=$(( suggested % 60 ))
            ok "shifted to $sug_str to avoid the clash"
            break
        fi
        # User said no — re-prompt, defaulting to their original pick.
        input_default="$t_input"
        continue
    fi
    break
done
ok "scheduled run time: $(printf '%02d:%02d' "$HOUR" "$MINUTE") daily"


# ---------------------------------------------------------------------------
# 9. Install launchd plist
# ---------------------------------------------------------------------------
say "Installing launchd agent"

mkdir -p "$HOME/Library/LaunchAgents"

# Unload first if already installed, so the new version takes effect.
if launchctl list | grep -q "$LAUNCHD_LABEL"; then
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    ok "unloaded previous agent"
fi

# Render template
sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|__HOUR__|$HOUR|g" \
    -e "s|__MINUTE__|$MINUTE|g" \
    "$REPO_DIR/com.user.dailydigest.plist.template" \
    > "$LAUNCHD_PLIST"

launchctl load "$LAUNCHD_PLIST"
ok "agent installed and loaded ($LAUNCHD_PLIST)"


# ---------------------------------------------------------------------------
# 10. First run to trigger permission prompts
# ---------------------------------------------------------------------------
say "First run — expect macOS permission prompts"

echo "    macOS will ask for permission to read Calendar. Click Allow."
echo "    (This run is a dry run — no email will be sent.)"
echo ""
read -r -p "    Press Enter to continue..." _

set +e
"$REPO_DIR/run.sh" --dry-run
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    ok "dry run succeeded"
    ok "preview at $STATE_DIR/preview.html"
    echo ""
    echo "    To see the preview:"
    echo "        open $STATE_DIR/preview.html"
else
    warn "dry run exited with $rc — check $LOG_DIR/daily-digest.log"
fi


# ---------------------------------------------------------------------------
# 11. Remind about Full Disk Access
# ---------------------------------------------------------------------------
# Suggest a pmset wake a minute before the scheduled run.
wake_mins=$(( HOUR * 60 + MINUTE - 1 ))
(( wake_mins < 0 )) && wake_mins=$(( wake_mins + 1440 ))
wake_hh=$(( wake_mins / 60 ))
wake_mm=$(( wake_mins % 60 ))
wake_str=$(printf '%02d:%02d:00' "$wake_hh" "$wake_mm")
sched_str=$(printf '%02d:%02d' "$HOUR" "$MINUTE")

cat <<EOF

${B}Two things left, both manual:${N}

1. ${B}Full Disk Access${N} (required for the scheduled $sched_str launchd run
   to work unattended when you're not logged in at the terminal):

       System Settings → Privacy & Security → Full Disk Access
       → '+' → /bin/bash

2. ${B}Wake the Mac at $sched_str${N} (optional; if you skip this the digest
   fires on next wake instead of waking the Mac itself):

       sudo pmset repeat wake MTWRFSU $wake_str

   (Check current schedule with 'pmset -g sched'; cancel with
   'sudo pmset repeat cancel'. pmset stores only one schedule, so
   setting this overrides any existing repeat-wake.)

${B}Useful commands:${N}
   $REPO_DIR/run.sh --dry-run         # preview any time
   launchctl start $LAUNCHD_LABEL     # force a real run now
   tail -f $LOG_DIR/daily-digest.log  # watch it run
   $REPO_DIR/uninstall.sh             # remove everything

${G}Install complete.${N}
EOF
