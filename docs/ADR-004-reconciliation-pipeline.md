# ADR-004: Reconciliation Pipeline — From Fact Extraction to Autonomous Action

**Date**: 2026-02-21
**Status**: Draft
**Context**: Conversations produce structured work products (plans, roadmaps, analyses) that are lost when context compacts. The extraction daemon captures durable facts but misses higher-order artifacts. The daemon should evolve from a passive fact extractor into an active reconciliation agent.

## Problem

Claude Code compresses older context to fit within the context window. When this happens, structured work products created mid-conversation — roadmaps, multi-step plans, user interview analyses, architectural decisions with rationale — get flattened into generic summaries that lose their utility.

The current extraction daemon (ADR-001) captures entity facts and decisions, but:
- It does not identify structured work products (plans, roadmaps, analyses)
- It does not compare extracted knowledge against current system state (Konban, Brain)
- It cannot take action (create tasks, update docs) based on what it finds
- Errors in Claude's tool usage (e.g., wrong CLI flags) are not captured as improvement signals

## Decision

Build a two-stage reconciliation pipeline that runs nightly alongside (not replacing) the existing fact extraction. Stage 1 extracts artifacts. Stage 2 reconciles them against system state and generates actions within a strict permission model.

## Architecture

### Pipeline Stages

```
                    ┌──────────────────────────────────┐
                    │     EXISTING (unchanged)          │
                    │  Qwen 3.5 → Fact Extraction       │
                    │  (entities, facts, relations,      │
                    │   decisions → SQLite)              │
                    └──────────────────────────────────┘
                                    │
                    ┌──────────────────────────────────┐
                    │     NEW: Stage 1 — Extract        │
                    │  [Model TBD] per transcript        │
                    │                                    │
                    │  Input:  parsed transcript          │
                    │  Output: structured artifacts       │
                    │          (plans, analyses, errors)  │
                    └──────────────────────────────────┘
                                    │
                    ┌──────────────────────────────────┐
                    │     NEW: Stage 2 — Reconcile      │
                    │  [Model TBD, likely Sonnet Batch]  │
                    │                                    │
                    │  Input:  artifacts from Stage 1    │
                    │        + Konban board state         │
                    │        + Brain Active Context       │
                    │        + KB entity graph            │
                    │  Output: action plan (JSON)         │
                    └──────────────────────────────────┘
                                    │
                    ┌──────────────────────────────────┐
                    │     NEW: Stage 3 — Execute         │
                    │  Python executor (no LLM)          │
                    │                                    │
                    │  Applies actions per permission     │
                    │  model. Logs everything.            │
                    │  Generates review summary.          │
                    └──────────────────────────────────┘
```

### Stage 1: Artifact Extraction

Runs per-transcript. Identifies structured work products that go beyond individual facts.

**Artifact types:**

| Type | Example | What makes it an artifact |
|------|---------|--------------------------|
| `plan` | "5-step roadmap for matching engine" | Ordered steps with an end goal |
| `analysis` | "User interview themes summary" | Synthesized insights from raw data |
| `framework` | "Permission model for daemon actions" | Reusable structure/taxonomy |
| `error_pattern` | "Claude used --description on create, which doesn't exist" | Tool misuse → skill improvement signal |
| `decision_with_context` | "Chose Qwen over GLM-5 because..." | Goes beyond the KB's flat decision record |

**Output format:**
```json
{
  "artifacts": [
    {
      "type": "plan",
      "title": "Matching Engine V2 Roadmap",
      "summary": "5-step plan covering profile depth, scoring, and honest framing",
      "domain": "KH",
      "entities_referenced": ["matching engine", "profile depth", "Roadmap"],
      "content": "1. Implement profile depth scoring...\n2. ...",
      "source_session": "abc123.jsonl",
      "confidence": 0.9
    }
  ],
  "error_patterns": [
    {
      "tool": "konban",
      "command": "create --description",
      "error": "unrecognized argument",
      "resolution": "used create + log instead",
      "suggested_fix": "add --description support to create command, or document limitation in SKILL.md"
    }
  ]
}
```

### Stage 2: Reconciliation

Runs once per batch (all transcripts from the day). Loads current system state and compares against extracted artifacts.

**System state loaded:**
- Konban board (`notion-api.py board` output)
- Brain Active Context (`notion-api.py read "Active Context" --raw`)
- KB entities + top facts for relevant domains
- Recent decisions (last 7 days)

**Reconciliation logic:**
1. For each artifact, check: does this already exist?
   - Plan steps → match against Konban tasks (fuzzy title match)
   - Analyses → match against Brain docs
   - Decisions → match against KB decisions
2. Identify gaps: what's in the artifact but not in the system?
3. Identify conflicts: what's in the artifact that contradicts the system?
4. Generate actions for gaps (within permission model)
5. Flag conflicts (never auto-resolve)

**Output format:**
```json
{
  "proposed_actions": [
    {
      "type": "create_konban_task",
      "title": "Implement profile depth scoring",
      "priority": "High",
      "source_artifact": "Matching Engine V2 Roadmap",
      "rationale": "Step 1 of roadmap, no matching Konban task found"
    },
    {
      "type": "log_konban_task",
      "task_id": "existing-page-id",
      "message": "Session discussed approach: use embedding similarity for matching",
      "rationale": "Context from session enriches existing task"
    },
    {
      "type": "create_brain_doc",
      "title": "User Interview Analysis — Feb 20",
      "content": "...",
      "rationale": "Detailed analysis not captured elsewhere"
    },
    {
      "type": "fix_skill",
      "skill": "konban",
      "file": "SKILL.md",
      "change": "Add note: create does not support --description, use log after create",
      "rationale": "Error pattern detected in session"
    }
  ],
  "conflicts_flagged": [
    {
      "artifact": "Matching Engine V2 Roadmap step 2",
      "conflicts_with": "Active Context says profile depth is deprioritized",
      "recommendation": "Review with Konstantin — Active Context NOT modified"
    }
  ],
  "reconciliation_summary": "3 new tasks proposed, 1 existing task enriched, 1 conflict flagged"
}
```

### Stage 3: Executor

Deterministic Python code (no LLM). Takes the action plan and applies it.

#### Three-Tier Action Model

The daemon's value isn't just creating new knowledge — it's keeping existing knowledge current. But autonomous mutation is risky. The solution: tier actions by confidence and reversibility.

**Design principle: the daemon is a signal producer, not a decision maker.** It surfaces what changed. The human (or the interactive Claude session at standup) decides what to do about ambiguous signals. The daemon auto-executes only when confidence is high and the action is reversible.

**Tier 1: Auto-execute** (high confidence + reversible)

| Action | Implementation | Reversibility |
|--------|----------------|---------------|
| Create Brain doc (new topic) | `notion-api.py create --parent Section` | Archive if wrong |
| Enrich existing Brain doc | `notion-api.py patch` (additive section only) | Remove section |
| Log on Konban task | `notion-api.py log` | Log entry stays (append-only) |
| Create Konban task | `notion-api.py create` + `[daemon]` tag | Trash if wrong |
| Mark Konban task done | `notion-api.py done` (high-confidence only) | Re-open to Doing |
| Update Brain doc metadata | `notion-api.py meta` (status, summary) | Change back |

Tier 1 actions are shown at standup for awareness ("spot-check if wrong").

**Tier 2: Propose at standup** (medium confidence or content mutation)

| Action | How surfaced | Resolution |
|--------|-------------|------------|
| Stale fact in Brain doc | Reconciliation report → standup prompt | Interactive Claude applies or dismisses |
| To-do item appears completed | Reconciliation report → standup prompt | User confirms |
| Task completion (ambiguous signal) | Log note on task: "[daemon] appears done" | User marks done or ignores |
| Skill fix needed | `skill-fixes-pending.json` | Standup reviews |
| Brain doc section needs update | Reconciliation report with proposed text | Standup applies via patch |

Tier 2 proposals are presented at standup as yes/no decisions. User says "yes, yes, skip" and the standup Claude batch-applies.

**Tier 3: Never** (irreversible or structural)

| Action | Why never |
|--------|-----------|
| Modify Active Context | Strategic document — Kraken-mode only |
| Delete anything | Irreversible (archive instead) |
| Send external comms | Irreversible, affects other people |
| Modify CLAUDE.md / SOUL.md | Infrastructure — requires explicit user request |
| Rewrite Brain doc content | Too risky for autonomous agent — use enrich or propose |

#### Confidence Scoring

The reconciliation model assesses each proposed action with a confidence level:

- **high** (>90%): Explicit signal in transcript — "shipped", "deployed", "merged", "sent the email." Auto-execute.
- **medium** (50-90%): Implicit signal — task was discussed as complete but no explicit confirmation. Propose at standup.
- **low** (<50%): Ambiguous — something might be done, or the discussion might invalidate existing content. Flag only.

Confidence is based on:
1. **Signal strength**: "committed and deployed EARTH-294" > "we discussed profile changes"
2. **Recency**: Yesterday's session > last week's session
3. **Corroboration**: Multiple sessions confirm the same change > single mention
4. **Explicitness**: Tool calls (konban done, notion-docs update) > verbal discussion

#### Audit Trail

Every action logged to `~/.claude/knowledge/reconciliation.log` with: what changed, what the previous value was (for mutations), transcript evidence, confidence score. This enables:
- Spot-checking at standup
- Rollback via `pipeline.py --rollback <action-id>` (planned)
- Learning from corrections over time

#### Standup Integration

The standup protocol reads the reconciliation output and presents:
```
## Overnight Reconciliation

DONE (spot-check if wrong):
  [+] Created "Research: Gmail Dark Mode" in Brain/Research (3,799 chars)
  [+] Marked "PLZ validation fix" as Done — "committed and deployed" in session ccb0abfc

YOUR CALL:
  [1] "Roadmap" still says Profile Expansion at #6, moved to #1 per Feb 20 session. Update?
  [2] "Send Jörg response" — email appears sent. Mark done?
```

User says "yes, yes" and the standup Claude executes both. Total reconciliation review: 30 seconds.

## Scheduling & Incremental Processing

**Original design**: nightly batch at 3 AM. **Problem**: Mac is asleep overnight. Options (pmset wake, run-on-wake, server) all have significant tradeoffs.

**Revised design: Incremental extraction, reconciliation at standup.**

### Stage 1 (Extraction): Piggyback on existing 30-min daemon

When the daemon processes a session for fact extraction, also run artifact extraction. Store artifacts in `~/.claude/knowledge/artifacts-pending.json`. Cost: +40-70s per session.

**Critical change: per-session offset tracking.** The current daemon takes the "last 50 messages" of a session — this creates gaps. Messages 200-350 are never seen if the session grew from 200 to 400 messages between runs.

New approach:
- Track per-session high-water mark in `~/.claude/knowledge/.session-offsets.json`: `{"session_id": last_processed_message_index}`
- Each run: read messages from offset to end (the delta)
- Include last 10 messages from previous window as context overlap (so the model understands references)
- Extraction prompt marks `[--- NEW MESSAGES BELOW ---]` to focus extraction on new content only
- On session resume (next day): only new messages are processed. No re-extraction.

**Key insight**: The .jsonl transcript preserves ALL messages, including ones Claude compacted away. The extraction model has access to information Claude itself forgot. This is the core value — the pipeline remembers what Claude doesn't.

### Stage 2 (Reconciliation): Run at standup time

- Trigger: first message of the day (standup mode), or manual `python3 pipeline.py --reconcile`
- Input: all pending artifacts from `artifacts-pending.json` + live Konban/Brain state
- Single GLM-5 call, ~60-90 seconds
- Output: action plan JSON

### Stage 3 (Execution): Immediate after reconciliation

- No LLM, deterministic Python
- 5-10 seconds
- Review summary surfaces in standup dashboard

### Temporal ordering

Artifacts carry timestamps from the session. When a plan is created at message 100 and superseded at message 300 (same session, same day):
- Daemon run at 10:30: extracts "plan created" artifact (timestamp T1)
- Daemon run at 2:30: extracts "plan superseded" artifact (timestamp T2)
- Stage 2 reconciliation: sees both, later artifact supersedes earlier (same pattern as fact extraction)

Multiple concurrent sessions (different projects) are processed independently. Cross-session references are resolved in Stage 2 via system state (Konban/Brain already contains the cross-session context).

## Model Selection

**Decision: GLM-5 (`z-ai/glm-5`)** for both Stage 1 and Stage 2. Precision matters most for an autonomous system — false positives create noise (garbage Konban tasks). GLM-5 was more precise (exact title matches, no spurious results) and consistent between runs. Qwen was cheaper/faster but inconsistent (different results on same input). Cost difference ($0.01 vs $0.02/night) is irrelevant at this scale. Sonnet via OpenRouter failed; Batch API skipped (unreliable in practice).

**Benchmark results** (eval-001, Carlotta interview analysis):

| Variable | Options |
|----------|---------|
| Model | Qwen 3.5 ($0.15/$1.00) vs GLM-5 ($0.30/$2.55) vs Sonnet 4.6 Batch (~$1.50/$7.50) |
| Context strategy | Full-load (transcript + state in one call) vs two-stage (extract then reconcile) |
| Prompt caching | Sonnet supports cached input at 90% discount — stable state context is cacheable |
| Context window | Qwen 262K, GLM-5 205K, Sonnet 1M — matters for full-load strategy |

**Eval dataset:** Real transcripts where structured work products were created. Score on:
1. Artifact detection accuracy (did it find the plan?)
2. Action correctness (did it propose the right Konban tasks?)
3. False positive rate (did it propose actions for things that already exist?)
4. Conflict detection (did it catch contradictions with current state?)

## Quality Gates (Acceptance Criteria)

Established after first real execution revealed low-quality Brain docs (summaries of summaries).

| # | Criterion | How verified |
|---|-----------|-------------|
| 1 | **Content reproduction**: Brain docs contain full artifact (headings, data, reasoning), not summaries | Read created doc, compare to source |
| 2 | **No phantom references**: Never "includes X" without X in the content | Grep for "includes", "contains" |
| 3 | **Section routing**: Every Brain doc goes to correct section (Strategy/Operations/Product/Research) | Check `--parent` on create call |
| 4 | **Staleness filtering**: Already-implemented recommendations get `no_action` | Verify against Active Context |
| 5 | **Content > 500 chars**: No trivial Brain docs that should be Konban logs | Check content length |
| 6 | **Daemon attribution**: All created content has source attribution | Read doc header |

## Brain Section Routing

The Brain has a hierarchical structure with typed sections. The daemon routes docs to the appropriate section:

| Signal | Section | Title convention |
|--------|---------|-----------------|
| Company direction, positioning, "why" | Strategy | Descriptive title |
| Day-to-day execution, billing, playbooks | Operations | Descriptive title |
| Feature specs, platform standards, UX | Product | Descriptive title |
| External research, market/legal analysis, interviews | Research | `Research: [Topic]` prefix |
| Superseded content | Archive | Never create here |

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Daemon creates garbage tasks | Tag all daemon-created items `[daemon]`. Morning review catches bad ones. |
| Duplicate tasks (daemon creates what already exists) | Reconciliation stage fuzzy-matches against existing Konban before proposing. |
| Stale state (Konban changed since last read) | Load state fresh at reconciliation time, not at extraction time. |
| Model hallucination in artifact extraction | Confidence threshold on artifacts. Low-confidence artifacts flagged, not acted on. |
| Permission creep over time | Three-tier model defined in code (ALLOWED_ACTIONS in executor.py), not just in prompt. |
| Cost spiral | GLM-5 via OpenRouter keeps cost at ~$0.50/day. Budget alert if > $1/day. |
| Brain doc quality (v1 failure) | Content reproduction rules in extraction prompt. Reconciler passes content verbatim. Quality gates validated per run. |
| Enrichment overwrites content | `enrich_brain_doc` is additive only — appends new section, never modifies existing. Falls back to `patch` for re-enrichment of daemon's own section. |
| Auto-marking tasks done incorrectly | Only high-confidence signals (explicit "shipped"/"deployed"/"merged" + matching task). Easily reversible. Shown at standup for spot-check. |

## Implementation Plan

### Phase 1: Core pipeline (complete)

1. ~~**Write benchmark harness**~~ Done: `eval/benchmark.py`
2. ~~**Build eval dataset**~~ Done: `eval/cases.json` (5 cases)
3. ~~**Run benchmark**~~ Done: GLM-5 selected (precision > cost > speed)
4. ~~**Build executor**~~ Done: `executor.py` (permission model, audit log, review summary)
5. ~~**Add per-session offset tracking**~~ Done: `extract.py` now tracks per-session high-water marks in `.session-offsets.json`. Incremental delta with 10-message context overlap. Context/new separator markers for extraction LLM.
6. ~~**Add artifact extraction to daemon**~~ Done: `artifact_extract.py` (GLM-5 via OpenRouter, separate offset tracking, XML-wrapped transcript to prevent echo). Daemon calls it after fact extraction (non-critical — failure doesn't stop daemon).
7. ~~**Build Stage 2 reconciliation**~~ Done: `pipeline_reconcile.py` (loads live Konban/Brain/KB state, GLM-5 reconciliation call, action plan output)
8. ~~**Build pipeline orchestrator**~~ Done: `pipeline.py` (--status, --reconcile, --execute, --show-pending, --clear-pending). Ties Stage 1+2+3 together.
9. ~~**Integrate with standup**~~ Done: Added to Personal Support CLAUDE.md standup protocol. Pipeline runs as subagent during context loading. Review summary surfaces in dashboard under "Overnight Reconciliation".
10. ~~**Research: survey existing agent memory tools**~~ Done: CogCanvas (verbatim citation), memU (category summaries, content hashing, reinforcement), Mem0 (ADD/UPDATE/DELETE/NOOP taxonomy)

### Phase 2: Quality & enrichment (in progress)

11. ~~**Fix content reproduction quality**~~ Done: Extraction prompt requires full content (not excerpts). Reconciler passes content verbatim. Quality gates defined and validated.
12. ~~**Add Brain section routing**~~ Done: `executor.py` passes `--parent Section` to notion-api.py create. Reconciler outputs `section` field per routing table.
13. ~~**Add staleness checking**~~ Done: Reconciliation prompt checks artifacts against current Active Context timeline.
14. ~~**Add `enrich_brain_doc` action**~~ Done: Executor reads existing doc, appends new section, writes back. Re-enrichment patches daemon's own section. Reconciler prefers enrichment over creation when existing doc covers the same topic.
15. ~~**Artifact decomposition**~~ Done: When artifacts contain both analysis and actionable recommendations, the reconciler produces compound actions — Brain doc (the "why") + Konban tasks (the "what"). Cross-referenced bidirectionally: tasks log the Brain doc pointer, Brain docs list which tasks were created. Prevents recommendations from sitting inert inside reference documents.
16. ~~**Add `done_konban_task` to Tier 1**~~ Done: Moved from DENIED to ALLOWED with confidence gating. Executor rejects unless `confidence: "high"`. Evidence logged before marking done. Reconciler uses explicit signal detection ("shipped", "deployed", "committed", tool calls).
17. ~~**Add confidence scoring**~~ Done: Every action requires `confidence: high|medium|low`. High → auto-execute. Medium → deferred to `standup-proposals.json` for standup review. Low → skipped. Based on signal strength, recency, corroboration, explicitness.
18. ~~**Split reconciliation output**~~ Done: Review summary now has two sections: **DONE** (auto-executed, spot-check) and **YOUR CALL** (deferred proposals, approve/dismiss). Deferred actions saved to `~/.claude/knowledge/standup-proposals.json`.
19. ~~**Add Brain index to reconciliation context**~~ Done: `load_brain_index()` loads `notion-api.py index` output alongside Active Context, Konban, and KB decisions. Enables accurate enrich-vs-create decisions.

### Phase 3: Standup integration (planned)

20. **Interactive approval at standup** — Standup Claude reads Tier 2 proposals and presents as batch yes/no decisions. Executes approved actions in-session.
21. **Rollback capability** — `pipeline.py --rollback <action-id>` undoes a specific auto-executed action using audit log's "previous value" field.
22. **Feedback loop** — Log rejections of Tier 2 proposals. Over time, tune confidence thresholds based on rejection rate.

## Research Patterns (from CogCanvas, memU, Mem0)

| Pattern | Source | Status |
|---------|--------|--------|
| Verbatim citation grounding (`quote` field) | CogCanvas | Adopt in Stage 1 extraction prompt |
| Content hashing for exact dedup | memU | Adopt (augment existing fuzzy dedup) |
| ADD/UPDATE/DELETE/NOOP reconciliation taxonomy | Mem0 | Adopt in Stage 2 |
| Category summaries as reconciliation layer | memU | Mirrors Brain docs pattern — validate |
| Conversation segmentation before extraction | memU | Defer (offset tracking handles incrementality) |
| Reinforcement counting for recurring facts | memU | Defer (nice-to-have) |
| Temporal regex fallback for dates | CogCanvas | Defer (low priority) |
| Two-pass gleaning | CogCanvas | Defer (2x cost) |
| Soft-delete for contradictions | Mem0 | Defer (current hard delete works for now) |

## References

- ADR-001: Architecture (why file-based, why extraction pipeline)
- ADR-003: Domain-aware extraction (context injection pattern, reusable for reconciliation)
- OpenClaw agent architecture: permission-scoped autonomous agents (github.com/openclaw/openclaw)
- CogCanvas: arxiv.org/abs/2601.00821 (verbatim grounding, temporal resolution)
- memU: github.com/NevaMind-AI/memU (category summaries, content hashing, reinforcement)
- Mem0: arxiv.org/abs/2504.19413 (ADD/UPDATE/DELETE/NOOP taxonomy, graph memory)
- Konban task: `30eb2e4a-2d15-81f6-a205-f1b6148e4355`
- Model comparison: artificialanalysis.ai/leaderboards/models
