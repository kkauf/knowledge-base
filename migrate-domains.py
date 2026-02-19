#!/usr/bin/env python3
"""One-time migration: assign domains to existing entities based on their fact sources.

Each entity gets assigned to every domain it appears in, weighted by fact count.
Primary domain = most facts from that source pattern.

Usage:
    python3 migrate-domains.py          # Run migration
    python3 migrate-domains.py --dry    # Preview assignments
"""

import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH = os.path.expanduser("~/.claude/knowledge/knowledge.db")

# Same domain rules as briefing.py
DOMAIN_RULES = [
    ("KH", ["kaufmann-health", "kaufmann/health", "kaufmann%health"]),
    ("Personal", ["Personal-Support", "Personal/Support", "cornell", "email-katherine"]),
    ("VSS", ["vss"]),
    ("IsAI", ["IsAIConsciousYet", "isai"]),
    ("Infrastructure", ["claude-sessions", "knowledge-base", "kkauf"]),
]


def detect_domain(source: str) -> str:
    source_lower = source.lower()
    for domain, patterns in DOMAIN_RULES:
        for pat in patterns:
            if pat.lower() in source_lower:
                return domain
    return "Other"


def main():
    dry_run = "--dry" in sys.argv

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # Count facts per entity per domain
    entity_domains = defaultdict(lambda: defaultdict(int))  # entity_id -> {domain -> count}
    entity_names = {}

    for row in db.execute("""
        SELECT f.entity_id, f.source, e.name
        FROM facts f
        JOIN entities e ON f.entity_id = e.id
        WHERE f.source IS NOT NULL
    """):
        domain = detect_domain(row['source'])
        entity_domains[row['entity_id']][domain] += 1
        entity_names[row['entity_id']] = row['name']

    # Also check relations — entities only connected via relations get the domain of their related entities
    # (skip for now, handle in reconciliation)

    total_assignments = 0
    domain_counts = defaultdict(int)

    if dry_run:
        print("DRY RUN — no changes will be made\n")

    for entity_id, domains in sorted(entity_domains.items(), key=lambda x: entity_names.get(x[0], '')):
        total_facts = sum(domains.values())
        name = entity_names.get(entity_id, entity_id)

        for domain, count in domains.items():
            confidence = round(count / total_facts, 2) if total_facts > 0 else 0.5

            if dry_run:
                marker = " ★" if confidence >= 0.5 else ""
                print(f"  {name:40s} → {domain:15s} ({count}/{total_facts} facts, conf={confidence}){marker}")
            else:
                db.execute("""
                    INSERT OR REPLACE INTO entity_domains (entity_id, domain, confidence, source)
                    VALUES (?, ?, ?, 'migration')
                """, (entity_id, domain, confidence))

            total_assignments += 1
            domain_counts[domain] += 1

    if not dry_run:
        db.commit()

    print(f"\n{'Would assign' if dry_run else 'Assigned'} {total_assignments} domain memberships:")
    for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
        print(f"  {domain}: {count} entities")

    # Entities with no facts (and thus no domain)
    orphans = db.execute("""
        SELECT e.id, e.name FROM entities e
        WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.entity_id = e.id)
    """).fetchall()

    if orphans:
        print(f"\n{len(orphans)} entities with no facts (no domain assigned):")
        for o in orphans[:10]:
            print(f"  {o['name']}")
        if len(orphans) > 10:
            print(f"  ... and {len(orphans) - 10} more")

    db.close()


if __name__ == '__main__':
    main()
