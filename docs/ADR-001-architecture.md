# ADR-001: Architecture — File-based Knowledge Management over MCP

**Date**: 2026-02-19
**Status**: Accepted
**Context**: Solving Claude Code session amnesia for a solopreneur's personal/business knowledge.

## Decision

Build a local, file-based knowledge management system using SQLite + auto-generated briefing files + CLI query tool. No MCP server.

## Rationale

### Why SQLite (not Postgres, not Notion, not flat files)
- **Zero infrastructure**: No server process, no Docker, no connection strings. Single file.
- **Local-first**: Runs on macOS without network dependency. Fast for personal scale (hundreds of facts, not millions).
- **Queryable**: Full SQL, full-text search, temporal queries. Flat files (MEMORY.md) can't do this.
- **Migratable**: Easy to export to Postgres or a hosted DB if this becomes a product later.
- Notion was considered but rejected: adds API latency for every query, schema is too rigid (page/DB model doesn't fit a knowledge graph), and it's already overloaded with KH Brain + Konban.

### Why no MCP server
- MCP tool definitions consume 500-1000+ tokens of context window per server, loaded every session.
- For a tool designed to *preserve* context quality, adding constant overhead is counterproductive.
- A briefing file (Tier 1) provides passive context at ~500 tokens — comparable to MEMORY.md.
- A CLI tool (Tier 2) can be called via Bash on-demand with zero persistent overhead.

### Why extraction pipeline (not real-time writes)
- The core failure mode is Claude *not writing* when told to. Instructions in CLAUDE.md to "update memory" are unreliable.
- An extraction pipeline runs *after* the session — it doesn't depend on Claude's cooperation during the session.
- A cheap model (Haiku 4.5 or Gemini Flash) processes the transcript and extracts structured facts.
- This is the only approach that's genuinely reliable for capture.

### Why temporal facts (not just latest state)
- "Marta is QA tester" → "Marta quit" is a state transition, not a replacement.
- History matters: "When did concierge matching get removed?" "What was Katherine's role before the Feb 2026 reorg?"
- Temporal validity (valid_from / valid_to) on facts enables both current-state queries and history queries.

## Alternatives Considered

| Alternative | Why rejected |
|------------|-------------|
| MCP-only | Token overhead, context window degradation |
| Notion as store | API latency, schema mismatch, already overloaded |
| Postgres | Infrastructure overhead for personal use |
| Enhanced MEMORY.md | Flat text, no temporal model, no querying, Claude still has to write |
| Vector DB + embeddings | Over-engineered for structured facts. Embeddings are for fuzzy retrieval of unstructured text. |

## Consequences

- Claude Code sessions gain a passive knowledge layer via BRIEF.md
- Fact capture depends on running extraction pipeline post-session (manual for MVP)
- No real-time writes during sessions — facts are captured after the fact
- System is self-contained: one repo, one DB file, three Python scripts
