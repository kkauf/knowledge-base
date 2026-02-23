# knowledge-base

Persistent memory for Claude Code. Extracts facts from your sessions, stores them in SQLite, and injects them back automatically — so Claude remembers what happened last week, last month, or six months ago.

## The Problem

Claude Code is powerful inside a session. But sessions are ephemeral. When you start a new one, Claude doesn't know that:

- Your team lead left three weeks ago and hasn't been replaced
- You decided to drop Redis in favor of SQLite last month
- The API migration is 60% done — the auth endpoints shipped but billing hasn't
- Your client prefers email over Slack and invoices are net-30

You can put some of this in `CLAUDE.md` or `MEMORY.md`, but those are manual, unstructured, and don't scale. After a few months of active use, you have hundreds of entities (people, projects, tools, decisions) with thousands of facts spread across hundreds of sessions. No static file can capture that.

**The result: you re-explain context constantly.** Every session starts with "remember, we decided X" and "by the way, Y changed." You become a human context loader for your own AI assistant.

## What This Does

This tool runs a background daemon that reads your Claude Code session transcripts, extracts structured knowledge, and makes it available to every future session — automatically.

### 1. It builds a knowledge graph from your sessions

Every 30 minutes, a daemon reads your session transcripts and extracts:

- **Entities**: people, projects, companies, tools, features
- **Facts**: attributes with values (`role: "engineering lead"`, `status: "on parental leave"`)
- **Relations**: who works where, what depends on what, who owns what
- **Decisions**: choices made and their rationale

These go into a local SQLite database. Extraction is incremental — it picks up where it left off, never reprocesses old content, and handles sessions of any length.

### 2. It injects knowledge into every session automatically

Two mechanisms, zero manual effort:

**BRIEF.md** — A auto-generated summary loaded into Claude's context at session start. Shows your domains, key entities, recent decisions, and top facts. ~500 tokens. Gives Claude a "lay of the land" before you say anything.

```
# Knowledge Brief (847 entities, 2,391 facts)

## Domains
| Region   | Entities | Facts | Last updated          |
|----------|----------|-------|-----------------------|
| MyApp    | 523      | 1,641 | Stripe (2026-02-23)   |
| Personal | 180      | 498   | schedule (2026-02-22) |

## Recent Decisions (7d)
- **Switched from Redis to SQLite for session store** (2026-02-22) — Latency benchmarks showed...
- **Moved to biweekly deploys** (2026-02-20) — Too many hotfixes from weekly cadence...

## MyApp
- **Acme Corp** (company, 84f): plan=enterprise | mrr=$4,200 | main_contact=Dana Chen
- **Auth Service** (project, 52f): stack=Go | status=migrating_to_v2 | deadline=March 15
- **Dana Chen** (person, 31f): role=VP Engineering | timezone=PST | prefers=async_updates
```

**Recall Hook** — A `UserPromptSubmit` hook (~10-50ms) that pattern-matches entity names in your messages and injects their facts as `system-reminder` context. When you type "what's the status with Dana?", Claude already knows Dana is VP Engineering at Acme Corp before it even starts thinking.

### 3. It detects when things are stale or done

The reconciliation pipeline (`pipeline.py --reconcile`) compares extracted artifacts against your actual system state — git commits, task boards, documents — and flags inconsistencies:

- A task marked "in progress" but the feature already shipped (git evidence)
- A priority that was completed but never updated
- A SKILL.md doc that's missing a flag Claude keeps getting wrong

Actions are tiered by confidence:

| Confidence | What happens |
|------------|-------------|
| **High** (explicit evidence) | Auto-executed: mark task done, log context, patch docs |
| **Medium** (implicit signal) | Proposed at your next review for approval |
| **Low** (ambiguous) | Logged only, never acted on |

Nothing irreversible happens without your approval.

## Who This Is For

- **Claude Code power users** who work across multiple projects and domains
- **Solo operators / small teams** where one person carries all the context
- **Anyone tired of re-explaining context** at the start of every session

You need: macOS (for the launchd daemon), Python 3.10+, and an [OpenRouter](https://openrouter.ai/) API key (~$0.50/day for active use).

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/kkauf/knowledge-base.git ~/github/knowledge-base
cd ~/github/knowledge-base
```

### 2. Create your config

```bash
mkdir -p ~/.knowledge-base
cp config.example.json ~/.knowledge-base/config.json
```

Edit `~/.knowledge-base/config.json` with your values:

```jsonc
{
  // Where to store the knowledge database and generated files
  "kb_dir": "~/.knowledge-base",

  // Where Claude Code stores session transcripts
  "sessions_dir": "~/.claude/projects",

  // Your OpenRouter API key (or set OPENROUTER_API_KEY env var)
  "openrouter_api_key_sources": [
    "env:OPENROUTER_API_KEY",
    "~/.knowledge-base/secrets/openrouter.env"
  ],

  // Domain detection: map session paths to knowledge domains
  // (sessions in ~/github/my-saas/ get tagged "MyApp", etc.)
  "domains": [
    {"name": "MyApp", "patterns": ["my-saas", "my-app"]},
    {"name": "Personal", "patterns": ["personal", "notes"]},
    {"name": "Infrastructure", "patterns": ["knowledge-base", "dotfiles"]}
  ],

  // Git repos to scan for "done" signals during reconciliation
  "git_repos": ["~/github/my-saas"],

  // Your name variants (for deduplicating "John Doe" / "jdoe" / "John")
  "owner_entity_names": ["John Doe", "jdoe", "John"]
}
```

### 3. Set up your OpenRouter API key

Create the secrets file:

```bash
mkdir -p ~/.knowledge-base/secrets
echo "OPENROUTER_API_KEY=sk-or-v1-your-key-here" > ~/.knowledge-base/secrets/openrouter.env
chmod 600 ~/.knowledge-base/secrets/openrouter.env
```

Or just export the environment variable: `export OPENROUTER_API_KEY=sk-or-v1-...`

### 4. Run setup

```bash
./setup.sh
```

This creates the database, symlinks scripts, generates your first BRIEF.md, and installs the launchd daemon (runs every 30 minutes).

### 5. Wire up the recall hook

Add to your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "command": "python3 ~/.knowledge-base/kb-recall.py \"$PROMPT\"",
        "timeout": 100
      }
    ]
  }
}
```

The recall hook script (`kb-recall.py`) is not included in this repo — it's a thin wrapper that matches entity names against `entity-index.json` and returns `system-reminder` blocks. See [ADR-001](docs/ADR-001-architecture.md) for the design. A reference implementation is straightforward: ~80 lines of Python doing regex matching + SQLite lookups.

### 6. Point Claude at your BRIEF.md

Add to your `~/.claude/CLAUDE.md` (or project-level):

```markdown
## Knowledge Base
- Auto-generated brief: `~/.knowledge-base/BRIEF.md` (loaded at session start)
- Deep lookup: `python3 ~/.knowledge-base/kb.py query "entity"` | `search "term"` | `decisions`
```

That's it. The daemon extracts knowledge in the background. BRIEF.md refreshes automatically. The recall hook injects entity facts in real time. Claude just... remembers.

## Usage

### Query the knowledge base

```bash
kb.py query "Acme Corp"         # Everything known about an entity
kb.py search "migration"        # Full-text search across all facts
kb.py decisions                 # Active decisions and rationale
kb.py decisions --all           # Including superseded/reversed
kb.py domain "MyApp"            # All entities in a domain
kb.py status                    # Health check: daemon, stats, last extraction
kb.py recent --days 7           # Audit recent extractions
```

### Correct mistakes

The extraction LLM is good but not perfect. When you spot a wrong fact:

```bash
kb.py correct "Acme Corp" plan "growth"     # Fix a fact
kb.py delete-fact "Dana Chen" old_role       # Remove a stale fact
```

### Run the reconciliation pipeline

```bash
# Full pipeline: extract artifacts, reconcile against system state, execute safe actions
pipeline.py --reconcile --execute

# Just check status
pipeline.py --status

# Review and approve proposed actions
pipeline.py --show-proposals
pipeline.py --approve 1,3          # Approve specific proposals
pipeline.py --approve all          # Approve everything
pipeline.py --dismiss-proposals    # Skip all

# Manual extraction (outside daemon schedule)
extract.py --last-session          # Process the most recent session
briefing.py                        # Regenerate BRIEF.md
```

## How It Works

```
 Claude Code sessions (JSONL transcripts)
          │
          ▼
 ┌─────────────────┐    every 30 min
 │  extract.py      │◄── launchd daemon
 │  (Qwen 3.5)      │    reads new messages
 └────────┬─────────┘    since last offset
          │
          ▼
 ┌─────────────────┐
 │  knowledge.db    │    SQLite: entities,
 │                  │    facts, relations,
 │                  │    decisions
 └────────┬─────────┘
          │
     ┌────┴────┐
     ▼         ▼
 BRIEF.md   entity-index.json
 (summary)  (for recall hook)
```

**Extraction** is passive and incremental. The daemon tracks a per-session offset (how many messages it's already processed). Each run picks up only new messages. A context overlap of 10 messages ensures continuity across extraction windows.

**Artifact extraction** runs alongside fact extraction, using a different model (GLM-5) to identify higher-order content: plans, analyses, error patterns. These are stored as pending artifacts for reconciliation.

**Reconciliation** is on-demand. It loads pending artifacts plus your current system state (git commits, task board, documents) and decides what actions to take. High-confidence reversible actions execute automatically. Everything else gets proposed for your review.

**The recall hook** runs on every user message (~10-50ms). It regex-matches entity names from `entity-index.json` against your input and injects matching facts as `system-reminder` context. No LLM call — pure string matching + SQLite lookup.

## Configuration

All configuration lives in `~/.knowledge-base/config.json`. See [`config.example.json`](config.example.json) for the full template.

| Key | What | Default |
|-----|------|---------|
| `kb_dir` | Where to store knowledge.db and generated files | `~/.knowledge-base` |
| `sessions_dir` | Claude Code session transcripts | `~/.claude/projects` |
| `extraction_model` | LLM for fact extraction | `qwen/qwen3.5-397b-a17b` |
| `reconciliation_model` | LLM for artifact extraction + reconciliation | `z-ai/glm-5` |
| `domains` | Path patterns → domain names (for organizing entities) | `[]` |
| `git_repos` | Repos to scan for "done" signals | `[]` |
| `owner_entity_names` | Your name variants for dedup | `[]` |
| `external_tools` | Optional integrations (task board, docs) | `{}` |
| `briefing.key_entities` | Always show these in BRIEF.md | `[]` |
| `briefing.domain_order` | Display order for domains | auto-detected |

### External tool integrations

The reconciliation pipeline can optionally create tasks and documents in external systems. Configure script paths in `external_tools`:

```json
{
  "external_tools": {
    "konban_script": "~/path/to/task-board-cli.py",
    "brain_script": "~/path/to/docs-cli.py"
  }
}
```

These scripts must accept specific subcommands (`create`, `log`, `done`, `read`, `list`). Without them, the pipeline still works — it just extracts and reports instead of taking actions.

## Models and Cost

| Component | Model | Cost (per M tokens) | Why |
|-----------|-------|---------------------|-----|
| Fact extraction | Qwen 3.5 397B | $0.15 / $1.00 | Good extraction quality, low cost |
| Artifact extraction | GLM-5 | $0.30 / $2.55 | High precision for autonomous actions |
| Reconciliation | GLM-5 | $0.30 / $2.55 | False positives create noise |
| Recall hook | None | $0 | Regex + SQLite, no LLM |

Both models run via [OpenRouter](https://openrouter.ai/). GLM-5 is chosen for precision over cost — in a system that takes autonomous actions, false positives create garbage (wrong tasks, bad doc patches). See [ADR-004](docs/ADR-004-reconciliation-pipeline.md) for benchmark details.

Typical cost: ~$0.30-0.50/day for active use (5-10 sessions/day).

## Design Principles

- **The daemon is a signal producer, not a decision maker.** It surfaces what changed. You decide about ambiguous signals.
- **Three tiers of autonomy.** High-confidence reversible actions auto-execute. Medium gets proposed. Irreversible never happens without you.
- **Write-through, not write-behind.** Facts, artifacts, and actions are persisted immediately — not batched at end-of-day.
- **Skills self-improve.** When Claude misuses a tool (wrong flag, missing constraint), the pipeline detects the error-retry-success pattern and patches the documentation automatically.
- **Local-first.** Everything is files and SQLite. No cloud services except the LLM API calls. Your knowledge stays on your machine.

## Architecture

For those who want to understand or contribute:

```
~/.knowledge-base/
├── config.json               # Your configuration
├── knowledge.db              # SQLite — entities, facts, relations, decisions
├── BRIEF.md                  # Auto-generated summary (loaded by Claude)
├── entity-index.json         # Entity name index (for recall hook)
├── context-frame.md          # Live system state snapshot (6h TTL)
├── artifacts-pending.json    # Extracted artifacts awaiting reconciliation
├── reconciliation-review.md  # Human-readable review of last pipeline run
├── reconciliation.log        # Full audit trail
└── (symlinks to repo scripts)

~/github/knowledge-base/      # This repo
├── config.py                 # Centralized config module
├── config.example.json       # Template for new users
├── extract.py                # Fact extraction from session transcripts
├── artifact_extract.py       # Artifact extraction (plans, analyses, errors)
├── pipeline.py               # Pipeline orchestrator + CLI
├── pipeline_reconcile.py     # Reconciliation against live system state
├── executor.py               # Action execution with permission model
├── briefing.py               # BRIEF.md generator
├── context_frame.py          # Dynamic context frame generator
├── reconcile.py              # Entity dedup and merge
├── kb.py                     # CLI query tool
├── schema.sql                # Database schema
├── setup.sh                  # Installer
├── kb-extract-daemon.sh      # Daemon entry point
├── kb-extract.plist.template # launchd plist template
└── docs/                     # Architecture Decision Records
```

## Documentation

- [ADR-001: Architecture](docs/ADR-001-architecture.md) — Why file-based, why extraction pipeline, alternatives considered
- [ADR-002: Information Architecture](docs/ADR-002-information-architecture.md) — Entity/fact/relation data model
- [ADR-003: Domain-Aware Extraction](docs/ADR-003-domain-aware-extraction.md) — Context injection, domain routing
- [ADR-004: Reconciliation Pipeline](docs/ADR-004-reconciliation-pipeline.md) — Full pipeline architecture, permission model, skill improvement

## License

MIT
