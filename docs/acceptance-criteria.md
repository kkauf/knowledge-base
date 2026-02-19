# Acceptance Criteria — Knowledge Base MVP

## AC-1: Database schema supports temporal facts

- [ ] SQLite database at `~/.claude/knowledge/knowledge.db`
- [ ] Entities table: id, name, type, created_at, updated_at
- [ ] Facts table: id, entity_id, attribute, value, source, valid_from, valid_to, superseded_by, created_at
- [ ] Relations table: id, from_entity_id, relation_type, to_entity_id, valid_from, valid_to, created_at
- [ ] Decisions table: id, title, rationale, status, context, decided_at, created_at
- [ ] `setup.sh` creates DB and applies schema idempotently

## AC-2: CLI query tool returns current facts

- [ ] `kb.py query "marta"` returns all current (valid_to IS NULL) facts about Marta
- [ ] `kb.py query "marta" --history` includes superseded facts
- [ ] `kb.py search "QA"` searches across entity names, fact values, and decision titles
- [ ] `kb.py decisions` lists active decisions
- [ ] `kb.py decisions --all` includes superseded/reversed decisions
- [ ] `kb.py entities` lists all known entities with type
- [ ] Output is human-readable (formatted text, not raw JSON)
- [ ] Exit code 0 on success, 1 on no results, 2 on error

## AC-3: Extraction pipeline captures facts from transcripts

- [ ] `extract.py --input file.txt` processes a transcript file
- [ ] `extract.py --last-session` finds and processes the most recent Claude Code session
- [ ] Extraction uses a cheap model (Haiku 4.5 or Gemini Flash) via API
- [ ] Extracts: new entities, new/changed facts, new relations, new decisions
- [ ] Distinguishes durable facts from ephemeral tasks (tasks are skipped)
- [ ] Upserts into SQLite: new facts supersede old ones (valid_to stamped on predecessor)
- [ ] Dry-run mode: `--dry-run` shows what would be written without writing
- [ ] After extraction, auto-regenerates BRIEF.md

## AC-4: BRIEF.md is accurate and concise

- [ ] `briefing.py` generates `~/.claude/knowledge/BRIEF.md`
- [ ] Contains current-state facts only (valid_to IS NULL)
- [ ] Grouped by entity, sorted by recency
- [ ] Active decisions listed with rationale
- [ ] Under 200 lines / ~500 tokens (fits in context without bloat)
- [ ] Header warns "Auto-generated — do not edit manually"

## AC-5: Setup and integration

- [ ] `setup.sh` is idempotent (safe to run multiple times)
- [ ] Creates `~/.claude/knowledge/` directory
- [ ] Creates SQLite DB with schema
- [ ] Symlinks `kb.py`, `extract.py`, `briefing.py` into `~/.claude/knowledge/`
- [ ] Instructions in README for adding to CLAUDE.md

## AC-6: The "Marta quit" test

End-to-end validation using the motivating example:
- [ ] Seed DB with: Entity "Marta Sapor" (person), Fact "role = QA Tester" (valid_from Jan 17)
- [ ] Run extraction on a transcript containing "Marta quit"
- [ ] Verify: old role fact gets valid_to stamped, new status fact created
- [ ] Verify: BRIEF.md shows "Marta Sapor — former QA Tester, left"
- [ ] Verify: `kb.py query "marta"` shows current state
- [ ] Verify: `kb.py query "marta" --history` shows both states

## Out of scope for MVP

- Automated post-session hooks (manual trigger for now)
- Web UI or visualization
- Multi-user / sync
- Conflict resolution for concurrent writes
- Embedding-based semantic search (full-text search is sufficient for personal scale)
