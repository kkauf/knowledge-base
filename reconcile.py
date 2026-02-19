#!/usr/bin/env python3
"""Reconciliation — merge duplicate entities and clean up the knowledge graph.

Finds entities that refer to the same real-world thing and merges them:
- Facts from the secondary entity are moved to the primary
- Relations are re-pointed
- Domain assignments are merged
- Secondary entity is deleted

Usage:
    python3 reconcile.py              # Auto-detect and merge duplicates
    python3 reconcile.py --dry        # Preview merges without changing anything
    python3 reconcile.py --prune      # Also remove orphan entities (no facts, no relations)
"""

import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/.claude/knowledge/knowledge.db")

# Manually confirmed semantic duplicates: (canonical_name, duplicate_name)
SEMANTIC_MERGES = [
    ("Konstantin Kaufmann", "Kkaufmann"),
    ("Konstantin Kaufmann", "K Kaufmann"),
    ("Konstantin Kaufmann", "K. Kaufmann"),
]


def normalize(name: str) -> str:
    """Normalize entity name for duplicate detection."""
    n = name.lower().strip()
    n = n.replace('-', ' ').replace('_', ' ').replace('.', ' ')
    return ' '.join(n.split())


def find_duplicates(db) -> list[tuple[str, str]]:
    """Find duplicate entity pairs: (keep_id, merge_id).

    Keep the entity with more facts. If tied, keep the one with the human-readable name.
    """
    entities = db.execute('SELECT id, name, type FROM entities ORDER BY name').fetchall()

    # Group by normalized name
    groups = defaultdict(list)
    for e in entities:
        groups[normalize(e['name'])].append(e)

    merges = []
    for norm, entries in groups.items():
        if len(entries) < 2:
            continue

        # Pick the primary: most facts, then prefer human-readable name (no dashes)
        def score(e):
            fc = db.execute(
                'SELECT COUNT(*) FROM facts WHERE entity_id = ? AND valid_to IS NULL',
                (e['id'],)
            ).fetchone()[0]
            readable = 0 if '-' in e['name'] or '_' in e['name'] else 1
            return (fc, readable, e['name'])

        ranked = sorted(entries, key=score, reverse=True)
        primary = ranked[0]
        for secondary in ranked[1:]:
            merges.append((primary['id'], primary['name'], secondary['id'], secondary['name']))

    # Add semantic merges
    for canonical, duplicate in SEMANTIC_MERGES:
        canon = db.execute('SELECT id FROM entities WHERE name = ?', (canonical,)).fetchone()
        dupe = db.execute('SELECT id FROM entities WHERE name = ?', (duplicate,)).fetchone()
        if canon and dupe:
            # Don't add if already covered by normalization
            pair = (canon['id'], dupe['id'])
            existing_pairs = {(m[0], m[2]) for m in merges}
            if pair not in existing_pairs:
                merges.append((canon['id'], canonical, dupe['id'], duplicate))

    return merges


def merge_entity(db, keep_id: str, keep_name: str, merge_id: str, merge_name: str, dry: bool) -> dict:
    """Merge merge_id into keep_id."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    stats = {"facts_moved": 0, "facts_skipped": 0, "relations_moved": 0, "domains_moved": 0}

    # 1. Move facts (skip if same attribute already exists on primary)
    facts = db.execute(
        'SELECT * FROM facts WHERE entity_id = ?', (merge_id,)
    ).fetchall()

    for f in facts:
        existing = db.execute(
            'SELECT id FROM facts WHERE entity_id = ? AND attribute = ? AND valid_to IS NULL',
            (keep_id, f['attribute'])
        ).fetchone()

        if existing and f['valid_to'] is None:
            # Primary already has a current fact for this attribute — skip
            stats["facts_skipped"] += 1
            continue

        if not dry:
            db.execute('UPDATE facts SET entity_id = ? WHERE id = ?', (keep_id, f['id']))
        stats["facts_moved"] += 1

    # 2. Move relations (re-point from/to)
    rels_from = db.execute(
        'SELECT id FROM relations WHERE from_entity_id = ?', (merge_id,)
    ).fetchall()
    rels_to = db.execute(
        'SELECT id FROM relations WHERE to_entity_id = ?', (merge_id,)
    ).fetchall()

    for r in rels_from:
        if not dry:
            db.execute('UPDATE relations SET from_entity_id = ? WHERE id = ?', (keep_id, r['id']))
        stats["relations_moved"] += 1
    for r in rels_to:
        if not dry:
            db.execute('UPDATE relations SET to_entity_id = ? WHERE id = ?', (keep_id, r['id']))
        stats["relations_moved"] += 1

    # 3. Move domain assignments
    domains = db.execute(
        'SELECT domain, confidence FROM entity_domains WHERE entity_id = ?', (merge_id,)
    ).fetchall()

    for d in domains:
        existing = db.execute(
            'SELECT confidence FROM entity_domains WHERE entity_id = ? AND domain = ?',
            (keep_id, d['domain'])
        ).fetchone()

        if existing:
            # Keep higher confidence
            if d['confidence'] > existing['confidence']:
                if not dry:
                    db.execute(
                        'UPDATE entity_domains SET confidence = ? WHERE entity_id = ? AND domain = ?',
                        (d['confidence'], keep_id, d['domain'])
                    )
                stats["domains_moved"] += 1
        else:
            if not dry:
                db.execute(
                    'INSERT INTO entity_domains (entity_id, domain, confidence, source) VALUES (?, ?, ?, ?)',
                    (keep_id, d['domain'], d['confidence'], 'reconcile')
                )
            stats["domains_moved"] += 1

    # 4. Delete secondary entity and its domain assignments
    if not dry:
        db.execute('DELETE FROM entity_domains WHERE entity_id = ?', (merge_id,))
        db.execute('DELETE FROM entities WHERE id = ?', (merge_id,))

    return stats


def prune_orphans(db, dry: bool) -> int:
    """Remove entities with no facts and no relations."""
    orphans = db.execute("""
        SELECT e.id, e.name, e.type FROM entities e
        WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.entity_id = e.id)
        AND NOT EXISTS (SELECT 1 FROM relations r WHERE r.from_entity_id = e.id OR r.to_entity_id = e.id)
    """).fetchall()

    if orphans:
        print(f"\n  Orphan entities ({len(orphans)}):")
        for o in orphans[:20]:
            print(f"    {o['name']} ({o['type']})")
        if len(orphans) > 20:
            print(f"    ... and {len(orphans) - 20} more")

        if not dry:
            for o in orphans:
                db.execute('DELETE FROM entity_domains WHERE entity_id = ?', (o['id'],))
                db.execute('DELETE FROM entities WHERE id = ?', (o['id'],))

    return len(orphans)


def main():
    dry = "--dry" in sys.argv
    do_prune = "--prune" in sys.argv

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    if dry:
        print("DRY RUN — no changes will be made\n")

    # Find and merge duplicates
    merges = find_duplicates(db)

    if not merges:
        print("No duplicates found.")
    else:
        print(f"Found {len(merges)} duplicate pairs to merge:\n")

        total_stats = {"facts_moved": 0, "facts_skipped": 0, "relations_moved": 0, "domains_moved": 0}
        merged_ids = set()  # Track already-merged entity IDs

        for keep_id, keep_name, merge_id, merge_name in merges:
            if merge_id in merged_ids:
                print(f"  Skip: {merge_name} (already merged)")
                continue
            if keep_id in merged_ids:
                print(f"  Skip: {keep_name} → {merge_name} (primary was merged into another entity)")
                continue
            stats = merge_entity(db, keep_id, keep_name, merge_id, merge_name, dry)
            action = "Would merge" if dry else "Merged"
            print(f"  {action}: {merge_name} → {keep_name}  "
                  f"({stats['facts_moved']}f moved, {stats['facts_skipped']}f skipped, "
                  f"{stats['relations_moved']}r, {stats['domains_moved']}d)")

            merged_ids.add(merge_id)
            for k in total_stats:
                total_stats[k] += stats[k]

        print(f"\n  Total: {total_stats['facts_moved']} facts moved, "
              f"{total_stats['facts_skipped']} skipped, "
              f"{total_stats['relations_moved']} relations re-pointed, "
              f"{total_stats['domains_moved']} domain assignments merged")

    # Prune orphans
    if do_prune:
        pruned = prune_orphans(db, dry)
        action = "Would prune" if dry else "Pruned"
        print(f"\n  {action} {pruned} orphan entities")

    if not dry:
        db.commit()

    # Summary
    entity_count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    fact_count = db.execute("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL").fetchone()[0]
    print(f"\n  DB now: {entity_count} entities, {fact_count} active facts")

    db.close()


if __name__ == '__main__':
    main()
