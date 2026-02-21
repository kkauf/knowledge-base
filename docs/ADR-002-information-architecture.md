# ADR-002: Information Architecture — What Goes Where

**Date**: 2026-02-19
**Status**: Accepted
**Context**: The Knowledge Base is one of 7+ information systems. Without clear boundaries, every new fact triggers a "where does this go?" decision, which kills adoption.

## The Principle

Each system answers ONE question:

| Question | System | Examples |
|----------|--------|----------|
| **What is true right now?** | Knowledge Base (SQLite + BRIEF.md) | "Marta left", "Katherine: 10h/week", "Concierge matching: removed" |
| **What should I do today?** | Konban (Notion Kanban) | "Contact institutes", "Send proposal to Rouven" |
| **What's the KH strategy?** | Notion Brain (Active Context) | "Priority #1: therapist acquisition via institutes" |
| **What features are we building?** | Linear | "EARTH-123: Implement mandatory onboarding flow" |
| **When am I free?** | Google Calendar | Meetings, Make Blocks, European Window |
| **What did a client pay for?** | Google Drive | Proposals, contracts, deliverables |
| **What did I learn about a tool/pattern?** | MEMORY.md | "iCloud Glob fails", "use worktrees for parallel sessions" |

## Decision Rules

### Facts about entities → Knowledge Base

If it's a **state** about a person, company, feature, or project — and it would be true next week — it goes in the KB.

- "Katherine's role is Strategy & Support" → KB fact
- "Marta quit" → KB fact (supersedes "Marta is QA Tester")
- "Concierge matching feature removed" → KB decision
- "Therapist onboarding is now mandatory" → KB decision

### Things to do → Konban

If it dies when completed, it's a task.

- "Contact NARM institutes" → Konban task
- "Get testimonials from Luuse" → Konban task
- "Send booking link to therapist friend" → Konban task

### Strategic direction → Notion Brain

If it shapes priorities across multiple sessions, it's strategy.

- "Therapist acquisition is #1 priority" → Brain (Active Context)
- "We're removing concierge matching to simplify the platform" → Brain (Active Context decision section)
- "Roadmap: Stripe billing → Coach expansion → Native booking" → Brain

### Technical implementation → Linear

If it needs acceptance criteria and a developer, it's a dev task.

- "Build mandatory onboarding flow with 30min scheduling" → Linear (EARTH-xxx)
- "Remove concierge matching UI from platform" → Linear

### Records and deliverables → Google Drive

If it's a document someone else will read, or needs long-term archival.

- "Rouven Soudry notary proposal" → GDrive
- "Kerstin Kampschulte phase 2 scope" → GDrive
- "Katherine's KH role adjustment email" → GDrive (if worth archiving)

### Tool/workflow learnings → MEMORY.md

If it helps a future Claude session avoid a mistake or find a capability.

- "iCloud Glob fails, use explicit paths" → MEMORY.md
- "Use worktrees for parallel KH sessions" → MEMORY.md
- "Konban skill supports child pages under tasks" → MEMORY.md

## Overlap Resolution

### Decisions appear in both KB and Brain

This is intentional, not duplication:
- **KB decisions** are captured automatically by the extraction pipeline. They're structural facts: "X was decided on date Y."
- **Brain decisions** are curated by Konstantin during Kraken sessions. They include strategic context: "Why this matters for the roadmap."

The KB is the **ledger** (complete, automatic). The Brain is the **executive summary** (curated, contextual).

### MEMORY.md shrinks over time

Before the KB, MEMORY.md stored facts about people and projects (Oz's behavioral history, Marta's role, Katherine's therapist friend). Those migrate to the KB. MEMORY.md keeps only tool-usage patterns and workflow gotchas.

## Consequences

- Every session loads BRIEF.md passively — entity context is always available
- MEMORY.md is no longer the catch-all for "things to remember"
- Extraction pipeline is the primary write path for the KB (not manual)
- Konban tasks never store entity facts — they reference KB entities if needed

---

## Addendum: KB Recall Hook (2026-02-20)

### Architecture

The KB now has an **automatic retrieval layer** that bridges long-term storage and working memory:

```
User Message
    │
    ▼
[Activation Layer]    ← UserPromptSubmit hook (~10-50ms)
    │                    Entity name matching → SQLite lookup → context injection
    ▼
[Working Memory]      ← CLAUDE.md + MEMORY.md + BRIEF.md + injected recall
    │
    ▼
[LLM Processing]     ← Responds with full context, no visible lookup
    │
    ▼
[Consolidation]      ← Extraction daemon (launchd, every 30 min)
    │
    ▼
[Long-Term Store]    ← knowledge.db (SQLite)
```

**Three retrieval tiers:**

| Tier | Mechanism | Latency | Coverage |
|------|-----------|---------|----------|
| Always loaded | BRIEF.md (auto-generated summary) | 0ms | Top entities per domain, recent decisions, key metrics |
| Auto-primed | Recall hook (entity name matching + 1-hop neighbors) | 10-50ms | Any entity mentioned by name in user message |
| On-demand | `kb.py query/search` (explicit tool call) | ~200ms | Full KB (899+ entities) |

**Key files:**
- Hook script: `~/.claude/scripts/kb-recall.py`
- Entity name index: `~/.claude/knowledge/entity-index.json` (rebuilt by daemon)
- Debug log: `~/.claude/knowledge/recall-debug.log`

### Data Quality

**Problem:** The extraction LLM (Qwen 3.5) can hallucinate qualifiers. With the recall hook serving facts automatically, bad data has high blast radius.

**Mitigations (implemented):**
- Extraction prompt: "NEVER embellish or infer qualifiers not explicitly stated"
- Extraction prompt: skip assistant-echoed KB data (prevents feedback loop)
- Fuzzy dedup at write time (substring matching prevents rephrase drift)
- `kb.py correct` / `delete-fact` for manual corrections
- `kb.py recent` for periodic fact audit

**Feedback loop risk:** Claude echoes KB facts in responses → daemon re-extracts them → facts get re-dated or drift. Mitigated but not eliminated. `kb recent` audit is the safety net.

### Roadmap

**Phase 2 — Smarter BRIEF.md:**
Redesign to include all people entities, recent activity (30d), all active decisions. Target ~300-400 lines. Eliminates most explicit lookups for common entities.

**Phase 3 — Session entity tracking:**
Track which entities are "active" in the current conversation. Pre-load graph neighbors of discussed entities across turns (spreading activation), not just on the message that mentions them.

**Phase 4 — Visual fact review UI:**
VSS-style (graph visualization) interface for KB fact audit. Browse entities, review recent extractions, correct/confirm/delete facts visually. Candidate for `~/github/vss` adaptation or standalone tool. This addresses the scalability problem with text-based `kb recent` — at 1400+ facts and growing, a visual interface is needed.

**Phase 5 — Confidence scoring:**
Computed (not LLM-self-assessed) confidence per fact based on: source type (user-stated > assistant-derived), corroboration count (mentioned across N sessions), recency, specificity. Low-confidence facts excluded from recall hook. Facts that aren't corroborated in 30d decay below recall threshold.
