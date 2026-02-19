#!/usr/bin/env python3
"""Backfill knowledge base from all historical Claude Code sessions.

Iterates through all session JSONL files, extracts knowledge from each,
and upserts into the KB. Skips agent subprocesses and tiny sessions.

Usage:
    python3 backfill.py                    # Process all sessions
    python3 backfill.py --dry-run          # Show what would be processed
    python3 backfill.py --limit 10         # Process only 10 sessions
    python3 backfill.py --min-size 50000   # Only sessions > 50KB
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Import from same directory
script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir))

import extract
import briefing

SESSIONS_DIR = Path.home() / ".claude" / "projects"
MIN_SIZE = 10_000  # Skip sessions < 10KB (trivial/empty)
LOG_PATH = Path.home() / ".claude" / "knowledge" / "backfill.log"


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


def read_session_transcript(path: Path) -> str:
    """Read a session JSONL and return the transcript text."""
    messages = []
    with open(path) as f:
        for line in f:
            try:
                msg = json.loads(line)
                role = msg.get("type", "")
                if role not in ("user", "assistant"):
                    continue

                inner = msg.get("message", {})
                content = inner.get("content", "") if isinstance(inner, dict) else ""

                if isinstance(content, list):
                    text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    content = "\n".join(text_parts)
                if content:
                    messages.append(f"[{role}]: {content}")
            except json.JSONDecodeError:
                continue

    # Take last 50 messages to stay within model limits
    return "\n\n".join(messages[-50:])


def find_all_sessions(min_size: int = MIN_SIZE) -> list[tuple[Path, int, float]]:
    """Find all main session files (not agent subprocesses), sorted by date."""
    sessions = []
    for f in SESSIONS_DIR.rglob("*.jsonl"):
        # Skip agent subprocesses
        if f.name.startswith("agent-"):
            continue
        size = f.stat().st_size
        if size < min_size:
            continue
        mtime = f.stat().st_mtime
        sessions.append((f, size, mtime))

    # Sort by modification time (oldest first)
    sessions.sort(key=lambda x: x[2])
    return sessions


def get_session_date(path: Path) -> str:
    """Get the date of a session from its modification time."""
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y-%m-%d')


def get_project_name(path: Path) -> str:
    """Extract project name from session path."""
    # Path looks like: ~/.claude/projects/-Users-kkaufmann-github-kaufmann-health/abc123.jsonl
    parts = path.parts
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            proj = parts[i + 1]
            # Clean up the path-encoded project name
            proj = proj.lstrip('-').replace('-Users-kkaufmann-', '').replace('-', '/')
            return proj
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Backfill KB from historical sessions")
    parser.add_argument('--dry-run', '-n', action='store_true', help='List sessions without processing')
    parser.add_argument('--limit', '-l', type=int, default=0, help='Max sessions to process (0=all)')
    parser.add_argument('--min-size', type=int, default=MIN_SIZE, help=f'Min session size in bytes (default: {MIN_SIZE})')
    parser.add_argument('--model', '-m', default=extract.DEFAULT_MODEL, help='OpenRouter model')
    parser.add_argument('--delay', type=int, default=2, help='Seconds between API calls (rate limiting)')
    args = parser.parse_args()

    sessions = find_all_sessions(args.min_size)
    total = len(sessions)

    log(f"Found {total} sessions to process (min size: {args.min_size} bytes)")

    if args.dry_run:
        for path, size, mtime in sessions:
            date = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            proj = get_project_name(path)
            print(f"  {date}  {size//1024:>5}KB  {proj}  {path.name}")
        print(f"\nTotal: {total} sessions")
        return

    succeeded = 0
    failed = 0
    skipped = 0

    limit = args.limit if args.limit > 0 else total

    for i, (path, size, mtime) in enumerate(sessions[:limit]):
        date = get_session_date(path)
        proj = get_project_name(path)
        source = f"backfill:{proj}/{path.stem}"

        log(f"[{i+1}/{min(limit, total)}] Processing {proj}/{path.name} ({size//1024}KB, {date})")

        # Read transcript
        transcript = read_session_transcript(path)
        if not transcript.strip():
            log(f"  Skipped (empty transcript)")
            skipped += 1
            continue

        # Truncate if needed
        if len(transcript) > 50000:
            transcript = transcript[-50000:]

        # Extract
        try:
            extractions = extract.call_extraction_model(transcript, args.model)
        except SystemExit:
            log(f"  FAILED (extraction error)")
            failed += 1
            time.sleep(args.delay)
            continue
        except Exception as e:
            log(f"  FAILED ({e})")
            failed += 1
            time.sleep(args.delay)
            continue

        entities = len(extractions.get('entities', []))
        facts = len(extractions.get('facts', []))
        relations = len(extractions.get('relations', []))
        decisions = len(extractions.get('decisions', []))

        if entities + facts + relations + decisions == 0:
            log(f"  Skipped (nothing extracted)")
            skipped += 1
            time.sleep(args.delay)
            continue

        # Write to DB
        try:
            db = extract.get_db()
            stats = extract.upsert_extractions(db, extractions, source, date)
            db.close()
            log(f"  OK: {stats['entities']}e {stats['facts']}f {stats['relations']}r {stats['decisions']}d ({stats['superseded']} superseded)")
            succeeded += 1
        except Exception as e:
            log(f"  FAILED writing to DB ({e})")
            failed += 1

        # Rate limit
        time.sleep(args.delay)

    # Regenerate BRIEF.md once at the end
    log("Regenerating BRIEF.md...")
    briefing.generate()

    log(f"Backfill complete: {succeeded} succeeded, {failed} failed, {skipped} skipped out of {min(limit, total)}")


if __name__ == '__main__':
    main()
