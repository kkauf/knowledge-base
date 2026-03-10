# ADR-008: Session-Task Linking

**Date**: 2026-03-10
**Status**: MVP Shipped — Phase 1 complete, future phases proposed
**Triggered by**: Claudine.pro — a VS Code extension that makes kanban cards = sessions, eliminating the extraction gap entirely. We don't want their model (our knowledge needs to cross sessions and outlive tasks), but the insight — sessions as first-class objects tied to tasks — eliminates the hardest inference problem in the pipeline.

## Problem

Claude Code sessions are ephemeral JSONL files with no structural link to the work they represent. This causes two compounding problems:

1. **Category inference is the hardest step in extraction.** The daemon must read raw conversation content and guess: "What is this about? What category? What entities?" The LLM gets it wrong ~20% of the time on cold-start sessions, and is biased toward Business/KH when seeing billing/insurance keywords (ADR-007 addendum documents the supplement→payment collision).

2. **Session discoverability is poor.** Finding "the session where we discussed X" means searching chronological JSONL files by modification date. The session packer exists as a workaround, but it's still a flat list. "Show me sessions for the Stripe setup task" is not possible.

## Insight

Claudine's model: kanban card = session. Context lives where it was born — no extraction needed. Our model: sessions are write-only inputs to a distributed knowledge graph. Knowledge must be lifted OUT of sessions into durable stores, then re-injected into future sessions.

The gap between these models is the extraction pipeline. Session-task linking bridges it: if a session declares its task, the pipeline inherits task metadata instead of inferring it.

## Decision

Add **bidirectional links between Claude Code sessions and Konban tasks**.

### What the link propagates (inherited, not inferred)

| From Konban task | Daemon gets for free |
|------------------|---------------------|
| Task title | Scope boundary for relevance |
| Task status (Done/Doing) | Staleness signal — Done sessions are historical |
| Task priority | Urgency context |
| Linked roadmap item (via Konban→Brain chain) | Facts auto-associate to right feature |

The existing knowledge graph chain: Session → Konban task → Roadmap item → Brain doc → Category taxonomy. Each link is already established; session-task linking adds the first edge.

### Unlinked sessions

Not every session will be linked. Ad-hoc conversations (standup, personal support, exploration) don't need linking. The daemon falls back to current inference for unlinked sessions. Over time, the linking rate is a signal of workflow discipline.

## Implementation — Phase 1 (Shipped)

### Konban skill (`~/.claude/skills/konban/notion-api.py`)

- **"Sessions" property**: Rich text on Notion DB, stores newline-separated session UUIDs.
- **`link PAGE_ID [--session UUID]`**: Links a session to a task. Auto-detects current session UUID by finding the most recently modified JSONL in `~/.claude/projects/` (skips agent subprocesses).
- **`unlink PAGE_ID --session UUID`**: Removes a session link.
- **`sessions PAGE_ID`**: Lists linked sessions for a task.
- **`session-map`**: Outputs JSON `{session_uuid: {task_id, title, status, priority}}` for daemon consumption.

### Context frame (`context_frame.py`)

- **`load_session_task_map()`**: Calls `konban session-map`, caches to `session-task-map.json` with same TTL as context frame (6h).
- **`get_task_for_session(path)`**: Looks up a session file in the cached map.

### Extraction integration

- **`artifact_extract.py`**: Linked task injected as `<linked_task>` block before context frame in the LLM prompt. Tells the model to use task context as a strong prior for category classification.
- **`extract.py`**: Linked task hint prepended to `combined_context` for fact extraction.
- **`kb-extract-daemon.sh`**: Session-task map refreshed at start of each daemon run (alongside context frame refresh).

### Graceful degradation

All integration points use `try/except ImportError` guards. If the Konban skill is unavailable or the session-map is empty, both extractors fall back to existing behavior. No new hard dependencies.

## Future Phases

### Phase 2: Session packer integration
- Group sessions by task instead of chronological list
- `session-packer --task TASK_ID` → all sessions for that task
- Done-task sessions marked as archived context

### Phase 3: Reconciliation pipeline exact matching
- When a session is linked to a task, reconciliation uses exact match (session declared its task) instead of fuzzy title matching
- The category-aware matching rule (ADR-007 addendum) becomes deterministic for linked sessions
- Artifact → task routing skips the LLM classification step entirely

### Phase 4: Auto-suggestion
- At session start, suggest linking based on working directory or git branch
- `~/github/kaufmann-health/` → suggest active KH Konban tasks
- Git branch `feature/two-path-calendar` → suggest matching Konban task
- Could be implemented as a Claude Code hook (PreToolUse or custom)

### Phase 5: Category inheritance
- Add explicit "Category" select property to Konban tasks (Personal/KH/Consulting/KE)
- When a session is linked, category is a lookup, not inference
- Eliminates the LLM category classification step for linked sessions entirely
- Requires backfilling category on existing tasks (can be semi-automated)

## Tradeoffs

- **Pro**: Eliminates hardest inference problem, improves session discoverability, compounds through existing knowledge graph
- **Con**: Requires discipline to link at session start (friction vs. value)
- **Mitigation**: Phase 4 auto-suggestion reduces friction. Phase 1 auto-detection of session UUID already minimizes the mechanical cost.

## Related

- **ADR-002** (Information Architecture): Session-task linking extends the entity graph with session nodes
- **ADR-004** (Reconciliation Pipeline): Phase 3 enables deterministic artifact→task matching
- **ADR-007** (Semantic Session Memory): Category taxonomy (addendum) becomes a lookup instead of inference target for linked sessions
- **Brain doc**: "Session-Task Linking Architecture" (Notion) — the original design doc
- **Konban task**: `31fb2e4a-2d15-812a-9d44-d191535a2713` — "Session ↔ Konban bidirectional linking"
