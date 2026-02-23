# Knowledge Base — Personal Semantic Knowledge Management

A local, file-based knowledge management system that captures facts, decisions, and entity relationships from Claude Code sessions. Designed to solve the "Claude amnesia" problem — where context is lost between sessions despite instructions to remember.

## Problem

Claude Code sessions are ephemeral. Even with MEMORY.md and CLAUDE.md instructions, Claude regularly forgets facts ("Alice left", "Feature X was removed", "Bob's role changed"). The root cause: there's no structured knowledge layer between Claude and the user's world.

## Architecture

```
~/.knowledge-base/
├── knowledge.db              # SQLite — the truth store
├── BRIEF.md                  # Auto-generated context file (loaded by Claude)
├── entity-index.json         # Entity name index (rebuilt by daemon)
├── context-frame.md          # Live system state snapshot (6h TTL)
├── artifacts-pending.json    # Extracted artifacts awaiting reconciliation
├── standup-proposals.json    # Deferred actions for standup approval
├── skill-fixes-pending.json  # SKILL.md patch proposals for standup
├── reconciliation-review.md  # Human-readable review summary
├── reconciliation.log        # Full audit trail
├── .session-offsets.json     # Per-session extraction high-water marks
├── .artifact-offsets.json    # Per-session artifact extraction offsets
└── (symlinks to repo scripts)

~/github/knowledge-base/
├── kb.py                 # CLI query tool
├── extract.py            # Fact extraction + tool error parsing
├── artifact_extract.py   # Structured artifact extraction (plans, analyses, errors)
├── pipeline_reconcile.py # Reconciliation against live system state
├── executor.py           # Action execution with permission model
├── pipeline.py           # Pipeline orchestrator + CLI
├── context_frame.py      # Dynamic context frame generator
├── briefing.py           # BRIEF.md generator
├── schema.sql            # DB schema
├── setup.sh              # Install: create DB, symlinks
├── eval/                 # Benchmark harness + test cases
├── docs/
│   ├── ADR-001-architecture.md
│   ├── ADR-002-information-architecture.md
│   ├── ADR-003-domain-aware-extraction.md
│   ├── ADR-004-reconciliation-pipeline.md
│   └── acceptance-criteria.md
└── README.md
```

## How It Works

### Layer 1: Fact Extraction (passive, every 30 min)

A launchd daemon processes Claude Code session transcripts via Qwen 3.5 (OpenRouter). Extracts entities, facts, relations, and decisions into SQLite. Per-session offset tracking ensures incremental processing with no gaps.

### Layer 2: Artifact Extraction (passive, alongside Layer 1)

Same daemon pass also runs GLM-5 to identify higher-order artifacts: plans, analyses, frameworks, error patterns. These are stored in `artifacts-pending.json` for reconciliation.

**Tool error detection**: Parses raw `tool_use`/`tool_result` blocks from session JSONL to find skill helper errors and inefficiencies (wrong flags, case sensitivity, discovery calls, soft misses). These become `error_pattern` artifacts with structured fields: `skill`, `script`, `error_type`, `correct_usage`, `doc_gap`.

### Layer 3: Reconciliation (at standup or on-demand)

`pipeline.py --reconcile --execute` loads pending artifacts + live system state (Konban board, Brain Active Context, Brain index, KB decisions, git commit history, referenced SKILL.md docs) and reconciles via GLM-5.

**Three-tier action model:**

| Tier | Confidence | Action |
|------|-----------|--------|
| Auto-execute | High + reversible | Create tasks, log context, create Brain docs, mark done, apply SKILL.md patches |
| Propose at standup | Medium or content mutation | Stale facts, ambiguous completions, SKILL.md fixes |
| Never | Irreversible or structural | Modify Active Context, delete anything, send comms |

### Layer 4: Skill Self-Improvement (automated feedback loop)

When Claude misuses a skill helper (wrong flag, invalid value, missing constraint), the pipeline:
1. **Detects** the error sequence in session JSONL (error → retry → success)
2. **Classifies** it (wrong_arg_type, invalid_value, case_sensitivity, inefficient_lookup, discovery_call)
3. **Loads** the referenced SKILL.md and compares — is this already documented?
4. **Generates** a structured patch (append_to_section, add_note_after, add_new_section) or marks as no_action
5. **Applies** high-confidence additive patches automatically, defers others to `skill-fixes-pending.json`

### Layer 5: Recall Hook (real-time, ~10-50ms)

A `UserPromptSubmit` hook matches entity names in user messages against `entity-index.json` and injects relevant KB facts as `system-reminder` context. No explicit tool calls needed — entity knowledge is "just available."

## Three Access Tiers

| Tier | What | When | Token cost |
|------|------|------|------------|
| **1. BRIEF.md + Recall Hook** | Auto-generated summary + real-time entity lookup | Every session, every message | ~500 tokens + ~50 per match |
| **2. kb.py** | CLI query tool, called via Bash | On-demand deep lookup | Per-query only |
| **3. Pipeline** | Extraction + reconciliation + execution | Daemon (30 min) + standup | Zero (runs outside session) |

## Setup

```bash
cd ~/github/knowledge-base
./setup.sh
```

## Usage

### Knowledge queries
```bash
python3 ~/.knowledge-base/kb.py query "alice"          # Entity lookup
python3 ~/.knowledge-base/kb.py search "QA"            # Full-text search
python3 ~/.knowledge-base/kb.py decisions              # Active decisions
python3 ~/.knowledge-base/kb.py domain "EO"            # Domain filter
python3 ~/.knowledge-base/kb.py status                 # Health check
python3 ~/.knowledge-base/kb.py correct "Entity" attr "value"  # Fix a fact
python3 ~/.knowledge-base/kb.py delete-fact "Entity" attr      # Remove a fact
python3 ~/.knowledge-base/kb.py recent --days 7        # Audit recent extractions
```

### Pipeline
```bash
# Full pipeline (standup trigger)
python3 ~/github/knowledge-base/pipeline.py --reconcile --execute

# Status check
python3 ~/github/knowledge-base/pipeline.py --status

# Standup proposal approval
python3 ~/github/knowledge-base/pipeline.py --show-proposals
python3 ~/github/knowledge-base/pipeline.py --approve 1,3
python3 ~/github/knowledge-base/pipeline.py --approve all
python3 ~/github/knowledge-base/pipeline.py --dismiss-proposals

# Skill fix approval
python3 ~/github/knowledge-base/pipeline.py --show-skill-fixes
python3 ~/github/knowledge-base/pipeline.py --apply-skill-fix 1,3
python3 ~/github/knowledge-base/pipeline.py --apply-skill-fix all
python3 ~/github/knowledge-base/pipeline.py --dismiss-skill-fixes

# Consistency check only (no artifacts)
python3 ~/github/knowledge-base/pipeline_reconcile.py --consistency-only
```

### Manual extraction
```bash
python3 ~/.knowledge-base/extract.py --last-session
python3 ~/.knowledge-base/extract.py --input transcript.txt
python3 ~/.knowledge-base/briefing.py   # Regenerate BRIEF.md
```

## Model Selection

- **Fact extraction**: Qwen 3.5 397B via OpenRouter ($0.15/$1.00 per M tokens)
- **Artifact extraction + reconciliation**: GLM-5 via OpenRouter ($0.30/$2.55 per M tokens)
- **Recall hook**: No LLM — regex matching + SQLite lookup (~10-50ms)

GLM-5 chosen for precision over cost. False positives in an autonomous system create noise (garbage tasks, wrong SKILL.md patches). See ADR-004 for benchmark details.

## Design Principles

- **The daemon is a signal producer, not a decision maker.** It surfaces what changed. The human decides about ambiguous signals.
- **Skills self-improve.** Every tool error is a documentation bug. The pipeline closes the loop from error → SKILL.md patch.
- **Write-through, not write-behind.** Facts, artifacts, and actions are persisted immediately — not batched at end-of-day.
- **Three tiers of autonomy.** High-confidence reversible actions auto-execute. Everything else gets human review.

## Documentation

- [ADR-001: Architecture](docs/ADR-001-architecture.md) — Why file-based, why extraction pipeline
- [ADR-002: Information Architecture](docs/ADR-002-information-architecture.md) — Entity/fact/relation data model
- [ADR-003: Domain-Aware Extraction](docs/ADR-003-domain-aware-extraction.md) — Context injection, domain routing
- [ADR-004: Reconciliation Pipeline](docs/ADR-004-reconciliation-pipeline.md) — Full pipeline architecture, permission model, skill improvement
- [Acceptance Criteria](docs/acceptance-criteria.md) — Quality gates for pipeline output
