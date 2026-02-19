#!/bin/bash
# Knowledge Base extraction daemon
# Finds Claude Code sessions modified since last extraction and processes them.
# Designed to run via launchd every 30 minutes.
#
# Marker file tracks the last extraction timestamp so we only process new sessions.

set -euo pipefail

KB_DIR="$HOME/.claude/knowledge"
MARKER="$KB_DIR/.last-extraction"
LOG="$KB_DIR/extraction.log"
EXTRACT="$KB_DIR/extract.py"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

# Get last extraction time (epoch seconds), default to 0
if [ -f "$MARKER" ]; then
    LAST_RUN=$(cat "$MARKER")
else
    LAST_RUN=0
fi

# Find session files modified since last run
SESSIONS_DIR="$HOME/.claude/projects"
if [ ! -d "$SESSIONS_DIR" ]; then
    log "No sessions directory found"
    exit 0
fi

# Find .jsonl files newer than marker
NEW_SESSIONS=()
while IFS= read -r -d '' file; do
    MOD_TIME=$(stat -f '%m' "$file" 2>/dev/null || stat -c '%Y' "$file" 2>/dev/null)
    if [ "$MOD_TIME" -gt "$LAST_RUN" ]; then
        NEW_SESSIONS+=("$file")
    fi
done < <(find "$SESSIONS_DIR" -name "*.jsonl" -print0 2>/dev/null)

if [ ${#NEW_SESSIONS[@]} -eq 0 ]; then
    # No new sessions, nothing to do
    exit 0
fi

log "Found ${#NEW_SESSIONS[@]} session(s) to process"

# Process only the most recent session (avoid re-extracting old ones)
# Sort by modification time, take the latest
LATEST=""
LATEST_TIME=0
for session in "${NEW_SESSIONS[@]}"; do
    MOD_TIME=$(stat -f '%m' "$session" 2>/dev/null || stat -c '%Y' "$session" 2>/dev/null)
    if [ "$MOD_TIME" -gt "$LATEST_TIME" ]; then
        LATEST="$session"
        LATEST_TIME=$MOD_TIME
    fi
done

if [ -z "$LATEST" ]; then
    exit 0
fi

# Only process if the session hasn't been touched in the last 5 minutes
# (i.e., the session is likely finished, not still active)
NOW=$(date +%s)
AGE=$((NOW - LATEST_TIME))
if [ "$AGE" -lt 300 ]; then
    log "Most recent session is only ${AGE}s old, skipping (probably still active)"
    exit 0
fi

log "Processing: $LATEST (${AGE}s old)"

# Run extraction
if python3 "$EXTRACT" --last-session >> "$LOG" 2>&1; then
    log "Extraction complete"
else
    log "Extraction failed (exit code $?)"
fi

# Update marker
date +%s > "$MARKER"
log "Marker updated"
