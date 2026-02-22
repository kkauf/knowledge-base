#!/usr/bin/env python3
"""
Reconciliation Pipeline — Stage 2: Reconcile Artifacts Against System State

Takes pending artifacts (from Stage 1 artifact_extract.py) and compares them against
live system state (Konban board, Brain docs, KB decisions). Produces an action plan
for the executor (Stage 3).

Uses GLM-5 via OpenRouter for precision.

Usage:
    # Reconcile pending artifacts against live state
    python3 pipeline_reconcile.py

    # Dry-run (show action plan, don't pass to executor)
    python3 pipeline_reconcile.py --dry-run

    # Use specific artifacts file
    python3 pipeline_reconcile.py --artifacts path/to/artifacts.json

    # Output action plan to file
    python3 pipeline_reconcile.py --output action-plan.json

    # Reconcile and immediately execute
    python3 pipeline_reconcile.py --execute
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import get_api_key

# --- Config ---

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "z-ai/glm-5"
KB_DIR = Path.home() / ".claude" / "knowledge"
PENDING_FILE = KB_DIR / "artifacts-pending.json"

KONBAN_SCRIPT = Path.home() / ".claude" / "skills" / "konban" / "notion-api.py"
BRAIN_SCRIPT = Path.home() / ".claude" / "skills" / "notion-docs" / "notion-api.py"
KB_SCRIPT = KB_DIR / "kb.py"

# --- Reconciliation Prompt ---

RECONCILIATION_PROMPT = """You are a reconciliation system for a personal knowledge management pipeline.

You receive:
1. ARTIFACTS extracted from conversation transcripts (structured work products with full content)
2. CURRENT STATE of the user's task board (Konban), knowledge base, and recent git commits

Your job: determine what actions should be taken to reconcile the artifacts with current state.

RULES:
- Only propose actions for artifacts with value "medium" or "very_high"
- Only propose actions for artifacts with persistence_status "not_persisted" or "partial"
- Check if the artifact content already exists in Konban tasks or Brain docs (fuzzy match on title/content)
- Flag conflicts between artifact content and current state (but NEVER resolve them — flag only)
- Prefer enriching existing items over creating new ones:
  * For Konban tasks: use log_konban_task to add context to an existing task
  * For Brain docs: use enrich_brain_doc to append a new section to a related existing doc
  * Only create_brain_doc when no existing doc is a natural home for the content
- For error_patterns: propose SKILL.md fixes (propose only, never auto-apply)
- Be conservative — when in doubt, propose no_action rather than creating noise

ENRICHMENT vs CREATION decision:
- CHECK THE BRAIN DOCUMENT INDEX before proposing create_brain_doc. If any existing doc covers the same topic or entity, use enrich_brain_doc instead.
- Fuzzy match on topic: "Research: Carlotta Interview Implications" covers the same topic as an artifact titled "What the Carlotta Interview Tells You About the SaaS Product" — these are the SAME topic. Use enrich, not create.
- If the artifact is a genuinely new TOPIC not covered by any existing doc, use create_brain_doc.
- Enrichment is additive only — it appends a new section, never modifies existing content.
- When enriching, choose a section_name that describes what's being added (e.g., "SaaS Product Implications", "Pre-Booking Gap Analysis").

STALENESS CHECK (critical):
- Artifacts come from conversation transcripts that may be hours or days old.
- Before proposing an action, check if the CURRENT STATE already reflects the artifact's recommendations.
  For example: if an artifact says "profile pages need X improvement" but the Active Context or Konban already shows "deployed profile improvements", the artifact is STALE — propose no_action.
- When an artifact is stale, set rationale to explain WHY it's stale (e.g., "Already addressed: Active Context shows profile improvements deployed on Feb 20").
- Only create Brain docs for content that is STILL VALUABLE as a reference document, even if the recommendations were already acted upon. Strategic analyses, research findings, and decision frameworks retain value. Implementation checklists and "what to do next" lists do NOT.

CONTENT PASS-THROUGH (critical):
- Each artifact has a "content" field containing the FULL reproduced work product.
- For create_brain_doc actions: copy the artifact's "content" field VERBATIM into the action's "content" field. Do NOT summarize, excerpt, or describe what the content contains. The executor creates the Brain doc directly from this field.
- For log_konban_task actions: you may summarize the content for the log entry, since Konban logs are brief status updates.

SECTION ROUTING (for create_brain_doc):
The Brain has 5 sections. Every Brain doc MUST include a "section" field:
- "Strategy" — company direction, positioning, mission, vision, "why" decisions
- "Operations" — day-to-day execution, billing, playbooks, internal tooling analysis
- "Product" — feature specs, platform standards, UX analysis, implementation plans
- "Research" — external research, market analysis, legal analysis, user interview analysis, competitive research
- "Archive" — never create here (only for manually moving superseded content)

For Research section docs, prefix the title with "Research: " (e.g., "Research: Carlotta Interview Implications").

ARTIFACT DECOMPOSITION (critical — you MUST do this):
Many artifacts contain BOTH reference material (analysis, reasoning) AND actionable recommendations (to-dos, next steps). You MUST decompose these into SEPARATE actions. A Brain doc with recommendations sitting inside it is an anti-pattern — recommendations need to become Konban tasks.

SCAN every artifact for actionable items. Look for:
- Bullet points with "todo", "to do", "next step", "should", "need to", "action item"
- Numbered lists of recommendations
- Sections titled "What to do", "Next steps", "Recommendations", "Action items"
- Imperative sentences ("Add X", "Fix Y", "Measure Z", "Ship W")

When you find actionable items:
1. Create the Brain doc (or enrich existing doc) for the analysis — this is the "why"
2. Create a SEPARATE create_konban_task for EACH actionable item — this is the "what"
3. Give all actions from the same artifact the same "artifact_group" value (lowercase-kebab-case, e.g. "carlotta-interview")
4. On each Konban task, set "brain_doc" to the Brain doc title

DO NOT skip this step. If an artifact has 3 recommendations, you MUST produce 3 create_konban_task actions (minus any that already exist in Konban or are marked done).

For decomposed Konban tasks:
- Title: short imperative (e.g., "Add OFFLINE_GAP tracking for pre-booking")
- Content: 1-3 sentences of relevant reasoning from the artifact (why this matters)
- Priority: based on artifact confidence levels and urgency
- brain_doc: title of the companion Brain doc

CONFIDENCE SCORING (required on every action):
Every action MUST include a "confidence" field: "high", "medium", or "low".

- **high** (>90%): Explicit signal — "shipped", "deployed", "merged", "done", or tool call evidence (e.g., `konban done`, `git push`). Auto-executed by daemon.
- **medium** (50-90%): Implicit signal — task discussed as complete but no explicit confirmation, or recommendation is likely but not certain. Proposed at standup for user approval.
- **low** (<50%): Ambiguous — something might need action but evidence is weak. Logged only, not proposed.

Confidence is based on: signal strength (explicit > implicit), recency (today > last week), corroboration (multiple sessions > single mention), and explicitness (tool calls > verbal discussion).

PERMISSION MODEL (strict):
- CAN: create_konban_task (tagged [daemon]), log_konban_task, done_konban_task (HIGH confidence only), create_brain_doc, enrich_brain_doc, fix_skill, no_action
- CANNOT: delete anything, modify Active Context, send external comms

DONE_KONBAN_TASK rules:
- Only propose done_konban_task when evidence is EXPLICIT: "shipped", "deployed", "committed", "sent", "done", or you see the actual tool call (konban done, git push) in the transcript.
- Git commits are the STRONGEST done signal. A commit message like "fix(portal): accept 4-5 digit postal codes" + a Konban task "PLZ validation fix" = high confidence done. Commits prefixed with "Ship:" indicate production deployment.
- Always include the evidence in the "content" field (e.g., "Committed as af6b665 and deployed to production").
- The executor will REJECT done_konban_task unless confidence is "high".

GIT HISTORY usage:
- The system state includes recent git commits across active repos.
- Use commits to corroborate artifact claims: if an artifact says "shipped X" and git shows a matching commit, confidence is HIGH.
- Use commits to detect staleness: if an artifact recommends "fix Y" but git already shows a commit fixing Y, the artifact is stale.
- Commits prefixed with "Ship:" were deployed to production. Other commits may be staging-only.
- Match commits to Konban tasks by topic (fuzzy match on subject matter, not exact title match).

Return ONLY valid JSON:
{
  "proposed_actions": [
    {
      "type": "create_konban_task | log_konban_task | done_konban_task | create_brain_doc | enrich_brain_doc | fix_skill | no_action",
      "title": "task or doc title (for create actions)",
      "target": "existing doc or task name (for enrich/log actions)",
      "section_name": "heading for the new section (for enrich_brain_doc only, e.g. 'Carlotta Interview Findings')",
      "priority": "High | Medium | Low (for create_konban_task only)",
      "content": "FULL content for brain docs (verbatim from artifact) or summary for log entries",
      "section": "Strategy | Operations | Product | Research (for create_brain_doc only)",
      "domain": "metadata domain tag (e.g. Product, Research, Strategy, Operational)",
      "source_artifact": "artifact title this action comes from",
      "artifact_group": "shared ID linking actions from the same artifact (e.g. 'carlotta-interview')",
      "brain_doc": "title of the Brain doc this task relates to (for create_konban_task from decomposition)",
      "confidence": "high | medium | low (REQUIRED — see confidence scoring rules)",
      "rationale": "why this action (include staleness assessment and confidence justification)"
    }
  ],
  "conflicts_flagged": [
    {
      "artifact": "artifact title",
      "conflicts_with": "what it contradicts in current state",
      "recommendation": "what the user should review"
    }
  ],
  "summary": "1-2 sentence summary of reconciliation results"
}

If nothing needs to be done, return empty arrays. Do NOT create actions for artifacts that already exist in the system."""


# --- System state loading ---

def run_cmd(args: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def load_konban_state() -> str:
    """Load current Konban board state."""
    if not KONBAN_SCRIPT.exists():
        return "[Konban board unavailable]"
    output = run_cmd(["python3", str(KONBAN_SCRIPT), "board"], timeout=30)
    return output or "[Konban board empty or unavailable]"


def load_brain_active_context() -> str:
    """Load Active Context from Brain."""
    if not BRAIN_SCRIPT.exists():
        return "[Brain unavailable]"
    output = run_cmd(["python3", str(BRAIN_SCRIPT), "read", "Active Context", "--raw"], timeout=30)
    return output or "[Active Context unavailable]"


def load_recent_decisions() -> str:
    """Load recent KB decisions."""
    if not KB_SCRIPT.exists():
        return "[KB unavailable]"
    output = run_cmd(["python3", str(KB_SCRIPT), "decisions"], timeout=15)
    if output:
        lines = output.split("\n")
        if len(lines) > 25:
            output = "\n".join(lines[:25]) + f"\n... ({len(lines) - 25} more)"
    return output or "[No recent decisions]"


GIT_REPOS = [
    Path.home() / "github" / "kaufmann-health",
    Path.home() / "github" / "knowledge-base",
]


def load_git_history(days: int = 7) -> str:
    """Load recent git commit history across tracked repos."""
    sections = []
    for repo in GIT_REPOS:
        if not (repo / ".git").exists():
            continue
        output = run_cmd(
            ["git", "-C", str(repo), "log", "--oneline", f"--since={days} days ago",
             "--no-merges", "--format=%h %s (%ar)"],
            timeout=15,
        )
        if output:
            lines = output.split("\n")
            if len(lines) > 30:
                output = "\n".join(lines[:30]) + f"\n... ({len(lines) - 30} more)"
            sections.append(f"### {repo.name}\n{output}")
    return "\n\n".join(sections) if sections else "[No recent commits]"


def load_brain_index() -> str:
    """Load Brain doc index (all docs across sections)."""
    if not BRAIN_SCRIPT.exists():
        return "[Brain unavailable]"
    output = run_cmd(["python3", str(BRAIN_SCRIPT), "index"], timeout=30)
    return output or "[Brain index unavailable]"


def load_system_state() -> str:
    """Load all system state for reconciliation context."""
    print("Loading system state...")

    konban = load_konban_state()
    print(f"  Konban: {len(konban)} chars")

    brain = load_brain_active_context()
    print(f"  Brain Active Context: {len(brain)} chars")

    brain_index = load_brain_index()
    print(f"  Brain Index: {len(brain_index)} chars")

    decisions = load_recent_decisions()
    print(f"  Recent decisions: {len(decisions)} chars")

    git_history = load_git_history()
    print(f"  Git history: {len(git_history)} chars")

    return f"""## Current Konban Board (active tasks)
{konban}

## Active Context (KH Brain)
{brain}

## Brain Document Index (all existing docs by section)
{brain_index}

## Recent Decisions (KB)
{decisions}

## Recent Git Commits (last 3 days)
{git_history}"""


STATE_CONSISTENCY_PROMPT = """You are a state-consistency checker for a personal knowledge management system.

You receive the CURRENT STATE of the user's system: Active Context (strategic priorities), Konban board (task tracker), Brain doc index (knowledge base), and recent git commits.

Your job: find items in Active Context or Konban that are stale — the work they describe is already done (fully or partially) based on git commit evidence.

PROCEDURE — follow these steps:
1. List each Active Context priority/milestone and each Konban "Doing"/"To Do" task
2. For each item, scan ALL git commits for semantic matches (not just keyword matches)
3. If a priority has SUB-ITEMS (e.g., workstreams a/b/c), check each sub-item independently
4. Flag any item (or sub-item) where git commits show the work is shipped

MATCHING RULES:
- Match SEMANTICALLY, not just by keyword. "increase character limits ~3x" matches "increase profile char limits"
- "Werdegang" = "Mein Weg zu dieser Arbeit" (same field, German synonyms)
- feat() commits = feature shipped. fix() commits for the same area = iterative work on that feature.
- Multiple commits touching the same feature area = strong signal the work is done
- A commit prefixed with "Ship:" means explicit production deployment

WHAT TO FLAG:
- Items where ALL the described work is done → flag as "completed"
- Items where SOME sub-items are done → flag as "partially_completed" with details on what's done vs remaining
- Konban tasks in "Doing" or "To Do" where the work is visible in commits

EXAMPLE MATCH:
  Active Context says: "Profile depth — increase profile char limits + add Werdegang field"
  Git shows: "e56fd42 feat(profile): increase character limits ~3x and add 'Mein Weg zu dieser Arbeit' field"
  → This is a MATCH. "Mein Weg zu dieser Arbeit" IS the Werdegang field. Flag it.

EXAMPLE NON-MATCH:
  Active Context says: "Passwordless patient accounts (EARTH-292)"
  Git shows: "fix(portal): default to Profile tab for new therapists"
  → NOT a match. The commit is about therapist portal, not patient accounts.

Return ONLY valid JSON:
{
  "stale_items": [
    {
      "source": "active_context | konban",
      "item": "what's claimed as in-progress or planned",
      "status": "completed | partially_completed",
      "evidence": "git commit hash(es) and what they did",
      "remaining": "what sub-items are NOT yet done (null if fully completed)",
      "recommendation": "what should be updated"
    }
  ],
  "summary": "1 sentence summary"
}

If nothing is stale, return empty stale_items array. But be thorough — missing a stale item means the user wastes attention on work that's already done."""


# --- Model calls ---

def call_state_consistency_check(system_state: str, model: str = DEFAULT_MODEL) -> dict:
    """Check system state for internal inconsistencies (Active Context vs git, etc.)."""
    api_key = get_api_key()

    user_content = (
        "Check this system state for inconsistencies.\n\n"
        f"<system_state>\n{system_state}\n</system_state>\n\n"
        "Return ONLY valid JSON."
    )

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": STATE_CONSISTENCY_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.1,
        "provider": {"data_collection": "deny"},
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/kkaufmann/knowledge-base",
            "X-Title": "KB State Consistency",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"  State consistency check failed: {e}", file=sys.stderr)
        return {"stale_items": [], "summary": "Check failed"}

    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        return {"stale_items": [], "summary": "Empty response"}

    # Parse JSON (same cleanup as reconciliation)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]
    if "<think>" in content:
        content = content.split("</think>")[-1].strip()
    if not content.startswith("{"):
        idx = content.find("{")
        if idx >= 0:
            content = content[idx:]

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"stale_items": [], "summary": "Parse failed"}


def call_reconciliation_model(artifacts_json: str, system_state: str, model: str = DEFAULT_MODEL) -> dict:
    """Call GLM-5 to reconcile artifacts against system state."""
    api_key = get_api_key()

    user_content = (
        "Reconcile these artifacts against the current system state.\n\n"
        f"<artifacts>\n{artifacts_json}\n</artifacts>\n\n"
        f"<system_state>\n{system_state}\n</system_state>\n\n"
        "Return ONLY valid JSON with your reconciliation results."
    )

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": RECONCILIATION_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.1,
        "provider": {"data_collection": "deny"},
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/kkaufmann/knowledge-base",
            "X-Title": "KB Reconciliation",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Error: OpenRouter API returned {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Could not reach OpenRouter: {e.reason}", file=sys.stderr)
        sys.exit(1)

    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        print("Error: Empty response from model", file=sys.stderr)
        sys.exit(1)

    # Parse JSON
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]
    if "<think>" in content:
        content = content.split("</think>")[-1].strip()
    if "<output>" in content:
        content = content.split("<output>")[1].split("</output>")[0].strip()
    if not content.startswith("{"):
        idx = content.find("{")
        if idx >= 0:
            content = content[idx:]

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"Error parsing reconciliation response: {e}", file=sys.stderr)
        print(f"Raw (first 500 chars):\n{content[:500]}", file=sys.stderr)
        sys.exit(1)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Reconcile pending artifacts against system state")
    parser.add_argument("--artifacts", help="Path to artifacts JSON (default: pending file)")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL,
                        help=f"OpenRouter model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show action plan without executing")
    parser.add_argument("--output", "-o", help="Save action plan to JSON file")
    parser.add_argument("--execute", action="store_true",
                        help="Pass action plan directly to executor")
    parser.add_argument("--consistency-only", action="store_true",
                        help="Only run state consistency check (no artifact reconciliation)")

    args = parser.parse_args()

    # Always load system state (needed for both consistency check and reconciliation)
    system_state = load_system_state()
    print()

    # State consistency check — runs always, even with no artifacts
    print(f"Running state consistency check ({args.model})...")
    consistency = call_state_consistency_check(system_state, args.model)
    stale_items = consistency.get("stale_items", [])
    if stale_items:
        print(f"\n  STALE STATE DETECTED ({len(stale_items)} item(s)):")
        for item in stale_items:
            print(f"  [{item.get('source', '?')}] {item.get('item', '?')}")
            print(f"    Evidence: {item.get('evidence', '?')}")
            print(f"    → {item.get('recommendation', '?')}")
            print()
    else:
        print("  State is consistent.")
    print()

    if args.consistency_only:
        if args.output:
            with open(args.output, "w") as f:
                json.dump(consistency, f, indent=2, default=str)
            print(f"Consistency report saved to {args.output}")
        return

    # Load artifacts
    artifacts_path = args.artifacts or str(PENDING_FILE)
    has_artifacts = False
    actionable = []

    if os.path.exists(artifacts_path):
        with open(artifacts_path) as f:
            artifacts = json.load(f)
        actionable = [a for a in artifacts
                      if (a.get("persistence_status") != "persisted"
                          and a.get("value", "low") in ("very_high", "medium"))
                      or a.get("type") == "error_pattern"]
        has_artifacts = len(actionable) > 0

    if not has_artifacts and not stale_items:
        print("No pending artifacts and state is consistent. Nothing to do.")
        if artifacts_path == str(PENDING_FILE) and os.path.exists(artifacts_path):
            PENDING_FILE.write_text("[]")
        return

    # Build action plan — combine artifact reconciliation + consistency findings
    action_plan = {"proposed_actions": [], "conflicts_flagged": [], "summary": ""}

    # Artifact reconciliation (if any)
    if has_artifacts:
        print(f"Reconciling {len(actionable)} artifact(s)...")
        artifacts_json = json.dumps(actionable, indent=2, default=str)
        total_input = len(artifacts_json) + len(system_state)
        print(f"Calling {args.model} ({total_input} chars total input)...")
        action_plan = call_reconciliation_model(artifacts_json, system_state, args.model)

    # Merge stale state findings as conflicts
    for item in stale_items:
        status = item.get("status", "completed")
        remaining = item.get("remaining")
        detail = f"[{status}] {item.get('evidence', '')}"
        if remaining:
            detail += f" | Remaining: {remaining}"
        detail += f" — {item.get('recommendation', 'Review needed')}"
        action_plan.setdefault("conflicts_flagged", []).append({
            "artifact": f"[state-check] {item.get('source', '?')}",
            "conflicts_with": item.get("item", "?"),
            "recommendation": detail,
        })

    # Display results
    actions = action_plan.get("proposed_actions", [])
    conflicts = action_plan.get("conflicts_flagged", [])
    summary = action_plan.get("summary", "")

    print(f"\nReconciliation complete:")
    print(f"  Actions proposed: {len(actions)}")
    print(f"  Conflicts flagged: {len(conflicts)}")
    if summary:
        print(f"  Summary: {summary}")
    print()

    for a in actions:
        atype = a.get("type", "?")
        target = a.get("title") or a.get("target", "?")
        print(f"  [{atype}] {target}")
        if a.get("rationale"):
            print(f"    → {a['rationale']}")
        print()

    for c in conflicts:
        print(f"  [CONFLICT] {c.get('artifact', '?')}")
        print(f"    vs: {c.get('conflicts_with', '?')}")
        print(f"    → {c.get('recommendation', '?')}")
        print()

    # Save action plan
    if args.output:
        with open(args.output, "w") as f:
            json.dump(action_plan, f, indent=2, default=str)
        print(f"Action plan saved to {args.output}")

    if args.dry_run:
        print("[DRY RUN — no actions taken]")
        return

    # Execute if requested
    if args.execute:
        executor_path = Path(__file__).resolve().parent / "executor.py"
        if executor_path.exists():
            plan_file = "/tmp/reconciliation-plan.json"
            with open(plan_file, "w") as f:
                json.dump(action_plan, f, indent=2, default=str)

            print(f"\nExecuting action plan...")
            result = subprocess.run(
                ["python3", str(executor_path), "--plan", plan_file],
                timeout=120,
            )

            if result.returncode == 0 and artifacts_path == str(PENDING_FILE):
                PENDING_FILE.write_text("[]")
                print("Pending artifacts cleared.")
        else:
            print(f"Executor not found at {executor_path}")
    else:
        # Clear pending since we've reconciled (action plan is saved or displayed)
        if artifacts_path == str(PENDING_FILE):
            PENDING_FILE.write_text("[]")
            print("Pending artifacts cleared.")


if __name__ == "__main__":
    main()
