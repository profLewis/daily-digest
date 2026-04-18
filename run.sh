#!/bin/bash
# run.sh — wrapper invoked by launchd (or by you, manually).
# Sources non-secret config from ~/.config/daily-digest/config.env and
# pulls secrets out of the login Keychain. Install once with install.sh.

set -euo pipefail

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/daily-digest/config.env"
if [[ ! -f "$CONFIG" ]]; then
    echo "No config at $CONFIG — run ./install.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG"

: "${DIGEST_GMAIL_ADDRESS:?DIGEST_GMAIL_ADDRESS missing from $CONFIG}"
: "${DIGEST_PYTHON:?DIGEST_PYTHON missing from $CONFIG}"

# Pull secrets from Keychain. `security -w` prints the password on stdout and
# exits non-zero if missing, which is exactly the behaviour we want — fail
# loudly rather than run unauthenticated.
DIGEST_GMAIL_APP_PW="$(security find-generic-password \
    -a "$DIGEST_GMAIL_ADDRESS" -s daily-digest-gmail -w)"
ANTHROPIC_API_KEY="$(security find-generic-password \
    -s daily-digest-anthropic -w)"

export DIGEST_GMAIL_ADDRESS DIGEST_GMAIL_APP_PW ANTHROPIC_API_KEY
# Everything else (DIGEST_CAL_DAYS, ANTHROPIC_MODEL, etc.) comes from config.env.

cd "$(dirname "$0")"
exec "$DIGEST_PYTHON" daily_digest.py "$@"
