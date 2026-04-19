#!/bin/bash
# uninstall.sh — undo everything install.sh did that we can undo
# automatically, and print clear manual steps for everything macOS won't
# let us touch programmatically (Automation permissions, Full Disk
# Access, sudo pmset schedules).
#
#   ./uninstall.sh            remove agent, config, lockfile, Keychain items
#   ./uninstall.sh --purge    also remove state dir and log files

set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/daily-digest"
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/daily-digest"
LAUNCHD_LABEL="com.user.dailydigest"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
UPDATE_LABEL="com.user.dailydigest.update"
UPDATE_PLIST="$HOME/Library/LaunchAgents/$UPDATE_LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -t 1 ]]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else
    B=""; G=""; Y=""; N=""
fi
say()  { echo "${B}==>${N} $*"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }

PURGE=false
[[ "${1:-}" == "--purge" ]] && PURGE=true

# ---------------------------------------------------------------------------
# 1. Stop and remove the launchd agent
# ---------------------------------------------------------------------------
say "Stopping launchd agent"
if launchctl list | grep -q "$LAUNCHD_LABEL"; then
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    ok "unloaded $LAUNCHD_LABEL"
else
    ok "agent not loaded"
fi
if [[ -f "$LAUNCHD_PLIST" ]]; then
    rm "$LAUNCHD_PLIST"
    ok "removed $LAUNCHD_PLIST"
else
    ok "plist not present"
fi

# The weekly update agent is a separate launchd job installed alongside
# the main digest agent. Remove it too.
if launchctl list | grep -q "$UPDATE_LABEL"; then
    launchctl unload "$UPDATE_PLIST" 2>/dev/null || true
    ok "unloaded $UPDATE_LABEL"
else
    ok "update agent not loaded"
fi
if [[ -f "$UPDATE_PLIST" ]]; then
    rm "$UPDATE_PLIST"
    ok "removed $UPDATE_PLIST"
else
    ok "update plist not present"
fi

# ---------------------------------------------------------------------------
# 2. Kill any still-running daily_digest.py (belt-and-braces)
# ---------------------------------------------------------------------------
say "Stopping any running daily-digest processes"
if pgrep -f 'daily_digest\.py' >/dev/null 2>&1; then
    pkill -f 'daily_digest\.py' || true
    ok "sent SIGTERM to running instance(s)"
else
    ok "no running instances"
fi

# ---------------------------------------------------------------------------
# 3. Config, lockfile, Keychain
# ---------------------------------------------------------------------------
say "Removing config"
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    ok "removed $CONFIG_DIR"
else
    ok "config dir not present"
fi

# The lockfile lives in state_dir; remove it even without --purge so a
# re-install starts clean.
if [[ -f "$STATE_DIR/daily-digest.lock" ]]; then
    rm -f "$STATE_DIR/daily-digest.lock"
    ok "removed lockfile"
fi

say "Removing Keychain entries"
if security delete-generic-password -s daily-digest-gmail 2>/dev/null; then
    ok "removed Gmail app password from Keychain"
else
    ok "Gmail Keychain entry not present"
fi
# Hosted backend's API key.
if security delete-generic-password -s daily-digest-openai 2>/dev/null; then
    ok "removed hosted-backend API key from Keychain"
fi
# Older versions of this tool stored an Anthropic API key in the Keychain
# (item 'daily-digest-anthropic'). Clean it up if it's still there.
if security delete-generic-password -s daily-digest-anthropic 2>/dev/null; then
    ok "removed obsolete Anthropic Keychain entry from a prior install"
fi

# ---------------------------------------------------------------------------
# 4. State + logs (only under --purge)
# ---------------------------------------------------------------------------
if $PURGE; then
    say "Purging state + logs"
    if [[ -d "$STATE_DIR" ]]; then
        rm -rf "$STATE_DIR"
        ok "removed $STATE_DIR"
    fi
    # Remove all logs we might have written.
    local_removed=0
    for f in "$LOG_DIR/daily-digest.log" \
             "$LOG_DIR/daily-digest.stdout.log" \
             "$LOG_DIR/daily-digest.stderr.log" \
             "$LOG_DIR/daily-digest-update.log" \
             "$LOG_DIR/daily-digest-update.stdout.log" \
             "$LOG_DIR/daily-digest-update.stderr.log"; do
        if [[ -f "$f" ]]; then
            rm -f "$f"
            local_removed=$((local_removed + 1))
        fi
    done
    [[ $local_removed -gt 0 ]] && ok "removed $local_removed log file(s) from $LOG_DIR"
else
    say "Keeping state at $STATE_DIR and logs in $LOG_DIR"
    warn "pass --purge to delete them"
fi

# ---------------------------------------------------------------------------
# 5. Manual steps we can't do programmatically — print them loudly
# ---------------------------------------------------------------------------
cat <<EOF

${B}Local state removed.${N} A few things macOS or third parties own that
we ${B}cannot${N} remove automatically — do these manually if you want a
fully clean slate:

${B}macOS permissions${N} (System Settings → Privacy & Security):
  • Automation       → remove any "Calendar" or "Mail" entries for bash,
                        osascript, or your terminal.
  • Full Disk Access → remove /bin/bash if you added it for this
                        tool, and your terminal if you added it for
                        iMessage testing.

${B}Scheduled wake${N} (if you set one up per the README):
  sudo pmset repeat cancel

${B}Third-party credentials${N} (the Keychain copy is gone, but the
Gmail app password itself still exists upstream):
  • Revoke the Gmail app password:
      https://myaccount.google.com/apppasswords

${B}Local model backend${N} (left in place — you probably use Ollama for
other things):
  • If install.sh started Ollama via 'brew services', it's still running.
    To stop it without uninstalling:
      brew services stop ollama
  • To remove the package entirely (Homebrew install):
      brew services stop ollama
      brew uninstall ollama
      rm -rf ~/.ollama                # pulled model weights (many GB)
  • To remove Ollama.app (manual install): quit it from the menu bar,
    drag the app to Trash, and rm -rf ~/.ollama.

${B}The repo itself${N} (untouched so you can reinstall if you want):
  rm -rf "$REPO_DIR"

${G}Done.${N}
EOF
