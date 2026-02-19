#!/usr/bin/env python3
"""Knowledge Base CLI — query tool for semantic knowledge store."""

import argparse
import sqlite3
import sys
import os
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/.claude/knowledge/knowledge.db")


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}", file=sys.stderr)
        print("Run setup.sh first.", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def cmd_query(args):
    """Query all facts about an entity."""
    db = get_db()
    name_pattern = f"%{args.entity}%"

    # Find matching entities
    entities = db.execute(
        "SELECT * FROM entities WHERE lower(name) LIKE lower(?)",
        (name_pattern,)
    ).fetchall()

    if not entities:
        print(f"No entities matching '{args.entity}'")
        sys.exit(1)

    for entity in entities:
        print(f"\n{'='*60}")
        print(f"  {entity['name']}  ({entity['type']})")
        print(f"{'='*60}")

        # Current facts
        if args.history:
            facts = db.execute(
                "SELECT * FROM facts WHERE entity_id = ? ORDER BY attribute, valid_from DESC",
                (entity['id'],)
            ).fetchall()
        else:
            facts = db.execute(
                "SELECT * FROM facts WHERE entity_id = ? AND valid_to IS NULL ORDER BY attribute",
                (entity['id'],)
            ).fetchall()

        if facts:
            print("\n  Facts:")
            for f in facts:
                status = ""
                if f['valid_to']:
                    status = f"  [superseded {f['valid_to']}]"
                print(f"    {f['attribute']}: {f['value']}{status}")
                if args.verbose and f['source']:
                    print(f"      source: {f['source']}  |  since: {f['valid_from']}")

        # Current relations (outgoing)
        if args.history:
            rels_out = db.execute("""
                SELECT r.*, e.name as target_name FROM relations r
                JOIN entities e ON r.to_entity_id = e.id
                WHERE r.from_entity_id = ?
                ORDER BY r.relation_type, r.valid_from DESC
            """, (entity['id'],)).fetchall()
        else:
            rels_out = db.execute("""
                SELECT r.*, e.name as target_name FROM relations r
                JOIN entities e ON r.to_entity_id = e.id
                WHERE r.from_entity_id = ? AND r.valid_to IS NULL
                ORDER BY r.relation_type
            """, (entity['id'],)).fetchall()

        # Current relations (incoming)
        if args.history:
            rels_in = db.execute("""
                SELECT r.*, e.name as source_name FROM relations r
                JOIN entities e ON r.from_entity_id = e.id
                WHERE r.to_entity_id = ?
                ORDER BY r.relation_type, r.valid_from DESC
            """, (entity['id'],)).fetchall()
        else:
            rels_in = db.execute("""
                SELECT r.*, e.name as source_name FROM relations r
                JOIN entities e ON r.from_entity_id = e.id
                WHERE r.to_entity_id = ? AND r.valid_to IS NULL
                ORDER BY r.relation_type
            """, (entity['id'],)).fetchall()

        if rels_out or rels_in:
            print("\n  Relations:")
            for r in rels_out:
                status = f"  [ended {r['valid_to']}]" if r['valid_to'] else ""
                print(f"    → {r['relation_type']} → {r['target_name']}{status}")
            for r in rels_in:
                status = f"  [ended {r['valid_to']}]" if r['valid_to'] else ""
                print(f"    ← {r['relation_type']} ← {r['source_name']}{status}")

    db.close()


def cmd_search(args):
    """Full-text search across facts and decisions."""
    db = get_db()
    query = args.query
    found = False

    like_pattern = f"%{query}%"
    facts = db.execute("""
        SELECT f.*, e.name as entity_name FROM facts f
        JOIN entities e ON f.entity_id = e.id
        WHERE (lower(f.value) LIKE lower(?) OR lower(f.attribute) LIKE lower(?))
        AND f.valid_to IS NULL
        ORDER BY e.name
    """, (like_pattern, like_pattern)).fetchall()

    if facts:
        found = True
        print(f"\n  Current facts matching '{query}':")
        for f in facts:
            print(f"    [{f['entity_name']}] {f['attribute']}: {f['value']}")

    # Search decisions
    decisions = db.execute("""
        SELECT * FROM decisions
        WHERE lower(title) LIKE lower(?) OR lower(rationale) LIKE lower(?)
        ORDER BY decided_at DESC
    """, (like_pattern, like_pattern)).fetchall()

    if decisions:
        found = True
        print(f"\n  Decisions matching '{query}':")
        for d in decisions:
            status_marker = "" if d['status'] == 'active' else f" [{d['status']}]"
            print(f"    {d['title']}{status_marker}")
            if d['rationale']:
                print(f"      Rationale: {d['rationale'][:120]}")

    # Search entity names
    entities = db.execute(
        "SELECT * FROM entities WHERE lower(name) LIKE lower(?)",
        (like_pattern,)
    ).fetchall()

    if entities:
        found = True
        print(f"\n  Entities matching '{query}':")
        for e in entities:
            print(f"    {e['name']} ({e['type']})")

    if not found:
        print(f"No results for '{query}'")
        sys.exit(1)

    db.close()


def cmd_decisions(args):
    """List decisions."""
    db = get_db()

    if args.all:
        decisions = db.execute(
            "SELECT * FROM decisions ORDER BY decided_at DESC"
        ).fetchall()
    else:
        decisions = db.execute(
            "SELECT * FROM decisions WHERE status = 'active' ORDER BY decided_at DESC"
        ).fetchall()

    if not decisions:
        print("No decisions recorded.")
        sys.exit(1)

    print(f"\n  {'Active ' if not args.all else ''}Decisions:")
    print(f"  {'='*60}")
    for d in decisions:
        status_marker = "" if d['status'] == 'active' else f" [{d['status']}]"
        print(f"\n  {d['title']}{status_marker}")
        print(f"  Decided: {d['decided_at']}")
        if d['rationale']:
            print(f"  Rationale: {d['rationale']}")
        if d['context']:
            print(f"  Context: {d['context']}")

    db.close()


def cmd_entities(args):
    """List all entities."""
    db = get_db()

    entities = db.execute("""
        SELECT e.*, COUNT(f.id) as fact_count
        FROM entities e
        LEFT JOIN facts f ON e.id = f.entity_id AND f.valid_to IS NULL
        GROUP BY e.id
        ORDER BY e.type, e.name
    """).fetchall()

    if not entities:
        print("No entities in knowledge base.")
        sys.exit(1)

    current_type = None
    for e in entities:
        if e['type'] != current_type:
            current_type = e['type']
            print(f"\n  {current_type.upper()}")
            print(f"  {'-'*40}")
        print(f"    {e['name']}  ({e['fact_count']} facts)")

    db.close()


def cmd_assert(args):
    """Assert a fact (manual write)."""
    import uuid
    db = get_db()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Find or create entity
    entity = db.execute(
        "SELECT * FROM entities WHERE lower(name) = lower(?)",
        (args.entity,)
    ).fetchone()

    if not entity:
        entity_id = str(uuid.uuid4())[:8]
        entity_type = args.type or 'concept'
        db.execute(
            "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (entity_id, args.entity, entity_type, now, now)
        )
        print(f"  Created entity: {args.entity} ({entity_type})")
    else:
        entity_id = entity['id']
        db.execute(
            "UPDATE entities SET updated_at = ? WHERE id = ?",
            (now, entity_id)
        )

    # Supersede existing fact for same attribute
    existing = db.execute(
        "SELECT * FROM facts WHERE entity_id = ? AND attribute = ? AND valid_to IS NULL",
        (entity_id, args.attribute)
    ).fetchone()

    fact_id = str(uuid.uuid4())[:8]

    if existing:
        db.execute(
            "UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
            (today, fact_id, existing['id'])
        )
        print(f"  Superseded: {args.attribute} = {existing['value']}")

    db.execute(
        "INSERT INTO facts (id, entity_id, attribute, value, source, valid_from, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fact_id, entity_id, args.attribute, args.value, args.source or 'manual', today, now)
    )

    db.commit()
    print(f"  Asserted: [{args.entity}] {args.attribute} = {args.value}")
    db.close()


def cmd_decide(args):
    """Log a decision."""
    import uuid
    db = get_db()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    decision_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO decisions (id, title, rationale, status, context, decided_at, created_at) VALUES (?, ?, ?, 'active', ?, ?, ?)",
        (decision_id, args.title, args.rationale, args.context, today, now)
    )

    db.commit()
    print(f"  Decision logged: {args.title}")
    db.close()


def cmd_status(args):
    """Show KB health: daemon status, last extraction, DB stats."""
    import subprocess
    kb_dir = os.path.expanduser("~/.claude/knowledge")
    marker_path = os.path.join(kb_dir, ".last-extraction")
    log_path = os.path.join(kb_dir, "extraction.log")
    brief_path = os.path.join(kb_dir, "BRIEF.md")

    print("\n  Knowledge Base Status")
    print(f"  {'='*50}")

    # 1. Last extraction time
    if os.path.exists(marker_path):
        with open(marker_path) as f:
            last_epoch = int(f.read().strip())
        last_dt = datetime.fromtimestamp(last_epoch, tz=timezone.utc)
        age_secs = int((datetime.now(timezone.utc) - last_dt).total_seconds())
        if age_secs < 3600:
            age_str = f"{age_secs // 60}m ago"
        elif age_secs < 86400:
            age_str = f"{age_secs // 3600}h ago"
        else:
            age_str = f"{age_secs // 86400}d ago"
        healthy = age_secs < 7200  # <2h = healthy (daemon runs every 30m)
        indicator = "OK" if healthy else "STALE"
        print(f"\n  Last extraction: {last_dt.strftime('%Y-%m-%d %H:%M UTC')} ({age_str}) [{indicator}]")
    else:
        print(f"\n  Last extraction: NEVER (marker file missing)")

    # 2. Daemon status
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/com.kaufmann.kb-extract"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"  Daemon: LOADED (launchd, every 30m)")
        else:
            print(f"  Daemon: NOT LOADED")
    except Exception:
        print(f"  Daemon: UNKNOWN (couldn't check launchctl)")

    # 3. BRIEF.md age
    if os.path.exists(brief_path):
        brief_mtime = os.path.getmtime(brief_path)
        brief_dt = datetime.fromtimestamp(brief_mtime, tz=timezone.utc)
        brief_age = int((datetime.now(timezone.utc) - brief_dt).total_seconds())
        if brief_age < 3600:
            brief_age_str = f"{brief_age // 60}m ago"
        elif brief_age < 86400:
            brief_age_str = f"{brief_age // 3600}h ago"
        else:
            brief_age_str = f"{brief_age // 86400}d ago"
        print(f"  BRIEF.md: {brief_age_str}")
    else:
        print(f"  BRIEF.md: MISSING")

    # 4. DB stats
    db = get_db()
    entity_count = db.execute("SELECT COUNT(*) as c FROM entities").fetchone()['c']
    fact_count = db.execute("SELECT COUNT(*) as c FROM facts WHERE valid_to IS NULL").fetchone()['c']
    superseded = db.execute("SELECT COUNT(*) as c FROM facts WHERE valid_to IS NOT NULL").fetchone()['c']
    decision_count = db.execute("SELECT COUNT(*) as c FROM decisions WHERE status = 'active'").fetchone()['c']
    relation_count = db.execute("SELECT COUNT(*) as c FROM relations WHERE valid_to IS NULL").fetchone()['c']

    # Facts added in last 7 days
    week_ago = (datetime.now(timezone.utc)).strftime('%Y-%m-%d')
    recent_facts = db.execute(
        "SELECT COUNT(*) as c FROM facts WHERE created_at > datetime('now', '-7 days')"
    ).fetchone()['c']

    print(f"\n  DB: {entity_count} entities, {fact_count} facts, {relation_count} relations, {decision_count} decisions")
    print(f"  This week: {recent_facts} new facts, {superseded} total superseded")

    # 5. Last 5 log lines
    if os.path.exists(log_path):
        with open(log_path) as f:
            lines = f.readlines()
        recent = lines[-5:] if len(lines) >= 5 else lines
        if recent:
            print(f"\n  Recent log:")
            for line in recent:
                print(f"    {line.rstrip()}")

    print()
    db.close()


def cmd_domain(args):
    """List entities in a domain with their top facts."""
    db = get_db()
    # Normalize domain name (case-insensitive lookup)
    domain_map = {"kh": "KH", "personal": "Personal", "infrastructure": "Infrastructure",
                  "vss": "VSS", "isai": "IsAI", "other": "Other"}
    domain = domain_map.get(args.domain.lower(), args.domain)

    # Check if entity_domains table exists
    has_table = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='entity_domains'"
    ).fetchone()[0]

    if not has_table:
        print("Error: entity_domains table not found. Run migrate-domains.py first.", file=sys.stderr)
        sys.exit(2)

    entities = db.execute("""
        SELECT e.id, e.name, e.type, ed.confidence, COUNT(f.id) as fact_count
        FROM entity_domains ed
        JOIN entities e ON ed.entity_id = e.id
        LEFT JOIN facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE ed.domain = ?
        GROUP BY e.id
        ORDER BY fact_count DESC
    """, (domain,)).fetchall()

    if not entities:
        print(f"No entities in domain '{domain}'")
        print(f"Available domains: KH, Personal, Infrastructure, VSS, IsAI, Other")
        sys.exit(1)

    print(f"\n  Domain: {domain} ({len(entities)} entities)")
    print(f"  {'='*60}")

    max_facts = args.facts if hasattr(args, 'facts') else 3

    for e in entities:
        conf = f" [{e['confidence']:.0%}]" if e['confidence'] < 1.0 else ""
        print(f"\n  **{e['name']}** ({e['type']}, {e['fact_count']}f){conf}")

        if max_facts > 0:
            facts = db.execute("""
                SELECT attribute, value FROM facts
                WHERE entity_id = ? AND valid_to IS NULL
                ORDER BY created_at DESC
                LIMIT ?
            """, (e['id'], max_facts)).fetchall()

            for f in facts:
                val = f['value'][:80] if len(f['value']) > 80 else f['value']
                print(f"    {f['attribute']}: {val}")

    db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Knowledge Base CLI — query and manage semantic knowledge",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    # query
    p_query = subparsers.add_parser('query', help='Query facts about an entity')
    p_query.add_argument('entity', help='Entity name (partial match)')
    p_query.add_argument('--history', action='store_true', help='Include superseded facts')
    p_query.add_argument('-v', '--verbose', action='store_true', help='Show sources and dates')

    # search
    p_search = subparsers.add_parser('search', help='Full-text search across all knowledge')
    p_search.add_argument('query', help='Search term')

    # decisions
    p_decisions = subparsers.add_parser('decisions', help='List decisions')
    p_decisions.add_argument('--all', action='store_true', help='Include superseded/reversed')

    # entities
    subparsers.add_parser('entities', help='List all entities')

    # assert
    p_assert = subparsers.add_parser('assert', help='Assert a fact about an entity')
    p_assert.add_argument('entity', help='Entity name')
    p_assert.add_argument('attribute', help='Attribute name')
    p_assert.add_argument('value', help='Attribute value')
    p_assert.add_argument('--type', help='Entity type (if creating new)', default=None)
    p_assert.add_argument('--source', help='Source of this fact', default=None)

    # decide
    p_decide = subparsers.add_parser('decide', help='Log a decision')
    p_decide.add_argument('title', help='Decision title')
    p_decide.add_argument('--rationale', help='Why this decision was made')
    p_decide.add_argument('--context', help='Related context or entity names')

    # status
    subparsers.add_parser('status', help='Show KB health: daemon, last extraction, stats')

    # domain
    p_domain = subparsers.add_parser('domain', help='List entities in a domain (brain region)')
    p_domain.add_argument('domain', help='Domain name: KH, Personal, Infrastructure, VSS, IsAI, Other')
    p_domain.add_argument('--facts', type=int, default=3, help='Max facts per entity (default: 3, 0=names only)')

    args = parser.parse_args()

    commands = {
        'query': cmd_query,
        'search': cmd_search,
        'decisions': cmd_decisions,
        'entities': cmd_entities,
        'assert': cmd_assert,
        'decide': cmd_decide,
        'status': cmd_status,
        'domain': cmd_domain,
    }

    commands[args.command](args)


if __name__ == '__main__':
    main()
