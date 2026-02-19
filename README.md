# Knowledge Base — Personal Semantic Knowledge Management

A local, file-based knowledge management system that captures facts, decisions, and entity relationships from Claude Code sessions. Designed to solve the "Claude amnesia" problem — where context is lost between sessions despite instructions to remember.

## Problem

Claude Code sessions are ephemeral. Even with MEMORY.md and CLAUDE.md instructions, Claude regularly forgets facts ("Marta quit", "concierge matching was removed", "Katherine's role changed"). The root cause: there's no structured knowledge layer between Claude and the user's world.

## Architecture

```
~/.claude/knowledge/
├── knowledge.db      # SQLite — the truth store
├── BRIEF.md          # Auto-generated context file (loaded by Claude)
└── (symlinks to repo scripts)

~/github/knowledge-base/
├── kb.py             # CLI query tool (Tier 2)
├── extract.py        # Extraction pipeline (Tier 3)
├── schema.sql        # DB schema
├── briefing.py       # BRIEF.md generator
├── setup.sh          # Install: create DB, symlinks
├── docs/
│   ├── ADR-001-architecture.md
│   └── acceptance-criteria.md
└── README.md
```

### Three tiers, no MCP

| Tier | What | When | Token cost |
|------|------|------|------------|
| **1. BRIEF.md** | Auto-generated summary of current facts | Loaded passively every session | ~500 tokens |
| **2. kb.py** | CLI query tool, called via Bash | On-demand when Claude needs deeper lookup | Per-query only |
| **3. extract.py** | Post-session extraction via cheap model | After sessions that matter | Zero (runs outside session) |

No MCP server. No persistent tool definitions. The brief file does 90% of the work.

### Data model

- **Entities**: People, projects, companies, concepts
- **Facts**: Temporal attributes (valid_from / valid_to). New facts supersede old ones.
- **Relations**: Links between entities, also temporal.
- **Decisions**: First-class objects with rationale and status.

## Setup

```bash
cd ~/github/knowledge-base
./setup.sh
```

## Usage

```bash
# Query an entity
python3 ~/.claude/knowledge/kb.py query "marta"

# Search across all facts
python3 ~/.claude/knowledge/kb.py search "QA"

# List active decisions
python3 ~/.claude/knowledge/kb.py decisions

# Extract from last session
python3 ~/.claude/knowledge/extract.py --last-session

# Extract from a file
python3 ~/.claude/knowledge/extract.py --input transcript.txt

# Regenerate the brief
python3 ~/.claude/knowledge/briefing.py
```

## Claude integration

Add to `~/.claude/CLAUDE.md`:

```
### Knowledge Base
- Context file: `~/.claude/knowledge/BRIEF.md` — current facts about people, projects, and decisions. Auto-generated, do not edit manually.
- For deeper lookups: `python3 ~/.claude/knowledge/kb.py query "entity_name"`
- After sessions with significant fact changes: `python3 ~/.claude/knowledge/extract.py --last-session`
```
