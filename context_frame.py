#!/usr/bin/env python3
"""
Dynamic Context Frame Generator

Generates a context frame from live system state (Konban, Active Context, Brain index,
KB activity) that gets injected into extraction prompts. This gives the extraction LLM
awareness of what matters — active commitments, strategic priorities, recent activity —
so it can recognize signals that would otherwise be dismissed as "ephemeral."

The context frame is cached at ~/.claude/knowledge/context-frame.md with a configurable
TTL. The daemon refreshes it at the start of each run (if stale).

Usage:
    # Generate or refresh (respects TTL, default 6 hours)
    python3 context_frame.py --refresh

    # Force regeneration (ignore TTL)
    python3 context_frame.py --generate

    # Custom TTL
    python3 context_frame.py --refresh --ttl 12

    # Print to stdout without saving
    python3 context_frame.py --generate --stdout
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Paths ---

KB_DIR = Path.home() / ".claude" / "knowledge"
CONTEXT_FRAME_FILE = KB_DIR / "context-frame.md"
DB_PATH = KB_DIR / "knowledge.db"

KONBAN_SCRIPT = Path.home() / ".claude" / "skills" / "konban" / "notion-api.py"
BRAIN_SCRIPT = Path.home() / ".claude" / "skills" / "notion-docs" / "notion-api.py"

DEFAULT_TTL_HOURS = 6


# --- Helpers ---

def run_cmd(args: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


# --- Data loaders ---

def load_konban_board() -> str:
    """Load current Konban board — active commitments across all domains."""
    if not KONBAN_SCRIPT.exists():
        return "[Konban unavailable]"
    output = run_cmd(["python3", str(KONBAN_SCRIPT), "board"], timeout=30)
    return output or "[Konban empty or unavailable]"


def load_active_context_summary() -> str:
    """Load Active Context from Brain — strategic priorities."""
    if not BRAIN_SCRIPT.exists():
        return "[Brain unavailable]"
    output = run_cmd(["python3", str(BRAIN_SCRIPT), "read", "Active Context", "--raw"], timeout=30)
    if not output:
        return "[Active Context unavailable]"
    # Truncate to first ~2000 chars to keep the frame focused
    if len(output) > 2000:
        output = output[:2000] + "\n... (truncated — see full Active Context via notion-docs)"
    return output


def load_brain_index() -> str:
    """Load Brain doc index — what's already documented."""
    if not BRAIN_SCRIPT.exists():
        return "[Brain unavailable]"
    output = run_cmd(["python3", str(BRAIN_SCRIPT), "index"], timeout=30)
    return output or "[Brain index unavailable]"


def load_recent_kb_activity(days: int = 7) -> str:
    """Load recent KB activity — what the system has been learning about."""
    if not DB_PATH.exists():
        return "[KB unavailable]"

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row

        # Top entities by recent fact count
        entities = conn.execute("""
            SELECT e.name, e.type, COUNT(f.id) as recent_facts
            FROM entities e
            JOIN facts f ON f.entity_id = e.id
            WHERE f.created_at > datetime('now', ? || ' days')
            GROUP BY e.id
            ORDER BY recent_facts DESC
            LIMIT 15
        """, (f"-{days}",)).fetchall()

        # Recent decisions
        decisions = conn.execute("""
            SELECT title, decided_at
            FROM decisions
            WHERE decided_at > date('now', ? || ' days')
            ORDER BY decided_at DESC
            LIMIT 10
        """, (f"-{days}",)).fetchall()

        conn.close()

        lines = []
        if entities:
            lines.append("Top entities this week:")
            for e in entities:
                lines.append(f"  - {e['name']} ({e['type']}) — {e['recent_facts']} new facts")

        if decisions:
            lines.append("\nRecent decisions:")
            for d in decisions:
                lines.append(f"  - [{d['decided_at']}] {d['title']}")

        return "\n".join(lines) if lines else "[No recent activity]"

    except (sqlite3.Error, OSError):
        return "[KB query failed]"


# --- Frame generation ---

def generate_context_frame() -> str:
    """Generate the full context frame from live system state."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")

    print("Generating context frame...")

    konban = load_konban_board()
    print(f"  Konban: {len(konban)} chars")

    active_context = load_active_context_summary()
    print(f"  Active Context: {len(active_context)} chars")

    brain_index = load_brain_index()
    print(f"  Brain index: {len(brain_index)} chars")

    kb_activity = load_recent_kb_activity()
    print(f"  KB activity: {len(kb_activity)} chars")

    frame = f"""# Extraction Context Frame
Generated: {timestamp}

## Active Commitments (Konban Board)

These are things being actively tracked. When conversations reference these commitments,
the following are EXTRACTABLE (not ephemeral):
- Scheduling decisions: "moved X to Tuesday", "rescheduling Y to next week"
- Progress signals: "finished part 1 of X", "X is halfway done"
- Friction signals: "struggling to commit to X", "keep postponing Y"
- Completion signals: "X is done", "submitted Y"

{konban}

## Strategic Priorities (Active Context)

{active_context}

## Brain Document Index (what's already documented)

{brain_index}

## Recent Knowledge Activity (last 7 days)

{kb_activity}
"""
    return frame


def is_stale(ttl_hours: float) -> bool:
    """Check if the context frame file is stale (older than TTL)."""
    if not CONTEXT_FRAME_FILE.exists():
        return True

    mtime = CONTEXT_FRAME_FILE.stat().st_mtime
    age_hours = (datetime.now().timestamp() - mtime) / 3600
    return age_hours > ttl_hours


def get_or_refresh(ttl_hours: float = DEFAULT_TTL_HOURS) -> str:
    """Return the context frame, regenerating if stale."""
    if is_stale(ttl_hours):
        frame = generate_context_frame()
        CONTEXT_FRAME_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONTEXT_FRAME_FILE.write_text(frame)
        print(f"Context frame saved to {CONTEXT_FRAME_FILE}")
        return frame
    else:
        age = (datetime.now().timestamp() - CONTEXT_FRAME_FILE.stat().st_mtime) / 3600
        print(f"Context frame is fresh ({age:.1f}h old, TTL {ttl_hours}h)")
        return CONTEXT_FRAME_FILE.read_text()


def load_context_frame() -> str:
    """Load the context frame for use in extraction prompts.

    Returns empty string if no frame exists (graceful degradation).
    Called by extract.py and artifact_extract.py.
    """
    if CONTEXT_FRAME_FILE.exists():
        return CONTEXT_FRAME_FILE.read_text()
    return ""


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="Generate dynamic context frame for extraction")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--refresh", action="store_true",
                      help="Refresh context frame if stale (respects TTL)")
    mode.add_argument("--generate", action="store_true",
                      help="Force regeneration (ignore TTL)")

    parser.add_argument("--ttl", type=float, default=DEFAULT_TTL_HOURS,
                        help=f"TTL in hours for --refresh mode (default: {DEFAULT_TTL_HOURS})")
    parser.add_argument("--stdout", action="store_true",
                        help="Print to stdout instead of saving to file")

    args = parser.parse_args()

    if args.refresh:
        frame = get_or_refresh(args.ttl)
    else:
        frame = generate_context_frame()
        if not args.stdout:
            CONTEXT_FRAME_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONTEXT_FRAME_FILE.write_text(frame)
            print(f"Context frame saved to {CONTEXT_FRAME_FILE}")

    if args.stdout:
        print("\n" + frame)


if __name__ == "__main__":
    main()
