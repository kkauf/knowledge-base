#!/usr/bin/env python3
"""Seed lookup_path attributes on KB entities with known queryable data sources.

Deterministic — no LLM calls. Adds routing pointers so the recall hook tells
Claude WHERE to look up dynamic data instead of trusting stale cached values.

Usage:
    python3 seed-lookup-paths.py              # Dry-run (show what would be added)
    python3 seed-lookup-paths.py --write      # Write to DB
    python3 seed-lookup-paths.py --stats      # Show current lookup_path coverage
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_db_path

DB_PATH = str(get_db_path())


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


# --- Routing rules ---
# Each rule: (entity_filter_fn, attribute, value_template)
# entity_filter_fn receives (entity_name, entity_type, facts_dict) and returns True if applicable
# value_template can use {name} for entity name

ROUTING_RULES = [
    # Therapists → Supabase session data
    # Only KH platform therapists (have platform-specific facts like profile, Stripe, commission)
    {
        "name": "therapist_sessions",
        "attribute": "lookup_path",
        "filter": lambda name, etype, facts: (
            etype == "person"
            and any(kw in v.lower() for v in facts.values()
                    for kw in ("therapist on kh", "therapist on kaufmann", "platform therapist",
                               "verified therapist", "onboarded"))
            or (etype == "person"
                and any(kw in k.lower() for k in facts.keys()
                        for kw in ("stripe_status", "profile_complete", "cal_event_type",
                                   "commission", "onboarding")))
            and not any(kw in name.lower() for kw in ("test", "qa", "sister", "wife"))
        ),
        "value": "session data → Supabase: SELECT * FROM cal_bookings WHERE therapist_name ILIKE '%{name_short}%'",
    },
    # Therapists → billing/invoice status
    {
        "name": "therapist_billing",
        "attribute": "lookup_path_billing",
        "filter": lambda name, etype, facts: (
            etype == "person"
            and any(kw in k.lower() for k in facts.keys()
                    for kw in ("commission", "invoice_status", "stripe_status",
                               "stripe_account", "billing_status"))
            and not any(kw in name.lower() for kw in ("test", "qa", "sister", "wife"))
        ),
        "value": "billing status → Stripe dashboard (search by therapist email) + Konban invoicing task",
    },
    # Consulting clients → GDrive relationship log
    {
        "name": "consulting_client",
        "attribute": "lookup_path",
        "filter": lambda name, etype, facts: (
            any(k in ("drive_path", "relationship_log", "brief_filename") for k in facts.keys())
        ),
        "value": "engagement status → GDrive relationship log: gdrive-api.py search 'Relationship Log {name_short}'",
    },
    # Google Ads entity → Metabase (only the main entity, not every sub-concept)
    {
        "name": "google_ads",
        "attribute": "lookup_path",
        "filter": lambda name, etype, facts: (
            name.lower() in ("google ads", "google ads account")
        ),
        "value": "campaign metrics → Metabase: npx tsx scripts/metabase-pull.ts --only 'U-CAC,D-IntroToSession'",
    },
    # KH platform metrics → Metabase
    {
        "name": "kh_metrics",
        "attribute": "lookup_path_metrics",
        "filter": lambda name, etype, facts: (
            etype in ("company", "project", "concept")
            and any(kw in name.lower() for kw in ("kaufmann health", "kh platform", "kh therapy"))
            and any(kw in k.lower() for k in facts.keys()
                    for kw in ("conversion", "revenue", "metric", "rate", "cpl", "cac", "clv"))
        ),
        "value": "KPIs → Metabase: npx tsx scripts/metabase-pull.ts --days 30 (all funnel + unit economics)",
    },
    # Cal.com / booking entities → Supabase
    {
        "name": "cal_bookings",
        "attribute": "lookup_path",
        "filter": lambda name, etype, facts: (
            "cal.com" in name.lower() or "cal_bookings" in name.lower()
            or (etype == "tool" and "booking" in name.lower())
        ),
        "value": "booking data → Supabase: SELECT * FROM cal_bookings (join with therapists for names)",
    },
    # Linear-tracked features → Linear API
    {
        "name": "linear_features",
        "attribute": "lookup_path",
        "filter": lambda name, etype, facts: (
            etype == "feature"
            and any("earth-" in v.lower() or "linear" in k.lower() for k, v in facts.items())
        ),
        "value": "dev status → Linear: python3 ~/.claude/skills/linear/linear-api.py board (search by title)",
    },
]


def get_first_name(full_name: str) -> str:
    """Extract first name or short identifier from entity name."""
    parts = full_name.split()
    if len(parts) >= 2:
        return parts[0]
    return full_name


def seed_lookup_paths(db, dry_run: bool = True) -> dict:
    """Apply routing rules to all entities, return stats."""
    entities = db.execute("SELECT id, name, type FROM entities").fetchall()

    stats = {"checked": 0, "already_has": 0, "added": 0, "rules_matched": {}}

    for e in entities:
        eid, name, etype = e["id"], e["name"], e["type"]

        # Load facts for this entity
        facts_rows = db.execute(
            "SELECT attribute, value FROM facts WHERE entity_id = ? AND valid_to IS NULL",
            (eid,),
        ).fetchall()
        facts = {r["attribute"]: r["value"] for r in facts_rows}
        stats["checked"] += 1

        name_short = get_first_name(name)

        for rule in ROUTING_RULES:
            try:
                if not rule["filter"](name, etype, facts):
                    continue
            except Exception:
                continue

            attr = rule["attribute"]

            # Check if already exists
            existing = db.execute(
                "SELECT value FROM facts WHERE entity_id = ? AND attribute = ? AND valid_to IS NULL",
                (eid, attr),
            ).fetchone()

            if existing:
                stats["already_has"] += 1
                continue

            value = rule["value"].format(name=name, name_short=name_short)
            rule_name = rule["name"]
            stats["rules_matched"].setdefault(rule_name, [])
            stats["rules_matched"][rule_name].append(name)

            if dry_run:
                print(f"  [+] {name}.{attr} = {value}")
            else:
                import uuid
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                db.execute(
                    "INSERT INTO facts (id, entity_id, attribute, value, source, valid_from, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), eid, attr, value,
                     "seed-lookup-paths", now, now),
                )
                stats["added"] += 1
                print(f"  [✓] {name}.{attr} = {value}")

    if not dry_run:
        db.commit()

    return stats


def show_stats(db):
    """Show current lookup_path coverage."""
    total_entities = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    with_lookup = db.execute("""
        SELECT COUNT(DISTINCT e.id) FROM entities e
        JOIN facts f ON e.id = f.entity_id
        WHERE f.attribute LIKE 'lookup_path%' AND f.valid_to IS NULL
    """).fetchone()[0]

    all_lookups = db.execute("""
        SELECT e.name, e.type, f.attribute, f.value FROM facts f
        JOIN entities e ON f.entity_id = e.id
        WHERE f.attribute LIKE 'lookup_path%' AND f.valid_to IS NULL
        ORDER BY e.type, e.name
    """).fetchall()

    print(f"Entities with lookup_paths: {with_lookup}/{total_entities}")
    print()
    current_type = ""
    for r in all_lookups:
        if r["type"] != current_type:
            current_type = r["type"]
            print(f"  [{current_type}]")
        print(f"    {r['name']}.{r['attribute']} = {r['value'][:90]}")


def main():
    parser = argparse.ArgumentParser(description="Seed lookup_path attributes on KB entities")
    parser.add_argument("--write", action="store_true", help="Write to DB (default: dry-run)")
    parser.add_argument("--stats", action="store_true", help="Show current lookup_path coverage")
    args = parser.parse_args()

    db = get_db()

    if args.stats:
        show_stats(db)
        db.close()
        return

    print("Scanning entities for lookup_path candidates...\n")
    dry_run = not args.write
    stats = seed_lookup_paths(db, dry_run=dry_run)

    print(f"\nResults:")
    print(f"  Entities checked: {stats['checked']}")
    print(f"  Already had lookup_path: {stats['already_has']}")
    print(f"  {'Would add' if dry_run else 'Added'}: {stats['added'] if not dry_run else sum(len(v) for v in stats['rules_matched'].values())}")
    print()
    for rule, entities in stats["rules_matched"].items():
        print(f"  Rule '{rule}': {len(entities)} matches")
        for name in entities[:5]:
            print(f"    - {name}")
        if len(entities) > 5:
            print(f"    ... and {len(entities) - 5} more")

    if dry_run:
        print("\n[DRY RUN — use --write to persist]")

    db.close()


if __name__ == "__main__":
    main()
