#!/bin/bash
# Knowledge Base extraction daemon
# Finds Claude Code sessions modified since last extraction and processes them.
# Designed to run via launchd every 30 minutes.
#
# Processes ALL qualifying sessions (oldest first), not just the most recent.
# Skips sessions modified in the last 5 minutes (probably still active).
# Only advances the marker past successfully processed sessions.
# Stops on first failure to avoid skipping sessions when the API is down.

set -uo pipefail

KB_DIR="$HOME/.claude/knowledge"
MARKER="$KB_DIR/.last-extraction"
LOG="$KB_DIR/extraction.log"
EXTRACT="$KB_DIR/extract.py"
ARTIFACT_EXTRACT="$HOME/github/knowledge-base/artifact_extract.py"
MAX_PER_RUN=5  # Cap to avoid hammering the API on large backlogs

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

# Collect sessions modified since marker, with their modification times
declare -a SESSION_PAIRS=()
while IFS= read -r -d '' file; do
    MOD_TIME=$(stat -f '%m' "$file" 2>/dev/null || stat -c '%Y' "$file" 2>/dev/null)
    if [ "$MOD_TIME" -gt "$LAST_RUN" ]; then
        SESSION_PAIRS+=("$MOD_TIME|$file")
    fi
done < <(find "$SESSIONS_DIR" -name "*.jsonl" -print0 2>/dev/null)

if [ ${#SESSION_PAIRS[@]} -eq 0 ]; then
    exit 0
fi

# Sort by modification time (oldest first) so we process in chronological order
IFS=$'\n' SORTED=($(sort -t'|' -k1 -n <<<"${SESSION_PAIRS[*]}")); unset IFS

NOW=$(date +%s)
PROCESSED=0
SKIPPED=0
FAILED=0
LATEST_PROCESSED_TIME=$LAST_RUN

log "Found ${#SORTED[@]} session(s) to process"

for entry in "${SORTED[@]}"; do
    MOD_TIME="${entry%%|*}"
    SESSION="${entry#*|}"
    AGE=$((NOW - MOD_TIME))

    # Skip sessions still being written to (< 5 min old)
    if [ "$AGE" -lt 300 ]; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Cap sessions per run
    if [ "$PROCESSED" -ge "$MAX_PER_RUN" ]; then
        log "Hit per-run cap ($MAX_PER_RUN), remaining sessions deferred to next run"
        break
    fi

    log "Processing: $SESSION (${AGE}s old)"

    # Use --session flag for proper JSONL parsing
    python3 "$EXTRACT" --session "$SESSION" >> "$LOG" 2>&1
    EXIT_CODE=$?

    if [ "$EXIT_CODE" -eq 0 ]; then
        log "Fact extraction complete: $(basename "$SESSION")"
        PROCESSED=$((PROCESSED + 1))
        if [ "$MOD_TIME" -gt "$LATEST_PROCESSED_TIME" ]; then
            LATEST_PROCESSED_TIME=$MOD_TIME
        fi

        # Stage 1: Artifact extraction (runs alongside fact extraction)
        # Uses separate offset tracking and GLM-5 model
        if [ -f "$ARTIFACT_EXTRACT" ]; then
            log "Running artifact extraction: $(basename "$SESSION")"
            python3 "$ARTIFACT_EXTRACT" --session "$SESSION" >> "$LOG" 2>&1
            ART_EXIT=$?
            if [ "$ART_EXIT" -eq 0 ]; then
                log "Artifact extraction complete: $(basename "$SESSION")"
            else
                # Artifact extraction failure is non-critical — don't stop the daemon
                log "Artifact extraction failed (exit $ART_EXIT): $(basename "$SESSION") — continuing"
            fi
        fi
    elif [ "$EXIT_CODE" -eq 2 ]; then
        # Exit code 2 = empty transcript or no data — skip and advance past it
        log "Skipped (empty/no data): $(basename "$SESSION")"
        if [ "$MOD_TIME" -gt "$LATEST_PROCESSED_TIME" ]; then
            LATEST_PROCESSED_TIME=$MOD_TIME
        fi
    else
        # Exit code 1 = API/extraction error — stop and retry next run
        log "Extraction FAILED (exit $EXIT_CODE): $(basename "$SESSION") — stopping, will retry next run"
        FAILED=$((FAILED + 1))
        break
    fi
done

# Only advance marker if we successfully processed at least one session
if [ "$LATEST_PROCESSED_TIME" -gt "$LAST_RUN" ]; then
    echo "$LATEST_PROCESSED_TIME" > "$MARKER"
    log "Marker advanced to $LATEST_PROCESSED_TIME"
fi

# Rebuild recall index if we processed anything
if [ "$PROCESSED" -gt 0 ]; then
    RECALL_SCRIPT="$HOME/.claude/scripts/kb-recall.py"
    if [ -f "$RECALL_SCRIPT" ]; then
        if python3 "$RECALL_SCRIPT" --build-index >> "$LOG" 2>&1; then
            log "Recall index rebuilt"
        else
            log "Recall index rebuild failed (non-critical)"
        fi
    fi
fi

log "Done: $PROCESSED extracted, $FAILED failed, $SKIPPED skipped (active)"
