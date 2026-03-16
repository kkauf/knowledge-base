# ADR-007: Semantic Session Memory

**Date**: 2026-03-09
**Status**: Partially Implemented (category taxonomy shipped Mar 10; session memory still proposed)
**Triggered by**: Resistance band amnesia incident — Claude gave generic advice on equipment it had recommended, researched, and helped order 6 days earlier.

## Problem

The fact extraction pipeline (ADR-002, ADR-003) decomposes sessions into entity-attribute-value triples. This is the wrong abstraction for personal knowledge:

1. **Lossy by design.** An LLM decides which facts are "important enough" to extract. Each new category of missed knowledge (purchases, dates, events) requires prompt engineering. We're playing whack-a-mole.

2. **Destroys narrative.** "Konstantin researched and ordered REP tube resistance bands for shoulder rehab — they arrived, they're too long for pull-aparts without choking up" is a story. Decomposing it into 8 `entity.attribute = value` triples loses the arc and requires reassembly.

3. **Recall depends on entity names.** The recall hook matches entity names in user messages. "How do I use the resistance bands?" didn't match "REP Resistance Bands" until we added fuzzy matching and component-word aliases — behavioral and code fixes for an architectural gap.

4. **Context bloat.** Every recalled entity injects facts + 1-hop neighbors. A query mentioning "Konstantin" dumps 10+ relations and 10+ facts. Most are irrelevant to the current question. More context = worse reasoning.

Meanwhile, structured facts work well for what they are: "Mason is Katherine's brother", "Luise works for Kaufmann Health", "Kampschulte engagement: Phase 2 active." Entity-attribute-value is the right model for entities. It's the wrong model for sessions.

## Decision

Add a **semantic session memory** layer alongside (not replacing) the structured KB.

### Two layers, two jobs

| Layer | Stores | Model | Recall mechanism |
|-------|--------|-------|-----------------|
| **Structured KB** (existing) | Entity facts, relations, decisions | Entity-attribute-value triples | Entity name matching + FTS5 |
| **Session memory** (new) | What happened, what was decided, what was learned | Topic-organized prose summaries | Embedding similarity |

The structured KB answers: "Who is Luise?" "What's Kampschulte's rate?"
Session memory answers: "What do I know about the resistance bands?" "What's the Sky Hill Farm situation?"

### Session summary format

Not fixed-length. Not 1 sentence. Variable prose organized by topic, proportional to signal density:

- Alert-fix session → empty (nothing worth remembering)
- Personal planning session → 100-300 words of topic-organized paragraphs
- KH strategy session → key decisions, document locations, commitments

Example (from the session that triggered this ADR):

```
Katherine teaches NARM modules online (not in-person) for the NARM Institute Complex Trauma
training. This is a teaching role, not student.

Washington Island 2026: Steve and Suzanne's cabin is available for Konstantin and Katherine's
stay. A second cabin exists but will be mostly occupied by Katherine's aunt and uncle over
summer. Steve and Suzanne plan to go to Washington Island late August into September. They
also hope to attend an August 8 wedding in Greece.

Mason is Katherine's brother. He's tentatively visiting New Woodstock for July 4th, and has
talked about early August for Washington Island (unconfirmed).

Sky Hill Farm move timeline: Realistically mid-to-late May 2026, not April. Kitchen going in
now, interior doors weeks out, bathrooms unfinished (no tiles/vanities). Steve estimates
"second half of May, god willing." Hard deadline: house must be clear by June 22 (ECCB
dancers arrive).

Konstantin's 2026 yin agenda: Water activities — rowing club, swimming, sailing lessons. Buy
a cheap motorbike and take a lesson. South America trip deferred to winter (too hot in
summer). These are regulation/identity priorities, not productivity goals.

NARM Module 1 (Apr 16-19) childcare plan: Leah in daycare Thu-Fri, Konstantin covers Sat-Sun.
Steve & Suzanne unavailable that weekend (Buffalo). Likely canceling SingleCut shift Saturday
Apr 18.
```

Note what this captures that fact extraction missed across 3 attempts:
- The narrative arc (move timeline → hard deadline → childcare cascade)
- The "why" behind decisions (yin agenda = regulation, not productivity)
- Tentative vs confirmed status (Mason "talked about" vs ECCB "arrives")
- Document-level context (NARM = teaching role, not student)

### Fact extraction continues

Session summaries do NOT replace entity fact extraction. Both run on each session:

- **Fact extraction** feeds the structured KB → entities, relations, decisions. "Mason is Katherine's brother" belongs here. Structured, queryable, deduped.
- **Session summaries** feed the semantic memory → prose context for recall. "Washington Island cabin situation in summer 2026" belongs here. Narrative, searchable by meaning.

The extraction prompt can become more conservative — focus on genuine entity facts and relations, stop trying to force narrative knowledge into triples. Categories like "PERSONAL ASSETS & PURCHASES" and temporal event tracking in the extraction prompt are stopgaps; they migrate to session summaries.

### Recall architecture

```
User Message
    │
    ├──[Entity matching]──→ Structured KB ──→ Entity facts (existing, ~10-30ms)
    │
    └──[Embed message]────→ sqlite-vec ────→ Top-3 session summaries (~150-250ms)
    │
    ▼
[Merge & inject as system-reminder]
    │
    ▼
[LLM Processing]
```

**Injection format — pointers, not payloads:**

```
Session context (semantic match):
- [Mar 2] Ordered REP tube resistance bands for shoulder rehab, tested exercises
- [Mar 8] Summer planning: Washington Island trip, Sky Hill Farm move timeline, NARM childcare
```

Two lines. ~30 tokens. Claude sees the pointers and decides whether to read the full summary. Full summaries available via a tool call (e.g., `kb.py session 2026-03-08` or similar).

**Why pointers, not full summaries**: Every injected token competes with reasoning quality. The recall hook's job is to prevent amnesia ("Do I already know something about this?"), not to front-load all context. The LLM can pull details when it needs them.

### KH strategy sessions

KH dev sessions are mostly code — low memory value. But KH strategy sessions (Kraken mode) produce valuable context:

- Key decisions and their rationale
- Document locations ("SaaS pricing analysis lives in Brain page 'Strategy'")
- Metric snapshots and what they mean
- Commitments made to therapists/partners

These get summaries too. The summary naturally captures "Konstantin stores strategy decisions in Notion Brain under 'Strategy'" — which is exactly the kind of navigational knowledge that helps future sessions find things.

## Implementation

### Storage

```sql
-- In knowledge.db (or separate session-memory.db)
CREATE TABLE session_summaries (
    id TEXT PRIMARY KEY,
    session_path TEXT NOT NULL,
    project TEXT,           -- project path (for domain detection)
    domain TEXT,            -- detected domain (Personal, KH, Consulting)
    summary TEXT NOT NULL,  -- topic-organized prose
    one_liner TEXT,         -- compressed version for recall injection
    token_count INTEGER,    -- summary length for budgeting
    session_date TEXT,      -- ISO date
    created_at TEXT
);

-- sqlite-vec for embedding search
CREATE VIRTUAL TABLE session_embeddings USING vec0(
    id TEXT PRIMARY KEY,
    embedding FLOAT[1536]   -- text-embedding-3-small dimensions
);
```

### Pipeline

1. **Summarize** (async, after session ends): Send transcript to LLM with prompt:
   "Summarize new personal knowledge, decisions, and context from this session. Organize by topic. Skip pure code/ops work. If nothing worth remembering, return EMPTY."
   Model: same cheap model as extraction (Qwen 3.5 or equivalent).

2. **Embed** (after summary): Embed the summary text via OpenAI text-embedding-3-small ($0.02/M tokens). ~$0.00001 per session.

3. **Store**: Insert summary + embedding into SQLite.

4. **Recall hook integration**: Add a parallel path in `kb-recall.py`:
   - Embed user message (~100-150ms via API, or use a local model)
   - Vec search top-3 similar summaries
   - Inject one-liners into the same system-reminder

### Cost

| Component | Per session | 1000 sessions |
|-----------|-----------|---------------|
| Summary generation (Qwen 3.5 via OpenRouter) | ~$0.004 | $4 |
| Embedding (text-embedding-3-small) | ~$0.00001 | $0.01 |
| Storage (SQLite) | 0 | 0 |
| **Total** | ~$0.004 | ~$4 |

Recall cost: one embedding per user message (~$0.00001). Negligible.

### Latency budget

| Step | Time |
|------|------|
| Embed user message | 100-200ms (API) or 10-30ms (local model) |
| Vec search top-3 | 1-5ms |
| Format one-liners | <1ms |
| **Total added to recall hook** | ~100-200ms |

Current recall hook: 10-30ms. With semantic search: 110-230ms. Still well under perceptible delay.

### Migration path

1. **Phase 1**: Build summary + embed pipeline. Run alongside extraction. No recall integration yet — just accumulate summaries.
2. **Phase 2**: Integrate into recall hook as parallel path. Test quality of semantic matches.
3. **Phase 3**: Tune extraction prompt to be more conservative on personal knowledge (let summaries handle it). Entity facts and relations stay.
4. **Phase 4**: Backfill — generate summaries for historical sessions (same batch approach as current backfill).

## Open Questions

- **Embedding model choice**: text-embedding-3-small (cheap, good enough?) vs text-embedding-3-large (better quality, 2x cost) vs local model (no API dependency, but quality tradeoff).
- **Recall hook latency**: 100-200ms for embedding API call may be too slow. A local embedding model (e.g., all-MiniLM-L6-v2 via sentence-transformers) would be 10-30ms but lower quality. Could cache embeddings of common user message patterns.
- **Summary model**: Qwen 3.5 is the current extraction model. Is it good enough for summaries, or do we need a model with better narrative judgment?
- **Dedup across sessions**: Same topic discussed across multiple sessions (e.g., Washington Island in 3 sessions). Do summaries accumulate, or does a newer summary supersede? Probably accumulate — each session adds new context. The recall hook returns the most relevant, not all.
- **Privacy**: Session summaries contain personal information. Same security posture as knowledge.db — local SQLite, never transmitted except via the embedding API call. Consider local embedding model if this is a concern.

## Addendum: Structured Category Taxonomy as Cross-Cutting Disambiguation Layer (2026-03-09)

**Triggered by**: Daemon marked "Personal — Backup Payment Method for KH Ops" as done based on a supplement ordering artifact. Root cause: LLM fuzzy-matched "ordering supplements ($66.33)" to "backup payment method" — both Personal domain, both transactional keywords.

### Principle

Disambiguation is an LLM problem, not a code problem. But the current KB has a flat domain system (Personal, KH, Infrastructure, Consulting) that's too shallow. "Personal" contains baking, sports, health, finances, family, home — everything collides at the top level.

This affects **every system that needs to match or route**:

| System | What collides | Current failure mode |
|--------|--------------|---------------------|
| **Reconciliation** | Artifact → Konban task | Supplement order matched to payment method task (both "Personal" + transactional) |
| **Fact extraction** | Fact → entity | Facts about different projects in same domain attached to wrong entity |
| **Recall** | User message → relevant facts | "Personal" query dumps health + finances + family facts indiscriminately |

The fix isn't per-system — it's a shared **category taxonomy** that all three systems use for structured narrowing.

### Taxonomy (Seed — Grows Organically)

```
Personal
├── Health (supplements, rehab, exercise, medication)
├── Sports (rowing, swimming, sailing)
├── Baking (recipes, equipment, sourdough)
├── Family (Katherine, Leah, in-laws, Steve & Suzanne)
├── Home (Sky Hill Farm, furniture, renovation)
├── Finance (banking, insurance, taxes, payments)
├── Education (MBA, Quantic)
└── Travel (Washington Island, South America)

Business
├── Kaufmann Health
│   ├── Product (features, UX, matching)
│   ├── Therapists (onboarding, communication, profiles)
│   ├── Billing (invoicing, Stripe, commissions)
│   ├── Marketing (outreach, ads, website)
│   └── Infrastructure (deploy, CI, staging)
├── Consulting
│   ├── Kampschulte
│   └── [other clients]
└── Kaufmann Earth (LLC admin, legal, brand)
```

This is NOT a rigid ontology. It's a **disambiguation hint** — enough structure for an LLM to say "this is Personal/Health, not Personal/Finance" without needing exact category IDs. New sub-categories emerge naturally as content is classified.

### Design: Progressive Structured Matching

Use cheap structured round-trips to narrow before matching. The pattern applies to **all three systems**, not just reconciliation:

```
Input (artifact / fact / user message)
  │
  ├─ Step 1: Classify (cheap LLM, Flash Lite)
  │    → category: Personal / Health
  │    → sub-category: Supplements
  │    → key terms: ["Micro Ingredients", "order #4263254", "supplement stack"]
  │
  ├─ Step 2: Filter candidates (code, deterministic)
  │    → Narrow to items in same category/sub-category
  │    → Result: 3-5 candidates, not 30
  │
  └─ Step 3: Match / Route (LLM, with full context of narrowed set)
       → Confident match against small candidate set
       → No match → no_action (fail safe)
```

**For reconciliation**: classify artifact → filter Konban tasks by category → match against 3-5 tasks.
**For fact extraction**: classify fact → filter entities by category → attach to correct entity.
**For recall**: classify user message → filter facts/summaries by category → inject relevant subset.

**Balance between efficiency and accuracy**: Not the whole tree (expensive, noisy), not just one level (too shallow). Two levels of progressive reveal is the sweet spot — enough structure to disambiguate, cheap enough to run on every operation.

### Why this extends ADR-007

The same architectural insight applies across the KB:

- **Session memory** (ADR-007 main): Don't force narrative knowledge into entity-attribute-value triples. Use the right abstraction (prose summaries + embedding search).
- **Structured categorization** (this addendum): Don't force semantic disambiguation into flat domain labels or code-based keyword matching. Use the right abstraction (hierarchical categories assigned by cheap LLM + deterministic filtering).

Both cases: let LLMs do LLM work, but give them structure so they do it well.

### Implementation Notes

- Step 1 classification reuses the cheap model (Flash Lite, ~$0.25/M tokens). One call per artifact/fact/message.
- Step 2 filtering is pure code — category intersection against metadata on Konban tasks, entities, or facts.
- Step 3 matching stays LLM but with dramatically narrowed candidate sets.
- **Category assignment at creation**: When creating Konban tasks or extracting entities, classify at write-time. Cheaper than re-classifying at every read.
- **Taxonomy lives in the LLM prompt**, not in code. The seed taxonomy above gives the model enough structure to classify consistently, but new sub-categories can emerge without code changes.
- Fail-safe: 0 candidates after filtering → `no_action`. No guessing.

### Implementation Status (Mar 10, 2026)

**Shipped (category taxonomy — Phase 1 of the addendum):**
- `artifact_extract.py`: category/sub_category/key_terms on every artifact. Disambiguation rules: session project ≠ artifact category. Garbage title filter.
- `pipeline_reconcile.py`: Category-aware matching in reconciliation prompt + domain preamble enriched with category data. Action schema includes category/sub_category pass-through.
- `executor.py`: Daemon-created tasks prefixed with `[Category/Sub]` in title.
- `extract.py`: Canonical source principle with enumerated guardrails. `lookup_path` attribute format for routing pointers. Metric number stripping in hybrid decision/metric facts.
- `kb-recall.py`: `lookup_path` attributes surfaced as `⚡ QUERY SOURCE:` in recall output.
- `seed-lookup-paths.py`: Deterministic seeding of 48 lookup_paths (therapists→Supabase, clients→GDrive, metrics→Metabase). Hooked into daemon (runs after each extraction cycle).
- `validate-taxonomy.py`: Parallel validation harness (8 workers, 50 sessions in ~7min).

**Validation results (10-session smoke test):**
- Category hit rate: 100% on new extractions
- Disambiguation: State Farm personal insurance correctly excluded from Business/KH/Billing
- Garbage titles: "?" titles filtered by both prompt instruction and code post-processing
- Metric extraction: 24→4 facts on metric-heavy session (6x reduction). ~20% residual metric leak in hybrid facts.

**Known limitations:**
- Qwen 3.5 (extraction model) can't reliably apply principles — needs explicit examples. Enumerated source list restored alongside the principle.
- Qwen 3.5 doesn't generate `lookup_path` routing pointers — deterministic seeding covers the gap.
- Category hit rate was 44% on the 50-session backfill (cold start with new schema). Will be 100% going forward.

**Shipped (skill-fix routing — Mar 16, 2026):**
- `artifact_extract.py`: Dynamic skill inventory injected into extraction prompt. LLM can only assign error_patterns to real skills (or null). Discovers valid skills from filesystem (`SKILL.md` exists).
- `pipeline_reconcile.py`: Nonexistent skill names surfaced as "NONEXISTENT SKILLS" warning in reconciliation state. Prompt rule forces `no_action` for these.
- `executor.py`: Hard guard blocks `fix_skill` for skills without `SKILL.md`. 14-day TTL cleanup on skill proposals.
- Root cause: extraction LLM had no skill inventory, so it inferred category-like names ("infrastructure", "supabase", "knowledge-base"). 22 of 27 pending fixes were waste.

**Shipped (session pre-filter — Mar 16, 2026):**
- `session_prefilter.py`: Deterministic transcript compression per pipeline stage. Zero LLM cost.
  - `quick_classify()` gates which stages run: skips fact extraction for tiny subagent sessions (<1500 user chars), skips artifact extraction when no long assistant messages.
  - `filter_for_facts()`: "Gems" pattern — full user messages + compressed assistant messages (first/last paragraphs). 35% smaller transcripts.
  - `filter_for_artifacts()`: Only long assistant messages (>1500 chars) + triggering user context. 54% smaller transcripts.
- `extract.py`: Uses fact filter, drops context frame (saves ~4K tokens/call — context frame only benefits artifact extraction).
- `artifact_extract.py`: Uses artifact filter.
- `kb-extract-daemon.sh`: Classification gate before LLM calls. Sessions that skip both stages advance offsets without any API calls.
- Design from session-picker's `_extract_gems()` pattern: first + last text blocks carry signal, verbose middles are noise.
- Impact: 29% fewer LLM calls + smaller transcripts. $41/month → ~$20/month (51% reduction). Quality verified identical across 4 test sessions.

**Not shipped (session memory — main ADR-007):**
- Session summaries, embedding storage, semantic recall still proposed. The category taxonomy was the prerequisite disambiguation layer.

## Consequences

- Session memory becomes category-agnostic — no more "add purchases to extraction prompt" when a new type of personal knowledge is missed.
- Recall quality improves for fuzzy/narrative queries ("what's the situation with the house?") without degrading structured queries ("who is Luise?").
- Context injection stays lean — pointers, not payloads.
- Fact extraction narrows to its strength: entity facts, relations, decisions. Stops trying to be a general-purpose memory system.
- **Structured categories eliminate the class of cross-domain collision bugs** (supplement→payment, exercise→work, order→order) across reconciliation, extraction, and recall.
