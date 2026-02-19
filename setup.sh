#!/bin/bash
# Setup script for Knowledge Base
# Idempotent — safe to run multiple times

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
KB_DIR="$HOME/.claude/knowledge"
DB_PATH="$KB_DIR/knowledge.db"
PLIST_NAME="com.kaufmann.kb-extract"
PLIST_SRC="$REPO_DIR/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "Setting up Knowledge Base..."

# Create runtime directory
mkdir -p "$KB_DIR"

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

# Install launchd agent for automatic extraction
echo ""
echo "Extraction daemon:"
if [ -f "$PLIST_SRC" ]; then
    # Unload if already loaded
    launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
    # Copy plist (launchd doesn't follow symlinks reliably)
    cp "$PLIST_SRC" "$PLIST_DST"
    # Load
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    echo "  Installed: $PLIST_DST (runs every 30 min)"
    echo "  Logs: $KB_DIR/extraction.log"
else
    echo "  Skipped (plist not found)"
fi

echo ""
echo "Done."
