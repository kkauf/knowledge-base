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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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

    args = parser.parse_args()

    commands = {
        'query': cmd_query,
        'search': cmd_search,
        'decisions': cmd_decisions,
        'entities': cmd_entities,
        'assert': cmd_assert,
        'decide': cmd_decide,
    }

    commands[args.command](args)


if __name__ == '__main__':
    main()
