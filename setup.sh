#!/bin/bash
# Setup script for Knowledge Base
# Idempotent — safe to run multiple times

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
KB_DIR="$HOME/.claude/knowledge"
DB_PATH="$KB_DIR/knowledge.db"

echo "Setting up Knowledge Base..."

# Create runtime directory
mkdir -p "$KB_DIR"

# Apply schema (IF NOT EXISTS makes this idempotent)
sqlite3 "$DB_PATH" < "$REPO_DIR/schema.sql"
echo "Database: $DB_PATH"

# Symlink scripts
for script in kb.py extract.py briefing.py; do
    ln -sf "$REPO_DIR/$script" "$KB_DIR/$script"
    echo "Linked: $KB_DIR/$script → $REPO_DIR/$script"
done

# Generate initial BRIEF.md if it doesn't exist
if [ ! -f "$KB_DIR/BRIEF.md" ]; then
    python3 "$KB_DIR/briefing.py"
    echo "Generated: $KB_DIR/BRIEF.md"
fi

echo ""
echo "Done. Add this to ~/.claude/CLAUDE.md:"
echo ""
echo '### Knowledge Base'
echo '- Context: `~/.claude/knowledge/BRIEF.md` — auto-generated facts about people, projects, decisions.'
echo '- Deep lookup: `python3 ~/.claude/knowledge/kb.py query "entity_name"`'
echo '- Post-session: `python3 ~/.claude/knowledge/extract.py --last-session`'
