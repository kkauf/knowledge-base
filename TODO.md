# TODO — Knowledge Base

## Done
- [x] Schema + DB init (schema.sql, setup.sh)
- [x] CLI query tool (kb.py) — query, search, entities, assert, decide
- [x] Extraction pipeline (extract.py) — OpenRouter + Qwen 3.5
- [x] Briefing generator (briefing.py) — auto-generates BRIEF.md
- [x] End-to-end test: "Marta quit" scenario
- [x] End-to-end test: Katherine email extraction
- [x] Wire BRIEF.md into CLAUDE.md as auto-loaded context
- [x] Recall hook — entity name matching + SQLite lookup → system-reminder injection
- [x] Per-session offset tracking (incremental extraction, no gaps)
- [x] Artifact extraction (GLM-5, plans/analyses/errors → artifacts-pending.json)
- [x] Reconciliation pipeline (live Konban/Brain/KB state + GLM-5 reconciliation)
- [x] Executor with three-tier permission model + audit trail
- [x] Pipeline orchestrator (pipeline.py — status, reconcile, execute, approve, dismiss)
- [x] Standup integration (subagent runs pipeline, surfaces in dashboard)
- [x] Interactive proposal approval (--approve, --dismiss-proposals)
- [x] Dynamic context frame (context_frame.py, 6h TTL cache)
- [x] Brain section routing (Strategy/Operations/Product/Research/Archive)
- [x] Git commit history as "done" signal source
- [x] State consistency check (Active Context vs git vs Konban)
- [x] Confidence scoring (high→auto, medium→propose, low→skip)
- [x] Skill self-improvement pipeline (tool error parsing → SKILL.md patches)
- [x] Skill fix CLI (--show-skill-fixes, --apply-skill-fix, --dismiss-skill-fixes)

## Next
- [ ] Rollback capability — `pipeline.py --rollback <action-id>`
- [ ] Feedback loop — log proposal rejections, tune confidence thresholds
- [ ] Entity deduplication / merge tool

## Parked
- Web UI / visualization
- Import from Notion Brain
