#!/bin/bash
# run.sh — wrapper invoked by launchd (or by you, manually).
# Sources non-secret config from ~/.config/daily-digest/config.env and
# pulls the Gmail app password from the login Keychain.
# Install once with install.sh.

set -euo pipefail

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/daily-digest/config.env"
if [[ ! -f "$CONFIG" ]]; then
    echo "No config at $CONFIG — run ./install.sh first." >&2
    exit 1
fi
# `set -a` auto-exports every variable assigned while it's active, so the
# vars in config.env (OLLAMA_MODEL, OLLAMA_NUM_CTX, DIGEST_CAL_DAYS, etc.)
# reach the Python child via the environment. Without this, `source`
# sets shell-local variables that `exec` does NOT inherit — so everything
# the user sets in config.env is silently ignored and defaults win.
set -a
# shellcheck disable=SC1090
source "$CONFIG"
set +a

: "${DIGEST_GMAIL_ADDRESS:?DIGEST_GMAIL_ADDRESS missing from $CONFIG}"
: "${DIGEST_PYTHON:?DIGEST_PYTHON missing from $CONFIG}"

# Pull the Gmail app password from the login Keychain. `security -w` prints
# the password on stdout and exits non-zero if missing, which is exactly the
# behaviour we want — fail loudly rather than run unauthenticated.
DIGEST_GMAIL_APP_PW="$(security find-generic-password \
    -a "$DIGEST_GMAIL_ADDRESS" -s daily-digest-gmail -w)"

export DIGEST_GMAIL_ADDRESS DIGEST_GMAIL_APP_PW

# When the hosted backend is configured, pull its API key from Keychain
# too. Skipped for the default (ollama) backend so the digest never
# depends on a key that doesn't exist.
if [[ "${DIGEST_BACKEND:-ollama}" == "openai_compatible" ]]; then
    OPENAI_API_KEY="$(security find-generic-password \
        -s daily-digest-openai -w)"
    export OPENAI_API_KEY
fi

# Everything else (DIGEST_CAL_DAYS, OLLAMA_MODEL, OLLAMA_URL, etc.)
# comes from config.env.
cd "$(dirname "$0")"
exec "$DIGEST_PYTHON" daily_digest.py "$@"
