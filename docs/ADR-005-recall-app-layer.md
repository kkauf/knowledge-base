# ADR-005: Recall App Layer — From Regex Matching to 3-Tier Memory Retrieval

**Date**: 2026-02-23
**Status**: Active
**Context**: The KB recall hook (`kb-recall.py`) uses deterministic regex matching against entity names. When users refer to entities by shorthand, oblique reference, or natural language ("trademark filing" vs "KAUFMANN HEALTH Trademark"), the hook misses and Claude gets zero context — complete amnesia.

## Problem

The recall hook indexes 1496 entity names and matches them via word-boundary regex against user messages. This works for exact name mentions ("Pintz & Partners", "Katherine") but fails for:
- **Shorthand**: "trademark filing" → entity is "KAUFMANN HEALTH Trademark"
- **Oblique references**: "the company handling our EU filing"
- **Attribute references**: "that €170 thing"

When the hook misses, no context is injected. Claude has no reflex to check the KB itself, so it asks questions that the system already knows the answer to. This is the primary source of "amnesia" across sessions.

## Architectural Insight

OpenClaw (steipete) solves this with a 700-line `MemoryIndexManager` using vector embeddings (70%) + BM25 (30%), union merge, MMR re-ranking, and temporal decay. They need it — they run across WhatsApp/Slack/Telegram with no control over the LLM layer.

We have something they don't: **Claude Code's instruction layer.** CLAUDE.md loads every session. We can teach the LLM a behavioral reflex — "check your memory before asking" — and the LLM's own semantic reasoning bridges the gap that OpenClaw solves with embeddings.

**The app layer is an instruction, not a service.**

## Decision

Implement a 3-tier recall architecture:

```
User Message
    │
    ▼
┌──────────────────────────────────────┐
│ TIER 1: Deterministic Hook (~10ms)   │
│ Exact name + aliases + FTS5 fallback │
│ Zero LLM cost. High precision.       │
└──────────────┬───────────────────────┘
               │ (inject if matched)
               ▼
┌──────────────────────────────────────┐
│ TIER 2: App Layer (instruction)      │
│ "Before asking clarifying Qs about   │
│  prior-session context, search KB"   │
│ LLM's own semantic reasoning.        │
│ Cost: 1 tool call when triggered.    │
└──────────────┬───────────────────────┘
               │ (self-retrieved context)
               ▼
┌──────────────────────────────────────┐
│ TIER 3: Full LLM Reasoning           │
│ All context + full tool access.      │
└──────────────────────────────────────┘
```

### Implementation Phases

| Phase | What | Effort | Coverage |
|-------|------|--------|----------|
| 0 | Instruction reflex in CLAUDE.md | 30 min | ~60% |
| 1 | Alias index for entities | 2-3h | ~80% |
| 2 | FTS5 fallback search in hook | Half day | ~90% |
| 3+ | Vector embeddings (if needed) | Days | ~95%+ |

Phase 0 + 1 likely sufficient. Phase 2 is insurance. Phase 3+ probably never needed.

### OpenClaw Layer Mapping

| OpenClaw | Ours | Status |
|----------|------|--------|
| L1-3: Always-loaded (SOUL/AGENTS/MEMORY) | SOUL.md + CLAUDE.md + MEMORY.md | Done |
| L4: facts.db + 275 aliases | knowledge.db (0 aliases) | Phase 1 |
| L5: Hybrid semantic search | Instruction reflex + FTS5 | Phase 0+2 |
| L10-12: Plugins (graph-memory) | Recall hook + extraction daemon | Done |
| Activation/decay (Hot/Warm/Cool) | All facts treated equally | Future |
| Pre-compaction flush | Timer-based daemon (30 min) | Future |

## Testing Strategy

Each phase has a concrete test: run `kb-recall.py --test` against known-failing queries.

**Test cases** (all should match "KAUFMANN HEALTH Trademark" or "Pintz & Partners"):
- `"Trademark filing"` — shorthand (Phase 1: alias match)
- `"the trademark thing"` — colloquial (Phase 1: alias match)
- `"Pintz response"` — partial entity name (already works)
- `"EU filing update"` — oblique (Phase 2: FTS5 on "EU" + "filing" in facts)
- `"that €170 thing"` — attribute reference (Phase 2: FTS5 on "170" in facts)

## References

- OpenClaw architecture: steipete/openclaw, docs.openclaw.ai/concepts/memory
- 12-layer community extension: coolmanns/openclaw-memory-architecture
- Substack analysis: ppaolo.substack.com/p/openclaw-system-architecture-overview
- Architecture diagram: /tmp/kb-architecture.html
