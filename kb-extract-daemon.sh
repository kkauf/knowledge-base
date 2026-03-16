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

# Resolve repo dir (follow symlink if this script was symlinked by setup.sh)
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -L "$0" ] && REPO_DIR="$(cd "$(dirname "$(readlink "$0")")" && pwd)"

# Load all config values in one Python call
_config_out="$(python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from config import get_kb_dir, get_sessions_dir, get_recall_script, cfg
print(f'KB_DIR=\"{get_kb_dir()}\"')
print(f'SESSIONS_DIR=\"{get_sessions_dir()}\"')
r = get_recall_script() or ''
print(f'RECALL_SCRIPT=\"{r}\"')
print(f'MAX_PER_RUN={cfg(\"daemon_max_per_run\", 5)}')
" 2>/dev/null)"
if [ -n "$_config_out" ]; then
    eval "$_config_out"
else
    KB_DIR="$HOME/.claude/knowledge"
    SESSIONS_DIR="$HOME/.claude/projects"
    RECALL_SCRIPT=""
    MAX_PER_RUN=5
fi

MARKER="$KB_DIR/.last-extraction"
LOG="$KB_DIR/extraction.log"
EXTRACT="$REPO_DIR/extract.py"
ARTIFACT_EXTRACT="$REPO_DIR/artifact_extract.py"
CONTEXT_FRAME="$REPO_DIR/context_frame.py"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

# Refresh context frame (TTL-based, only regenerates if stale)
# This gives extraction prompts awareness of active commitments
if [ -f "$CONTEXT_FRAME" ]; then
    python3 "$CONTEXT_FRAME" --refresh >> "$LOG" 2>&1 || log "Context frame refresh failed (non-critical)"
fi

# Refresh session-task map (TTL-based, same cadence as context frame)
# Gives extractors awareness of which sessions are linked to Konban tasks
python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from context_frame import load_session_task_map
m = load_session_task_map()
print(f'Session-task map: {len(m)} linked session(s)')
" >> "$LOG" 2>&1 || log "Session-task map refresh failed (non-critical)"

# Get last extraction time (epoch seconds), default to 0
if [ -f "$MARKER" ]; then
    LAST_RUN=$(cat "$MARKER")
else
    LAST_RUN=0
fi

# Find session files modified since last run
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

    # Pre-filter: classify session to decide which pipeline stages to run
    PREFILTER="$REPO_DIR/session_prefilter.py"
    SKIP_FACTS=false
    SKIP_ARTIFACTS=false
    if [ -f "$PREFILTER" ]; then
        CLASSIFY_OUTPUT=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from session_prefilter import quick_classify
s = quick_classify('$SESSION')
print(f'skip_facts={s[\"skip_facts\"]} skip_artifacts={s[\"skip_artifacts\"]} user={s[\"user_chars\"]} asst={s[\"asst_chars\"]} msgs={s[\"msg_count\"]} sub={s[\"is_subagent\"]}')
" 2>/dev/null)
        if [ -n "$CLASSIFY_OUTPUT" ]; then
            log "Classify: $CLASSIFY_OUTPUT"
            [[ "$CLASSIFY_OUTPUT" == *"skip_facts=True"* ]] && SKIP_FACTS=true
            [[ "$CLASSIFY_OUTPUT" == *"skip_artifacts=True"* ]] && SKIP_ARTIFACTS=true
        fi
    fi

    # Skip both stages → advance offset without LLM calls
    if $SKIP_FACTS && $SKIP_ARTIFACTS; then
        log "Skipped (pre-filter: no signal): $(basename "$SESSION")"
        PROCESSED=$((PROCESSED + 1))
        if [ "$MOD_TIME" -gt "$LATEST_PROCESSED_TIME" ]; then
            LATEST_PROCESSED_TIME=$MOD_TIME
        fi
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Fact extraction (skipped for tiny subagent sessions)
    if $SKIP_FACTS; then
        log "Fact extraction skipped (subagent, <1500 user chars): $(basename "$SESSION")"
    else
        python3 "$EXTRACT" --session "$SESSION" >> "$LOG" 2>&1
        EXIT_CODE=$?

        if [ "$EXIT_CODE" -eq 0 ]; then
            log "Fact extraction complete: $(basename "$SESSION")"
        elif [ "$EXIT_CODE" -eq 2 ]; then
            log "Fact extraction skipped (empty/no data): $(basename "$SESSION")"
        else
            log "Extraction FAILED (exit $EXIT_CODE): $(basename "$SESSION") — stopping, will retry next run"
            FAILED=$((FAILED + 1))
            break
        fi
    fi

    PROCESSED=$((PROCESSED + 1))
    if [ "$MOD_TIME" -gt "$LATEST_PROCESSED_TIME" ]; then
        LATEST_PROCESSED_TIME=$MOD_TIME
    fi

    # Artifact extraction (skipped if no long assistant messages)
    if $SKIP_ARTIFACTS; then
        log "Artifact extraction skipped (no long assistant msgs): $(basename "$SESSION")"
    elif [ -f "$ARTIFACT_EXTRACT" ]; then
        log "Running artifact extraction: $(basename "$SESSION")"
        python3 "$ARTIFACT_EXTRACT" --session "$SESSION" >> "$LOG" 2>&1
        ART_EXIT=$?
        if [ "$ART_EXIT" -eq 0 ]; then
            log "Artifact extraction complete: $(basename "$SESSION")"
        else
            log "Artifact extraction failed (exit $ART_EXIT): $(basename "$SESSION") — continuing"
        fi
    fi
done

# Only advance marker if we successfully processed at least one session
if [ "$LATEST_PROCESSED_TIME" -gt "$LAST_RUN" ]; then
    echo "$LATEST_PROCESSED_TIME" > "$MARKER"
    log "Marker advanced to $LATEST_PROCESSED_TIME"
fi

# Seed lookup_path routing pointers on new entities (deterministic, no LLM cost)
SEED_SCRIPT="$REPO_DIR/seed-lookup-paths.py"
if [ "$PROCESSED" -gt 0 ] && [ -f "$SEED_SCRIPT" ]; then
    python3 "$SEED_SCRIPT" --write >> "$LOG" 2>&1
    log "Lookup paths seeded"
fi

# Rebuild recall index if we processed anything
if [ "$PROCESSED" -gt 0 ] && [ -n "$RECALL_SCRIPT" ]; then
    if [ -f "$RECALL_SCRIPT" ]; then
        if python3 "$RECALL_SCRIPT" --build-index >> "$LOG" 2>&1; then
            log "Recall index rebuilt"
        else
            log "Recall index rebuild failed (non-critical)"
        fi
    fi
fi

log "Done: $PROCESSED extracted, $FAILED failed, $SKIPPED skipped (active)"
