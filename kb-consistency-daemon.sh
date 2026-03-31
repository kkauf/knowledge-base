#!/bin/bash
# State consistency + reconciliation daemon
# Runs consistency check AND reconciles pending artifacts (batched).
# Designed to run via launchd every 4 hours.
# Consistency result is cached for standup via --skip-consistency flag.

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

log "Starting consistency + reconciliation"

# Run full pipeline: consistency check + artifact reconciliation + execution
# Batching is handled by pipeline_reconcile.py (default: 15 artifacts per run)
python3 "$REPO_DIR/pipeline_reconcile.py" --execute >> "$LOG" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    log "Consistency + reconciliation complete"
else
    log "Pipeline failed (exit $EXIT_CODE)"
fi
