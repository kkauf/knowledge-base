#!/bin/bash
# State consistency check daemon
# Runs the consistency check independently and caches the result.
# Designed to run via launchd every 4 hours.
# Result is read by standup via --skip-consistency flag.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -L "$0" ] && REPO_DIR="$(cd "$(dirname "$(readlink "$0")")" && pwd)"

KB_DIR="$(python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from config import get_kb_dir
print(get_kb_dir())
" 2>/dev/null)"
[ -z "$KB_DIR" ] && KB_DIR="$HOME/.claude/knowledge"

LOG="$KB_DIR/consistency-daemon.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

log "Starting consistency check"

# Run consistency-only mode — loads system state, calls GLM-5, caches result
python3 "$REPO_DIR/pipeline_reconcile.py" --consistency-only >> "$LOG" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    log "Consistency check complete"
else
    log "Consistency check failed (exit $EXIT_CODE)"
fi
