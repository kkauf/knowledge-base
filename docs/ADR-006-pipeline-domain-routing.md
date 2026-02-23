# ADR-006: Pipeline Domain Routing — Tasks, Docs, and Git Awareness

**Date**: 2026-02-23
**Status**: Active
**Context**: The extraction daemon and reconciliation pipeline lack domain awareness for routing actions. Dev tasks go to Konban instead of Linear. Brain docs go to KH Brain regardless of source domain. Git history only checks one branch, missing staging work.

## Problems

### 1. Task routing: everything goes to Konban
The artifact extraction captures "we should build PortalBookingDialog" from a KH coding session and the reconciliation proposes `create_konban_task`. But implementation-level dev tasks belong in Linear (EARTH-xxx), not the personal Kanban board. The Konban is for CEO-level actions: decisions, contacts, deadlines, meetings.

**Evidence**: 21 daemon-created Konban tasks reviewed on 2026-02-23. All 21 were either already done, stale, or belonged in Linear. 0/21 were useful as Konban tasks.

### 2. Reconciliation misses staging work
`load_git_history()` runs `git log` with no branch flag — it logs whatever branch is checked out (usually `main`). Konstantin commits frequently to staging branches, then batch-merges to production. The reconciliation prompt says "check if work is done" but the LLM never sees staging commits.

**Evidence**: The pipeline proposed tasks for work like "Create PortalBookingDialog" that was already implemented and committed on staging.

### 3. Brain docs go to wrong domain
`create_brain_doc` always creates under the KH Brain (Notion). VSS, knowledge-base, IsAI, and Personal domain docs shouldn't live there. The `domain` field exists in actions but isn't used for routing.

## Decision

### Fix 1: Git history — check all branches
Add `--all` and `--decorate=short` to the git log command in `load_git_history()`. The LLM sees commits across all branches with branch names, enabling it to distinguish staging from production.

### Fix 2: Reconciliation prompt — domain-aware task routing
Add rules to the reconciliation prompt:
- **Task routing**: Never `create_konban_task` for implementation-level code changes (components, APIs, endpoints, schemas, tests). Only for CEO-level actions.
- **Brain routing**: Only `create_brain_doc` for KH domain. Other domains have their own docs.

### Fix 3: Executor guards — defense in depth
Add deterministic guards in `executor.py`:
- `execute_create_konban_task`: Skip if domain is KH AND title contains dev keywords.
- `execute_create_brain_doc`: Skip if domain is not KH.

The prompt fix (Fix 2) catches ~90% of cases. The executor guard (Fix 3) catches the rest deterministically.

## Architecture

```
Session → artifact_extract.py → artifacts + domain tag
                                     │
                                     ▼
                            pipeline_reconcile.py
                            ├── load_git_history() [Fix 1: --all branches]
                            ├── reconciliation prompt [Fix 2: domain routing rules]
                            └── proposed actions + domain
                                     │
                                     ▼
                              executor.py
                              ├── permission_check() [existing]
                              ├── domain guard [Fix 3: skip wrong-domain actions]
                              └── execute action
```

### Domain routing table

| Domain | Konban tasks | Brain docs | Linear issues |
|--------|-------------|------------|---------------|
| KH | CEO-level only | Yes | Dev tasks (future) |
| Personal | Yes | No | No |
| Infrastructure | Yes | No | No |
| VSS | No | No | Own docs |
| IsAI | No | No | Own docs |

### Future: Linear integration
The executor currently has no `create_linear_issue` action. When this is added (EARTH-xxx), the reconciliation prompt should route KH dev tasks to Linear instead of Konban. For now, they're simply skipped.

## Testing

After deployment, run the reconciliation pipeline against a recent KH coding session transcript to verify:
1. Dev tasks are NOT created as Konban tasks
2. Brain docs are only created for KH domain
3. Git history shows commits from all branches with branch names
4. Already-completed work is detected via staging branch commits

## References
- ADR-004: Reconciliation Pipeline
- ADR-005: Recall App Layer (same session — KB amnesia diagnosis led to this)
- Konban cleanup: 21/21 daemon tasks trashed on 2026-02-23
