#!/bin/bash
# Setup script for Knowledge Base
# Idempotent — safe to run multiple times

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# Determine KB_DIR from config or default
CONFIG_FILE="$HOME/.knowledge-base/config.json"
if [ -f "$CONFIG_FILE" ]; then
    # Read kb_dir from config (simple grep, no jq dependency)
    KB_DIR=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('kb_dir', '~/.knowledge-base'))" 2>/dev/null | sed "s|~|$HOME|")
    DAEMON_LABEL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('daemon_label', 'org.knowledge-base.extract'))" 2>/dev/null)
else
    KB_DIR="$HOME/.knowledge-base"
    DAEMON_LABEL="org.knowledge-base.extract"
fi

DB_PATH="$KB_DIR/knowledge.db"
PLIST_TEMPLATE="$REPO_DIR/kb-extract.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$DAEMON_LABEL.plist"

echo "Setting up Knowledge Base..."
echo "  KB directory: $KB_DIR"

# Check for migration from old location
OLD_KB="$HOME/.claude/knowledge"
if [ -f "$OLD_KB/knowledge.db" ] && [ ! -f "$DB_PATH" ]; then
    echo ""
    echo "  Found existing KB at $OLD_KB"
    echo "  To migrate, either:"
    echo "    1. Set \"kb_dir\": \"$OLD_KB\" in $CONFIG_FILE (use existing location)"
    echo "    2. Copy: cp -r $OLD_KB/* $KB_DIR/ (move to new location)"
    echo ""
fi

# Create runtime directory
mkdir -p "$KB_DIR"

# Copy config template if no config exists
if [ ! -f "$CONFIG_FILE" ]; then
    mkdir -p "$(dirname "$CONFIG_FILE")"
    cp "$REPO_DIR/config.example.json" "$CONFIG_FILE"
    echo "  Config: $CONFIG_FILE (created from template — edit to customize)"
fi

# Apply schema (IF NOT EXISTS makes this idempotent)
sqlite3 "$DB_PATH" < "$REPO_DIR/schema.sql"
echo "  Database: $DB_PATH"

# Symlink scripts
for script in kb.py extract.py briefing.py kb-extract-daemon.sh; do
    ln -sf "$REPO_DIR/$script" "$KB_DIR/$script"
    echo "  Linked: $KB_DIR/$script"
done

# Generate initial BRIEF.md if it doesn't exist
if [ ! -f "$KB_DIR/BRIEF.md" ]; then
    python3 "$KB_DIR/briefing.py"
    echo "  Generated: $KB_DIR/BRIEF.md"
fi

# Install launchd agent for automatic extraction (macOS only)
echo ""
echo "Extraction daemon:"
if [ "$(uname)" = "Darwin" ] && [ -f "$PLIST_TEMPLATE" ]; then
    # Unload old daemon if loaded (handles label changes between versions)
    for old_label in "$DAEMON_LABEL"; do
        launchctl bootout "gui/$(id -u)/$old_label" 2>/dev/null || true
    done

    # Generate plist from template
    sed -e "s|__DAEMON_LABEL__|$DAEMON_LABEL|g" \
        -e "s|__REPO_DIR__|$REPO_DIR|g" \
        -e "s|__KB_DIR__|$KB_DIR|g" \
        "$PLIST_TEMPLATE" > "$PLIST_DST"

    # Load
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    echo "  Installed: $PLIST_DST (runs every 30 min)"
    echo "  Logs: $KB_DIR/extraction.log"
else
    if [ "$(uname)" != "Darwin" ]; then
        echo "  Skipped (launchd is macOS-only; use cron or systemd on Linux)"
    else
        echo "  Skipped (plist template not found)"
    fi
fi

echo ""
echo "Done."
