#!/usr/bin/env python3
"""
Session Memory — ADR-007 Implementation (Phase 1+2)

Stores topic-organized prose summaries of sessions and makes them searchable
via FTS5 for recall injection. Embeddings deferred to Phase 3.

Architecture:
  Artifact Extraction (existing) → session_memory field (expanded prompt)
      ↓
  session_memory.py store_summary() → SQLite session_summaries + FTS5
      ↓
  kb-recall.py → search_sessions() → one-liners injected in system-reminder

Storage: knowledge.db (same DB as entities/facts).
Search: FTS5 keyword matching (fast, 0 cost, <5ms).
Embeddings: Deferred — FTS5 covers most recall cases.

Usage:
    from session_memory import init_db, store_summary, search_sessions

    init_db(db)
    store_summary(db, session_path, domain, summary, one_liner, topics)
    results = search_sessions(db, "resistance bands shoulder rehab", limit=3)
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_db_path


def get_db() -> sqlite3.Connection:
    """Get a connection to knowledge.db."""
    db = sqlite3.connect(str(get_db_path()))
    db.row_factory = sqlite3.Row
    return db


def init_db(db: sqlite3.Connection = None):
    """Create session_summaries table and FTS5 index if they don't exist."""
    close = False
    if db is None:
        db = get_db()
        close = True

    db.executescript("""
        CREATE TABLE IF NOT EXISTS session_summaries (
            id TEXT PRIMARY KEY,
            session_path TEXT NOT NULL,
            project TEXT,
            domain TEXT,
            summary TEXT NOT NULL,
            one_liner TEXT,
            topics TEXT,
            token_count INTEGER,
            session_date TEXT,
            created_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_session_summaries_date
            ON session_summaries(session_date);
        CREATE INDEX IF NOT EXISTS idx_session_summaries_domain
            ON session_summaries(domain);
    """)

    # FTS5 virtual table for keyword search
    # Check if it already exists (CREATE VIRTUAL TABLE IF NOT EXISTS not supported)
    existing = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_memory_fts'"
    ).fetchone()
    if not existing:
        db.execute("""
            CREATE VIRTUAL TABLE session_memory_fts USING fts5(
                session_id UNINDEXED,
                one_liner,
                summary,
                topics,
                tokenize='porter unicode61'
            )
        """)

    db.commit()
    if close:
        db.close()


def store_summary(
    db: sqlite3.Connection,
    session_path: str,
    domain: str = None,
    summary: str = "",
    one_liner: str = "",
    topics: list = None,
    session_date: str = None,
) -> bool:
    """Store a session summary. Returns True if new, False if duplicate.

    Idempotent — uses session filename as ID, skips if already stored.
    """
    session_id = os.path.basename(session_path).replace(".jsonl", "")
    project = _extract_project(session_path)

    if not summary and not one_liner:
        return False

    # Check for existing
    existing = db.execute(
        "SELECT id FROM session_summaries WHERE id = ?", (session_id,)
    ).fetchone()

    if existing:
        # Update if the new summary is longer (incremental extraction may produce richer summaries)
        old = db.execute(
            "SELECT summary FROM session_summaries WHERE id = ?", (session_id,)
        ).fetchone()
        if old and len(old["summary"]) >= len(summary):
            return False
        # Update
        db.execute("""
            UPDATE session_summaries
            SET summary = ?, one_liner = ?, topics = ?, token_count = ?, updated_at = ?
            WHERE id = ?
        """, (
            summary, one_liner,
            json.dumps(topics) if topics else None,
            len(summary.split()),
            datetime.now(timezone.utc).isoformat(),
            session_id,
        ))
        # Update FTS
        db.execute("DELETE FROM session_memory_fts WHERE session_id = ?", (session_id,))
        db.execute(
            "INSERT INTO session_memory_fts (session_id, one_liner, summary, topics) VALUES (?, ?, ?, ?)",
            (session_id, one_liner, summary, " ".join(topics) if topics else ""),
        )
        db.commit()
        return True

    # Insert new
    if not session_date:
        session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    db.execute("""
        INSERT INTO session_summaries (id, session_path, project, domain, summary, one_liner, topics, token_count, session_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id, session_path, project, domain,
        summary, one_liner,
        json.dumps(topics) if topics else None,
        len(summary.split()),
        session_date,
        datetime.now(timezone.utc).isoformat(),
    ))

    # FTS5 index
    db.execute(
        "INSERT INTO session_memory_fts (session_id, one_liner, summary, topics) VALUES (?, ?, ?, ?)",
        (session_id, one_liner, summary, " ".join(topics) if topics else ""),
    )

    db.commit()
    return True


def search_sessions(
    db: sqlite3.Connection,
    query: str,
    limit: int = 3,
    domain: str = None,
) -> list[dict]:
    """Search session summaries via FTS5.

    Returns list of dicts with: session_id, one_liner, summary, session_date, domain, rank.
    """
    if not query or len(query.strip()) < 3:
        return []

    # Build FTS5 query — split words, OR them for broader matching
    words = query.strip().split()
    # Escape FTS5 special chars
    safe_words = [w.replace('"', '').replace("'", "") for w in words if len(w) > 2]
    if not safe_words:
        return []
    fts_query = " OR ".join(safe_words)

    sql = """
        SELECT s.id as session_id, s.one_liner, s.summary, s.session_date, s.domain,
               rank as relevance
        FROM session_memory_fts f
        JOIN session_summaries s ON s.id = f.session_id
        WHERE session_memory_fts MATCH ?
    """
    params = [fts_query]

    if domain:
        sql += " AND s.domain = ?"
        params.append(domain)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # FTS5 query syntax error — fall back to simpler query
        return []


def get_recent_summaries(
    db: sqlite3.Connection,
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Get recent session summaries for context."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = db.execute("""
        SELECT id as session_id, one_liner, summary, session_date, domain
        FROM session_summaries
        WHERE session_date >= ?
        ORDER BY session_date DESC
        LIMIT ?
    """, (cutoff, limit)).fetchall()

    return [dict(r) for r in rows]


def format_recall(results: list[dict]) -> str:
    """Format session memory results for recall hook injection.

    Returns compact one-liner pointers (ADR-007 spec: ~30 tokens).
    """
    if not results:
        return ""

    lines = []
    for r in results:
        date = r.get("session_date", "?")
        # Format date as [Mon DD] if possible
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_fmt = dt.strftime("%b %d")
        except (ValueError, TypeError):
            date_fmt = date

        one_liner = r.get("one_liner", "")
        if not one_liner:
            # Fall back to first sentence of summary
            summary = r.get("summary", "")
            one_liner = summary.split(".")[0][:100] if summary else "?"

        lines.append(f"  - [{date_fmt}] {one_liner}")

    return "Session context (memory match):\n" + "\n".join(lines)


def _extract_project(session_path: str) -> str:
    """Extract project identifier from session path."""
    # Path: ~/.claude/projects/-Users-foo-github-project/session.jsonl
    parts = Path(session_path).parts
    for p in parts:
        if p.startswith("-Users-") or p.startswith("-home-"):
            return p
    return ""


# --- CLI for testing ---

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Session memory management")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize database tables")
    search_p = sub.add_parser("search", help="Search session summaries")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=5)
    sub.add_parser("recent", help="Show recent summaries")
    sub.add_parser("stats", help="Show session memory stats")

    args = parser.parse_args()
    db = get_db()

    if args.command == "init":
        init_db(db)
        print("Session memory tables initialized.")

    elif args.command == "search":
        init_db(db)
        results = search_sessions(db, args.query, limit=args.limit)
        if not results:
            print("No matching sessions.")
        else:
            print(format_recall(results))
            print(f"\n({len(results)} result(s))")

    elif args.command == "recent":
        init_db(db)
        results = get_recent_summaries(db, days=14)
        if not results:
            print("No recent summaries.")
        else:
            for r in results:
                print(f"  [{r['session_date']}] {r.get('domain', '?'):12s} {r['one_liner'] or r['summary'][:80]}")

    elif args.command == "stats":
        init_db(db)
        count = db.execute("SELECT count(*) as n FROM session_summaries").fetchone()["n"]
        recent = db.execute(
            "SELECT count(*) as n FROM session_summaries WHERE session_date >= date('now', '-7 days')"
        ).fetchone()["n"]
        print(f"Session summaries: {count} total, {recent} in last 7 days")

    else:
        parser.print_help()

    db.close()
