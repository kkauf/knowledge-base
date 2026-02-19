# ADR-003: Domain-Aware Extraction & Graph-Based Retrieval

**Date:** 2026-02-19
**Status:** Accepted (steps 1-4 implemented 2026-02-19, step 5 deferred)
**Context:** After backfilling 215 sessions (915 entities, 1273 facts), two problems surfaced:
1. BRIEF.md is 3,069 lines — unusable as passive context (target: ~50 lines)
2. Entity duplication — same real-world entity gets multiple names ("KEARTH", "KH", "Kaufmann Health") because the extraction model has no context about what already exists

## Decision

### 1. Brain Regions (Domains)

Entities belong to domains — clusters of related knowledge, like regions of the brain.

| Domain | Root entity | Examples |
|--------|------------|---------|
| Kaufmann Health | Kaufmann Health (company) | therapists, features, metrics, platform decisions |
| Personal | Konstantin Kaufmann (person) | family, Oz, health, identity, AuDHD |
| Consulting | Kaufmann Earth (company) | Rouven, Kerstin, proposals, client briefs |
| Infrastructure | — | tools, deployment, CI/CD, Claude skills |

Entities can span multiple domains (Katherine: Personal + KH). Relations define the graph edges.

**Domain detection:** Session project path → domain.
- `kaufmann-health` → KH
- `Personal-Support` → Personal
- `kkauf` → Blog/Personal
- Default → infer from content

**Implementation:** `entity_domains` junction table + domain detection from session source path.

### 2. Context-Aware Extraction

Before calling the extraction model, load the relevant subgraph:

1. Detect domain from session project path
2. Load entities within 2 hops of domain root (via relations table)
3. Include entity names + key current facts in the extraction prompt
4. Model reuses existing names, flags superseded values

This keeps prompt context focused (~100 entities, not 915) and eliminates name drift.

**Prompt addition:**
```
Known entities in this domain (reuse these names exactly):
- Kaufmann Health (company) [booking_value=€125, primary_conversion=Lead Verified]
- Cal.com (tool) [conversion_multiplier=5.5x, conversion_rate=2.03%]
...

If a fact updates an existing value, use the SAME entity name and attribute
so the database supersedes the old value automatically.
```

### 3. Hierarchical BRIEF.md

BRIEF.md becomes an index, not a dump:

```markdown
# Knowledge Brief (915 entities, 1273 facts)

## Domains
| Region | Entities | Latest change |
|--------|----------|---------------|
| Kaufmann Health | 142 | Cal 5.5x conversion (Feb 19) |
| Personal | 34 | Oz sanctuary search (Feb 19) |
| Consulting | 12 | Rouven proposal pending |
| Infrastructure | 45 | KB daemon live |

## Key Numbers
Cal booking rate: 2.03% | Message: 0.37% | Multiplier: 5.5x
...

## Recent Decisions (7d)
...

Deep lookup: kb.py query "entity" | kb.py domain "kh"
```

Target: ~50-80 lines. Points down, doesn't duplicate.

### 4. Nightly Reconciliation (Future)

A periodic job that cleans up extraction drift:

- **Entity dedup:** Find entities with similar names (fuzzy match), merge them
- **Contradiction detection:** Facts about the same thing with conflicting values
- **Orphan pruning:** Entities with no relations and no recent facts
- **Domain assignment:** Entities without a domain get assigned based on their relations

Pattern: Same as ledger reconciliation in payments. Extract fast and loose, reconcile periodically. Could use the same cheap model (Qwen) with a "here are 5 entities that look similar, which ones are the same?" prompt.

## Consequences

- Extraction quality improves as the graph grows (more context = better matching)
- BRIEF.md stays useful at any scale
- SQLite is sufficient — graph traversal via recursive CTEs, no need for Neo4j
- Reconciliation job prevents drift from accumulating

## Implementation Order

1. Hierarchical BRIEF.md (immediate value, no schema changes)
2. Domain assignment for existing entities (one-time migration from source field)
3. Context-aware extraction prompt (inject subgraph into prompt)
4. `kb.py domain` subcommand for domain-scoped queries
5. Reconciliation job (when entity count > ~200 unique)
