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
# 5. Write config
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

# Claude model. 'claude-opus-4-7' is most capable; 'claude-sonnet-4-6' is
# cheaper and fast enough.
ANTHROPIC_MODEL="claude-opus-4-7"
EOF
chmod 600 "$CONFIG_FILE"
ok "config written"


# ---------------------------------------------------------------------------
# 6. Make scripts executable
# ---------------------------------------------------------------------------
chmod +x "$REPO_DIR/run.sh" "$REPO_DIR/daily_digest.py" "$REPO_DIR/uninstall.sh"
ok "run.sh, daily_digest.py, uninstall.sh marked executable"


# ---------------------------------------------------------------------------
# 7. Install launchd plist
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
    "$REPO_DIR/com.user.dailydigest.plist.template" \
    > "$LAUNCHD_PLIST"

launchctl load "$LAUNCHD_PLIST"
ok "agent installed and loaded ($LAUNCHD_PLIST)"


# ---------------------------------------------------------------------------
# 8. First run to trigger permission prompts
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
# 9. Remind about Full Disk Access
# ---------------------------------------------------------------------------
cat <<EOF

${B}Two things left, both manual:${N}

1. ${B}Full Disk Access${N} (required for the 02:00 launchd run to work
   unattended when you're not logged in at the terminal):

       System Settings → Privacy & Security → Full Disk Access
       → '+' → /bin/bash

2. ${B}Wake the Mac at 02:00${N} (optional; if you skip this the digest
   fires on next wake instead of waking the Mac itself):

       sudo pmset repeat wake MTWRFSU 01:59:00

${B}Useful commands:${N}
   $REPO_DIR/run.sh --dry-run         # preview any time
   launchctl start $LAUNCHD_LABEL     # force a real run now
   tail -f $LOG_DIR/daily-digest.log  # watch it run
   $REPO_DIR/uninstall.sh             # remove everything

${G}Install complete.${N}
EOF
