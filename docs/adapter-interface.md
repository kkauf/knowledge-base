# External Tool Adapter Interface

The knowledge-base pipeline can optionally create tasks, manage documents, and check system state via external tool scripts. These are configured in `config.json` under `external_tools`.

This document specifies the interface contract. Any script that implements these subcommands can serve as an adapter — whether it talks to Notion, Todoist, Linear, Obsidian, or a flat file system.

## Overview

| Config key | Role | Required subcommands |
|------------|------|---------------------|
| `konban_script` | Task board (Kanban) | `board`, `search`, `create`, `log`, `done`, `update` |
| `brain_script` | Knowledge docs | `index`, `read`, `create`, `update`, `patch` |

Both are optional. Without them, the pipeline still extracts facts and artifacts — it just can't create tasks or documents in external systems.

## Task Board Adapter (`konban_script`)

### `board` — List active tasks

```bash
python3 <script> board
```

Returns all active tasks (excluding done/archived). Output format is flexible, but must include task titles and page/task IDs in a human-readable format. The reconciliation model reads this as context.

**Used by**: `pipeline_reconcile.py` (system state), `context_frame.py` (dynamic context)

### `search` — Find task by name

```bash
python3 <script> search "<query>"
```

Fuzzy-matches `<query>` against active task titles. Output must include the task ID in square brackets on each matching line:

```
Doing ⚡️  | High | Fix auth timeout [a1b2c3d4-e5f6-7890-abcd-ef1234567890]
```

The executor extracts IDs via the regex `\[([0-9a-f-]{36})\]`. First match wins.

If no tasks match, output should contain "No active tasks" or similar.

**Used by**: `executor.py` (resolve task name → ID before log/done/update)

### `create` — Create a new task

```bash
python3 <script> create "<title>" --priority <High|Medium|Low> [--timebox <duration>]
```

Creates a task and prints the new ID on stdout in the format:

```
Created: a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

The executor extracts the ID from lines containing "Created:".

**Used by**: `executor.py` (`execute_create_konban_task`)

### `log` — Append message to task

```bash
python3 <script> log "<task_id>" "<message>"
```

Appends a timestamped log entry to the specified task. Exit code 0 = success.

This is the most-called subcommand. The executor logs:
- Task content after creation
- Evidence before marking done
- Reasons for updates
- Cross-references to related Brain docs

**Used by**: `executor.py` (all task operations include logging)

### `done` — Mark task as complete

```bash
python3 <script> done "<task_id>"
```

Moves the task to a "Done" state. Exit code 0 = success.

The executor always logs evidence *before* calling `done`.

**Used by**: `executor.py` (`execute_done_konban_task`)

### `update` — Update task metadata

```bash
python3 <script> update "<task_id>" [--name <title>] [--due <date>] [--priority <priority>] [--timebox <duration>]
```

Updates one or more properties. All flags are optional. Exit code 0 = success.

**Used by**: `executor.py` (`execute_update_konban_task`)

## Knowledge Docs Adapter (`brain_script`)

### `index` — List all documents

```bash
python3 <script> index
```

Returns a formatted list of all documents, organized by section. Output is read as context by the reconciliation model to decide whether to create new docs or enrich existing ones.

**Used by**: `pipeline_reconcile.py` (system state), `context_frame.py` (dynamic context)

### `read` — Get document content

```bash
python3 <script> read "<doc_title>" --raw
```

Returns the full markdown content of the document. `--raw` means no extra formatting or metadata headers — just the content.

The title is fuzzy-matched against existing documents.

**Used by**:
- `pipeline_reconcile.py` (reads "Active Context" for system state)
- `executor.py` (reads before enriching — append-only pattern)
- `context_frame.py` (reads "Active Context" for context frame)

### `create` — Create a new document

```bash
python3 <script> create "<title>" --file <path> --parent <section> [--domain <tag>]
```

Creates a new document under the specified section. Content is read from a file (not stdin) to avoid shell escaping issues.

Sections are logical groupings. The reference implementation uses: Strategy, Operations, Product, Research, Archive.

The executor writes content to `/tmp/daemon-brain-<timestamp>.md` before calling create, and cleans up afterward.

**Used by**: `executor.py` (`execute_create_brain_doc`)

### `update` — Full document rewrite

```bash
python3 <script> update "<doc_title>" --file <path> --force
```

Replaces the entire document content. `--force` suppresses safety checks (e.g., open comments).

The executor uses this for enrichment: read existing content → append new section → write back the full content.

**Used by**: `executor.py` (`execute_enrich_brain_doc`, `_cross_reference_artifact_groups`)

### `patch` — Update a single section

```bash
python3 <script> patch "<doc_title>" --section "<heading>" --file <path>
```

Replaces only the content under `<heading>`, preserving everything else. Faster and safer than full rewrite.

Used when the daemon has already enriched a document and needs to re-enrich (replace the daemon's own section, not duplicate it).

**Used by**: `executor.py` (`execute_enrich_brain_doc` — re-enrichment case)

## Conventions

### Content passing

All content is passed via `--file <path>`, never via stdin or argument. The executor writes to `/tmp/daemon-*.md` temp files and cleans up after each operation. This avoids shell escaping issues with markdown content.

### ID format

Task/document IDs are expected to be UUID-formatted: `a1b2c3d4-e5f6-7890-abcd-ef1234567890` (36 characters with dashes). The executor extracts IDs via regex.

### Error handling

- Exit code 0 = success
- Exit code != 0 = failure (executor logs the error and skips the action)
- If `search` returns no matches, the executor skips the action gracefully
- All subprocess calls have a 30-second timeout

### Prefix convention

The executor prefixes daemon-created tasks with `[daemon]` and includes source artifact references in log entries. This makes automated actions distinguishable from human ones.

### Idempotency

All operations should be safe to retry. The executor may call `search` + `log` multiple times for the same task if processing is interrupted and restarted.

## Writing a New Adapter

To integrate a different system (e.g., Todoist, Linear, Obsidian):

1. Create a Python script (or any executable) that accepts the subcommands above
2. Set the path in `config.json`:
   ```json
   {
     "external_tools": {
       "konban_script": "~/path/to/my-todoist-adapter.py",
       "brain_script": "~/path/to/my-obsidian-adapter.py"
     }
   }
   ```
3. You don't need both — configure only what you use
4. Test with `pipeline.py --reconcile` (dry run without `--execute`) to see proposed actions

### Minimal task board adapter

A minimal adapter only needs `board`, `search`, `create`, `log`, and `done`. The `update` command is optional (only used for title/date changes).

### Minimal docs adapter

A minimal adapter only needs `index`, `read`, and `create`. The `update` and `patch` commands are only needed for the enrichment workflow (appending sections to existing docs).

## Reference Implementation

See [claude-notion](https://github.com/kkauf/claude-notion) for a Notion-based implementation of both adapters, with full SKILL.md documentation for Claude Code integration.
