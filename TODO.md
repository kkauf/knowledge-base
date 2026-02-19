# TODO — Knowledge Base

## Done
- [x] Schema + DB init (schema.sql, setup.sh)
- [x] CLI query tool (kb.py) — query, search, entities, assert, decide
- [x] Extraction pipeline (extract.py) — OpenRouter + Gemini Flash
- [x] Briefing generator (briefing.py) — auto-generates BRIEF.md
- [x] End-to-end test: "Marta quit" scenario
- [x] End-to-end test: Katherine email extraction

## Next
- [ ] Wire BRIEF.md into CLAUDE.md as auto-loaded context
- [ ] Seed DB with existing MEMORY.md facts (Oz, KH status, clients, etc.)
- [ ] Add post-session hook for automatic extraction
- [ ] Add `kb.py supersede` command for manually ending decisions

## Parked
- Web UI / visualization (maybe connect to VSS?)
- Entity deduplication / merge tool
- Import from Notion Brain
