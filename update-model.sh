#!/bin/bash
# update-model.sh — refresh the configured Ollama model.
#
# Runs `ollama pull $OLLAMA_MODEL`, which is idempotent: if the local
# copy already matches the upstream manifest it's a no-op; otherwise
# the delta is downloaded. Safe to schedule or run by hand any time.
#
# Invoked weekly by the com.user.dailydigest.update LaunchAgent (set up
# by install.sh) so you don't silently drift onto a months-stale tag.

set -euo pipefail

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/daily-digest/config.env"
LOG="$HOME/Library/Logs/daily-digest-update.log"

if [[ ! -f "$CONFIG" ]]; then
    echo "No config at $CONFIG — run install.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG"

: "${OLLAMA_MODEL:?OLLAMA_MODEL missing from $CONFIG}"
: "${OLLAMA_URL:=http://localhost:11434}"

mkdir -p "$(dirname "$LOG")"

# launchd strips PATH down to /usr/bin:/bin:/usr/sbin:/sbin, so the
# ollama CLI from Homebrew won't be found without adding its bin dir.
for bp in /opt/homebrew/bin /usr/local/bin; do
    [[ -d "$bp" ]] && PATH="$bp:$PATH"
done
export PATH

# Make the ollama CLI target the same daemon the digest does (matters
# if OLLAMA_URL points at a remote host on the LAN).
export OLLAMA_HOST="$OLLAMA_URL"

{
    echo "==== $(date '+%Y-%m-%d %H:%M:%S') update start ===="
    echo "model:    $OLLAMA_MODEL"
    echo "url:      $OLLAMA_URL"

    if ! command -v ollama >/dev/null 2>&1; then
        echo "ERROR: ollama CLI not on PATH ($PATH)" >&2
        echo "==== failed ===="
        exit 2
    fi

    if ollama pull "$OLLAMA_MODEL"; then
        echo "==== ok at $(date '+%Y-%m-%d %H:%M:%S') ===="
    else
        rc=$?
        echo "==== failed (ollama pull exit $rc) ===="
        exit $rc
    fi
} 2>&1 | tee -a "$LOG"
