#!/bin/bash
# uninstall.sh — remove launchd agent, config, and Keychain items.
# Pass --purge to also delete state (yesterday.html and archives).

set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/daily-digest"
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/daily-digest"
LAUNCHD_LABEL="com.user.dailydigest"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"

if [[ -t 1 ]]; then
    B=$'\033[1m'; G=$'\033[32m'; N=$'\033[0m'
else
    B=""; G=""; N=""
fi
say() { echo "${B}==>${N} $*"; }
ok()  { echo "    ${G}✓${N} $*"; }

PURGE=false
[[ "${1:-}" == "--purge" ]] && PURGE=true

say "Stopping launchd agent"
if launchctl list | grep -q "$LAUNCHD_LABEL"; then
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    ok "unloaded"
fi
if [[ -f "$LAUNCHD_PLIST" ]]; then
    rm "$LAUNCHD_PLIST"
    ok "removed $LAUNCHD_PLIST"
fi

say "Removing config"
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    ok "removed $CONFIG_DIR"
fi

say "Removing Keychain entries"
security delete-generic-password -s daily-digest-gmail     2>/dev/null && ok "removed Gmail app password" || true
security delete-generic-password -s daily-digest-anthropic 2>/dev/null && ok "removed Anthropic API key"  || true

if $PURGE; then
    say "Purging state"
    if [[ -d "$STATE_DIR" ]]; then
        rm -rf "$STATE_DIR"
        ok "removed $STATE_DIR"
    fi
else
    say "Keeping state at $STATE_DIR (pass --purge to delete)"
fi

echo ""
echo "${G}Uninstalled.${N} Repo files themselves are untouched — remove with:"
echo "    rm -rf \"$(cd "$(dirname "$0")" && pwd)\""
