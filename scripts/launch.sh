#!/bin/bash
# Open the config editor, starting the server first if it isn't already up.
# Safe to run repeatedly: a second click reuses the running server.
#
# Self-update: before starting, the checkout is fast-forwarded from GitHub
# (best effort, a few seconds at most, silently skipped offline). If that
# or the hourly cc-utils updater brought new code while a server was already
# running, the server is restarted so the update takes effect. Set
# CC_NO_SELFUPDATE=1 to skip the pull.

set -euo pipefail

PORT="${CONFIG_EDITOR_PORT:-8421}"
URL="http://localhost:${PORT}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/config-editor.log"
MATCH="config_editor --port ${PORT}"

is_up() {
    # Any HTTP answer means something is already serving this port.
    curl -sf --max-time 2 -o /dev/null "${URL}/api/state" 2>/dev/null
}

notify() {
    command -v notify-send >/dev/null && notify-send "RMS Config Editor" "$1" || echo "$1" >&2
}

# --- self-update ------------------------------------------------------------
if [ "${CC_NO_SELFUPDATE:-0}" != "1" ] && [ -d "$PROJECT_DIR/.git" ]; then
    before="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || true)"
    # Fast-forward only: a checkout with local commits or edits is left alone.
    if timeout 20 git -C "$PROJECT_DIR" pull --ff-only --quiet >>"$LOG" 2>&1; then
        after="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || true)"
        if [ -n "$before" ] && [ "$before" != "$after" ]; then
            echo "$(date -Is) updated $before -> $after" >>"$LOG"
            notify "Updated to $(git -C "$PROJECT_DIR" log -1 --format='%h (%cs)')"
        fi
    fi
fi

# A server started before an update (the launcher's pull above or the hourly
# cc-utils updater) keeps running the old code: restart it once the checkout
# has moved past the commit it started on.
REV_FILE="${LOG%.log}.rev"
HEAD_REV="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || true)"
if [ -n "$HEAD_REV" ] && is_up && [ "$(cat "$REV_FILE" 2>/dev/null || true)" != "$HEAD_REV" ]; then
    pkill -f "$MATCH" 2>/dev/null || true
    for _ in $(seq 20); do is_up || break; sleep 0.2; done
fi

# Prefer the interpreter deploy.sh installed into; plain python3 works too
# (the editor is pure stdlib).
PYTHON="${CONFIG_EDITOR_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for cand in "$PROJECT_DIR/.venv/bin/python" "$HOME/vRMS/bin/python"; do
        if [ -x "$cand" ]; then
            PYTHON="$cand"
            break
        fi
    done
fi
PYTHON="${PYTHON:-python3}"

if ! is_up; then
    mkdir -p "$(dirname "$LOG")"
    cd "$PROJECT_DIR"
    nohup "$PYTHON" -m config_editor --port "$PORT" >>"$LOG" 2>&1 &
    disown
    printf '%s\n' "$HEAD_REV" >"$REV_FILE"

    # Give it a moment to bind before pointing a browser at it.
    for _ in $(seq 30); do
        is_up && break
        sleep 0.2
    done

    if ! is_up; then
        notify "Server failed to start — see ${LOG}"
        exit 1
    fi
fi

xdg-open "$URL" >/dev/null 2>&1
