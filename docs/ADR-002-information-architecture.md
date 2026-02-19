# ADR-002: Information Architecture — What Goes Where

**Date**: 2026-02-19
**Status**: Accepted
**Context**: The Knowledge Base is one of 7+ information systems. Without clear boundaries, every new fact triggers a "where does this go?" decision, which kills adoption.

## The Principle

Each system answers ONE question:

| Question | System | Examples |
|----------|--------|----------|
| **What is true right now?** | Knowledge Base (SQLite + BRIEF.md) | "Marta left", "Katherine: 10h/week", "Concierge matching: removed" |
| **What should I do today?** | Konban (Notion Kanban) | "Contact institutes", "Send proposal to Rouven" |
| **What's the KH strategy?** | Notion Brain (Active Context) | "Priority #1: therapist acquisition via institutes" |
| **What features are we building?** | Linear | "EARTH-123: Implement mandatory onboarding flow" |
| **When am I free?** | Google Calendar | Meetings, Make Blocks, European Window |
| **What did a client pay for?** | Google Drive | Proposals, contracts, deliverables |
| **What did I learn about a tool/pattern?** | MEMORY.md | "iCloud Glob fails", "use worktrees for parallel sessions" |

## Decision Rules

### Facts about entities → Knowledge Base

If it's a **state** about a person, company, feature, or project — and it would be true next week — it goes in the KB.

- "Katherine's role is Strategy & Support" → KB fact
- "Marta quit" → KB fact (supersedes "Marta is QA Tester")
- "Concierge matching feature removed" → KB decision
- "Therapist onboarding is now mandatory" → KB decision

### Things to do → Konban

If it dies when completed, it's a task.

- "Contact NARM institutes" → Konban task
- "Get testimonials from Luuse" → Konban task
- "Send booking link to therapist friend" → Konban task

### Strategic direction → Notion Brain

If it shapes priorities across multiple sessions, it's strategy.

- "Therapist acquisition is #1 priority" → Brain (Active Context)
- "We're removing concierge matching to simplify the platform" → Brain (Active Context decision section)
- "Roadmap: Stripe billing → Coach expansion → Native booking" → Brain

### Technical implementation → Linear

If it needs acceptance criteria and a developer, it's a dev task.

- "Build mandatory onboarding flow with 30min scheduling" → Linear (EARTH-xxx)
- "Remove concierge matching UI from platform" → Linear

### Records and deliverables → Google Drive

If it's a document someone else will read, or needs long-term archival.

- "Rouven Soudry notary proposal" → GDrive
- "Kerstin Kampschulte phase 2 scope" → GDrive
- "Katherine's KH role adjustment email" → GDrive (if worth archiving)

### Tool/workflow learnings → MEMORY.md

If it helps a future Claude session avoid a mistake or find a capability.

- "iCloud Glob fails, use explicit paths" → MEMORY.md
- "Use worktrees for parallel KH sessions" → MEMORY.md
- "Konban skill supports child pages under tasks" → MEMORY.md

## Overlap Resolution

### Decisions appear in both KB and Brain

This is intentional, not duplication:
- **KB decisions** are captured automatically by the extraction pipeline. They're structural facts: "X was decided on date Y."
- **Brain decisions** are curated by Konstantin during Kraken sessions. They include strategic context: "Why this matters for the roadmap."

The KB is the **ledger** (complete, automatic). The Brain is the **executive summary** (curated, contextual).

### MEMORY.md shrinks over time

Before the KB, MEMORY.md stored facts about people and projects (Oz's behavioral history, Marta's role, Katherine's therapist friend). Those migrate to the KB. MEMORY.md keeps only tool-usage patterns and workflow gotchas.

## Consequences

- Every session loads BRIEF.md passively — entity context is always available
- MEMORY.md is no longer the catch-all for "things to remember"
- Extraction pipeline is the primary write path for the KB (not manual)
- Konban tasks never store entity facts — they reference KB entities if needed
