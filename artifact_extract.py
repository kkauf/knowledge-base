#!/usr/bin/env python3
"""
Reconciliation Pipeline — Stage 1: Artifact Extraction

Identifies structured work products (plans, analyses, roadmaps, frameworks)
in Claude Code session transcripts. Runs alongside fact extraction in the daemon.

Uses GLM-5 via OpenRouter for precision (ADR-004 benchmark decision).

Usage:
    # Extract artifacts from a session (incremental, offset-tracked)
    python3 artifact_extract.py --session path/to/session.jsonl

    # Dry-run (show artifacts, don't save to pending file)
    python3 artifact_extract.py --session path/to/session.jsonl --dry-run

    # Force full re-extraction (ignore offsets)
    python3 artifact_extract.py --session path/to/session.jsonl --no-incremental

    # Show pending artifacts
    python3 artifact_extract.py --show-pending
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Reuse parsing infrastructure from extract.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (get_openrouter_url, get_reconciliation_model, get_kb_dir,
                    get_pending_file, get_artifact_offsets_file, get_api_key,
                    get_http_referer, get_skills_dir)
from extract import (
    parse_session_jsonl,
    parse_session_incremental,
    save_session_offset,
    _load_session_offsets,
    detect_session_domain,
    _parse_tool_error_sequences,
)

# --- Config ---

OPENROUTER_URL = get_openrouter_url()
DEFAULT_MODEL = get_reconciliation_model()
KB_DIR = get_kb_dir()
PENDING_FILE = get_pending_file()
ARTIFACT_OFFSETS_FILE = get_artifact_offsets_file()

# Separate offset tracking from fact extraction — they may run at different
# cadences or one may fail while the other succeeds.

# --- Skill Inventory ---

def _build_skill_inventory() -> list[str]:
    """Discover valid skill names from the filesystem."""
    skills_dir = Path(get_skills_dir())
    if not skills_dir.exists():
        return []
    return sorted([d.name for d in skills_dir.iterdir()
                   if d.is_dir() and (d / "SKILL.md").exists()])


def _build_extraction_prompt() -> str:
    """Build extraction prompt with dynamic skill inventory injected."""
    skills = _build_skill_inventory()
    if skills:
        skill_block = (
            "\n\nVALID SKILL NAMES (use ONLY these for error_pattern \"skill\" field, or null if none match):\n"
            + ", ".join(skills)
            + "\n\nDo NOT invent skill names. If the error doesn't map to one of these skills, set \"skill\" to null."
        )
    else:
        skill_block = ""
    return EXTRACTION_PROMPT + skill_block


# --- Artifact Extraction Prompt ---

EXTRACTION_PROMPT = """You are an artifact extraction system for a personal knowledge management pipeline.

Your job: read a conversation transcript and REPRODUCE structured work products that have durable value beyond the session. You are creating the permanent record — if you don't reproduce the content, it is lost forever.

IMPORTANT DISTINCTIONS:
- ARTIFACTS (extract these): Plans with ordered steps, strategic analyses with sections, decision frameworks, roadmaps with milestones, architectural designs, interview analysis summaries, research briefs with lasting reference value
- NOT ARTIFACTS (skip these): Casual discussion, code snippets (already in git), daily standup dashboards (ephemeral), simple Q&A, tool output, error messages, status updates
- IMPLEMENTATION ARTIFACTS (skip these — they belong in git/Linear, not the knowledge base):
  * Debugging narratives: "why X overflows at 375px", "root cause of API 400 error", test failure investigations
  * Codebase inventories: listing all templates, all test files, all endpoints, all email types
  * Session play-by-play summaries: "first we fixed X, then we fixed Y, then we committed"
  * Single-bug post-mortems: fix is already shipped, lesson is narrow and implementation-specific
  * CSS/layout audits, mobile optimization checklists, E2E test analyses
  * Documentation TODO lists: "these files need updating" — the git diff is the source of truth
  * Architecture dumps that just describe current code structure without strategic insight
- EPHEMERAL (skip these): Capacity snapshots, daily schedules, meeting agendas — these go stale within days

COMMITMENT UPDATES (extract these when CONTEXT FRAME is present):
When conversations discuss scheduling, progress, or emotional friction related to ACTIVE COMMITMENTS listed in the context frame, extract a "commitment_update" artifact. These track changes to tracked commitments that would otherwise be lost when the session ends.

Examples of commitment updates:
- "MBA work moved from Thursday to Tuesday" → commitment_update (reschedule)
- "Difficulty committing to MBA group project, keep postponing" → commitment_update (friction)
- "MBA group presentation is done, submitted" → commitment_update (completion)
- "Made progress on Stripe Connect — payment flow works in staging" → commitment_update (progress)

For commitment_update artifacts, include these extra fields:
- "commitment_target": the Konban task title this relates to (fuzzy match OK)
- "update_type": "reschedule" | "progress" | "friction" | "completion"

For each artifact found, assess:
1. TYPE: plan | analysis | framework | decision | roadmap | error_pattern | commitment_update
2. VALUE: very_high (strategic, multi-paragraph, decision-bearing) | medium (useful reference) | low (nice-to-have)
3. PERSISTENCE CHECK: Look for signals that it was already saved:
   - Tool calls to notion-api.py, konban, Brain, MEMORY.md in subsequent messages
   - Explicit mentions: "Logged", "Created", "Saved to", "Added to Brain"
   - If no persistence signal within ~5 messages after the artifact → mark as "not_persisted"

CRITICAL — CONTENT REPRODUCTION RULES:
- The "content" field must contain the FULL artifact, not a summary or excerpt.
- Reproduce the actual analysis, plan steps, framework, or decision with ALL details: headings, bullet points, data points, reasoning, confidence levels, caveats, recommendations.
- If the artifact spans multiple messages, reconstruct it into a coherent document.
- NEVER write "includes X" or "contains Y" — actually include X and Y.
- If you can't fit the full content, prioritize the parts that are NOT persisted elsewhere (the gap between what was saved and what was discussed).
- For "partial" persistence: focus on what was NOT saved. The Brain/Konban already has the summary — reproduce the reasoning, data, and nuance that was lost.

Also identify ERROR PATTERNS: places where the assistant used a tool suboptimally — not just hard errors, but also wasted round trips. Examples:
- Hard errors: wrong arg types, invalid flags, exceptions
- Soft misses: "project not found", "no active tasks matching X" then retrying with different name
- Discovery calls: running --help or bare command to learn the API (means SKILL.md was insufficient)
- Parameter hunting: multiple retries with slightly different args until one works
- Source code reading: Claude READ the helper script source code (means SKILL.md was insufficient)
- Identical retries: Same command run multiple times without changes (tool output was insufficient)
- Escalation cascades: Skill helper → raw curl/urllib/API calls (skill couldn't do what was needed)
- Output truncation: Tool returned short/clipped output, then Claude tried workarounds (script truncates data)
- Skill inspection: Claude searched/grepped the skill directory to understand capabilities (SKILL.md gap)

These are ALL skill improvement signals — each wasted call is a SKILL.md doc gap.

IMPORTANT — TOOL ERROR DATA:
If a <tool_errors> section is included below the transcript, it contains STRUCTURED suboptimal call sequences extracted directly from tool_use/tool_result blocks (the transcript text may not show these). Use this data to produce more precise error_patterns. Each entry shows the failed/wasted command, output, and (if available) the successful command.

If the transcript contains a section marked "[--- CONTEXT FROM PREVIOUS EXTRACTION ---]", that section is already processed. Only extract artifacts from the "[--- NEW MESSAGES BELOW ---]" section. Use the context section only for understanding references in the new messages.

CATEGORY CLASSIFICATION (required on every artifact):
Classify each artifact into the taxonomy below. The taxonomy is a disambiguation
hint, not a rigid ontology — create new sub-categories when needed.

Personal: Health | Sports | Baking | Family | Home | Finance | Education | Travel | Pets
Business/KH: Product | Therapists | Billing | Marketing | Infrastructure
Business/Consulting: [client name as sub-category]
Business/KE: LLC admin | Legal | Brand
Infrastructure: Knowledge Base | Claude Config | Automation

DISAMBIGUATION RULES:
- The CONTENT determines category, not the mechanism — "ordering supplements" is
  Personal/Health (the content), not Personal/Finance (the transaction).
- The SESSION PROJECT does NOT determine category. Personal insurance discussed
  during a KH coding session → Personal/Finance, NOT Business/KH/Billing. A KH
  email template mentioned in a standup → Business/KH/Marketing, not Personal.
  Classify by the artifact's SUBJECT MATTER, ignoring which project the session is in.
- Business/KH/Billing = KH platform billing (therapist commissions, Stripe, invoicing
  for KH services). Personal insurance, rent, personal bank accounts → Personal/Finance.

Include 3-5 key_terms that distinguish this artifact from others in the same category.

TITLE QUALITY: If you cannot produce a meaningful, descriptive title for an artifact,
it is probably not a standalone artifact — skip it. Never output "?" or empty titles.

Return ONLY valid JSON:
{
  "artifacts": [
    {
      "type": "analysis",
      "title": "Short descriptive title",
      "category": "Personal",
      "sub_category": "Health",
      "key_terms": ["supplements", "Micro Ingredients", "order"],
      "summary": "1-2 sentence summary for the pending queue (NOT the artifact itself)",
      "value": "very_high",
      "persistence_status": "not_persisted | persisted | partial",
      "persistence_evidence": "Description of what was/wasn't saved, or null",
      "content": "THE FULL ARTIFACT CONTENT — reproduced from the transcript as a complete, standalone document. Use markdown formatting. This is what gets saved to the knowledge base.",
      "entities_referenced": ["entity1", "entity2"]
    }
  ],
  "error_patterns": [
    {
      "skill": "konban",
      "script": "notion-api.py",
      "tool": "konban",
      "command": "create --description",
      "error_type": "missing_flag | wrong_arg_type | invalid_value | case_sensitivity | other",
      "error_summary": "Flag not supported",
      "correct_usage": "Use create + log instead (create does not accept --description)",
      "resolution": "Used create + log instead",
      "suggested_fix": "Add --description to create command or document limitation",
      "doc_gap": true
    }
  ],
  "session_summary": "1-2 sentence summary of the overall session",
  "session_memory": {
    "one_liner": "Compressed recall line (<100 chars)",
    "topics": ["topic1", "topic2"],
    "summary": "Topic-organized prose (50-300 words). Entities, decisions, personal knowledge."
  }
}

For error_patterns:
- "skill": one of the VALID SKILL NAMES listed below, or null if none match. NEVER invent skill names like "infrastructure", "supabase", "knowledge-base", etc.
- "script": the helper script filename (e.g., "notion-api.py", "linear-api.py")
- "error_type": classify the issue:
  * Hard errors: wrong_arg_type (string where int expected), invalid_value ("High" when "3" needed), case_sensitivity ("feature" vs "Feature"), missing_flag (flag doesn't exist)
  * Inefficiencies: inefficient_lookup (searched for wrong name/entity, "not found" then retry), discovery_call (ran --help or bare command to discover API — SKILL.md was insufficient)
  * Cross-tool patterns: source_reading (Read of helper .py source), identical_retry (same command repeated), escalation_cascade (skill → raw API fallback), output_truncation (short output + workaround attempts), skill_inspection (Grep/Glob of skill directory)
  * Fallback: other
- "correct_usage": the CORRECT way to invoke the command (from the successful retry or your analysis). For inefficient_lookup, include the correct entity name/search term.
- "doc_gap": true if this issue is likely because the SKILL.md documentation is missing or unclear about this constraint (e.g., valid project names, exact entity names, API shape). false if the info is probably already documented and Claude just ignored it.

SESSION MEMORY (always produce — even if no artifacts found):
Beyond the 1-2 sentence session_summary, produce a "session_memory" object that captures
knowledge with LASTING VALUE beyond the session. This is the permanent record — if you don't
capture it, it's lost forever.

"session_memory": {
  "one_liner": "Compressed 1-line version for recall injection (<100 chars). What happened + key outcome.",
  "topics": ["topic1", "topic2"],
  "summary": "Topic-organized prose paragraphs (50-300 words). Include: entities mentioned, decisions made, personal knowledge, commitments, outcomes. Organize by topic, not chronologically. Skip pure code/ops work. If nothing worth remembering, return empty string."
}

What to capture in session_memory:
- Personal knowledge: purchases, plans, health, family context, schedules
- Decisions and their rationale (why, not just what)
- Commitments: what was promised, to whom, by when
- Strategic insights: market findings, pricing decisions, competitive analysis
- Navigational knowledge: where things were saved, document locations
- Status changes: what shipped, what broke, what was deferred

What to skip:
- Pure code changes (already in git)
- Tool output / API responses
- Debugging narratives
- Ephemeral task coordination
- Content that was ALREADY WRITTEN to documents, Brain, or files during the session
  (check the ALREADY PERSISTED list if provided — those are the gap boundaries)

DEDUPLICATION RULE: If the session wrote to documents/Brain/files (listed in ALREADY PERSISTED),
the session_memory should capture CONTEXT and MOTIVATION — not repeat the content.
Good: "Updated autism profile with new meltdown patterns identified in conversation with Katherine"
Bad: [repeating the actual profile content that was already written to the file]

If no artifacts or error patterns found, return empty arrays. Do NOT hallucinate artifacts that aren't in the transcript."""


# --- Offset tracking (separate from fact extraction offsets) ---

def _load_artifact_offsets() -> dict:
    if ARTIFACT_OFFSETS_FILE.exists():
        try:
            return json.loads(ARTIFACT_OFFSETS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_artifact_offsets(offsets: dict):
    ARTIFACT_OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_OFFSETS_FILE.write_text(json.dumps(offsets, indent=2))


def _get_artifact_offset(session_path: str) -> int:
    key = os.path.basename(session_path)
    return _load_artifact_offsets().get(key, -1)


def _set_artifact_offset(session_path: str, offset: int):
    key = os.path.basename(session_path)
    offsets = _load_artifact_offsets()
    offsets[key] = offset
    _save_artifact_offsets(offsets)


# --- Persistence signal detection ---

def _detect_persistence_signals(session_path: str) -> list[str]:
    """Detect what was written to external systems during a session.

    Parses tool_use blocks for Brain/Notion writes, file edits (MEMORY.md, docs/,
    SKILL.md), git commits, and Konban/Linear task creation. Returns human-readable
    list of what's already persisted, so the session_memory can focus on gaps.
    """
    signals = []
    try:
        with open(session_path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("type") != "assistant":
                        continue
                    content = msg.get("message", {}).get("content", "")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        # Notion/Brain writes
                        if "notion" in name.lower() and any(
                            w in name.lower() for w in ("create", "update")
                        ):
                            title = str(inp.get("title", inp.get("page_id", "")))[:50]
                            signals.append(f"Notion write: {title}")
                        # File edits — any .md/.txt/.json doc is persistence
                        elif name in ("Edit", "Write"):
                            path = inp.get("file_path", "")
                            bname = os.path.basename(path)
                            ext = os.path.splitext(path)[1].lower()
                            # Skip source code files — those are git-tracked, not docs
                            code_exts = {".ts", ".tsx", ".js", ".jsx", ".py", ".css",
                                         ".html", ".sh", ".sql", ".yaml", ".yml"}
                            if ext in (".md", ".txt") or any(
                                k in path for k in ("MEMORY.md", "/docs/", "SKILL.md")
                            ):
                                signals.append(f"File write: {bname}")
                            elif ext not in code_exts and bname not in ("package.json", "tsconfig.json"):
                                signals.append(f"File write: {bname}")
                        # Git commits
                        elif name == "Bash":
                            cmd = inp.get("command", "")
                            if "git commit" in cmd:
                                signals.append("Git commit")
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return list(dict.fromkeys(signals))  # dedupe preserving order


# --- Model call ---

def call_extraction_model(transcript: str, model: str = DEFAULT_MODEL,
                          context_frame: str = "", tool_errors: list = None,
                          linked_task: dict = None,
                          persistence_signals: list = None) -> dict:
    """Call GLM-5 via OpenRouter to extract artifacts."""
    api_key = get_api_key()

    # Build user content with optional task link, context frame, and tool errors
    parts = []
    if persistence_signals:
        parts.append(
            "ALREADY PERSISTED (these were written to external systems during the session — "
            "session_memory should NOT duplicate this content, focus on gaps):\n"
            + "\n".join(f"- {s}" for s in persistence_signals)
            + "\n\n"
        )
    if linked_task:
        parts.append(
            "LINKED TASK: This session is explicitly linked to a Konban task. "
            "Use the task's context as a STRONG prior for artifact classification.\n\n"
            f"<linked_task>\n"
            f"Task: {linked_task['title']}\n"
            f"Status: {linked_task['status']}\n"
            f"Priority: {linked_task['priority']}\n"
            f"</linked_task>\n\n"
            "Unless the conversation clearly shifts to a different topic, "
            "classify artifacts based on this task's domain. "
            "commitment_update artifacts should reference this task as the target.\n\n"
        )
    if context_frame:
        parts.append(
            "The following CONTEXT FRAME shows the user's active commitments and priorities. "
            "Use it to identify commitment_update artifacts.\n\n"
            f"<context_frame>\n{context_frame}\n</context_frame>\n\n"
        )
    if tool_errors:
        errors_text = json.dumps(tool_errors, indent=2)
        parts.append(
            "The following TOOL ERRORS were extracted from actual tool_use/tool_result blocks "
            "in the session. These show exact commands that failed and (when available) the "
            "successful retry. Use these to produce precise error_patterns.\n\n"
            f"<tool_errors>\n{errors_text}\n</tool_errors>\n\n"
        )
    parts.append(
        "Extract artifacts from the following transcript. "
        "The transcript is wrapped in <transcript> tags — analyze it, do NOT continue it.\n\n"
        f"<transcript>\n{transcript}\n</transcript>\n\n"
        "Now return ONLY valid JSON with your extraction results."
    )
    user_content = "".join(parts)

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _build_extraction_prompt()},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.1,  # Low temp for precision
        "provider": {"data_collection": "deny"},
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            **({"HTTP-Referer": get_http_referer()} if get_http_referer() else {}),
            "X-Title": "KB Artifact Extraction",
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

    # Parse JSON (handle markdown fences, thinking tags)
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
        print(f"Error parsing model response as JSON: {e}", file=sys.stderr)
        print(f"Raw (first 500 chars):\n{content[:500]}", file=sys.stderr)
        sys.exit(1)


# --- Pending file management ---

def load_pending() -> list:
    """Load pending artifacts from file."""
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_pending(artifacts: list):
    """Save pending artifacts to file."""
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(artifacts, indent=2))


def append_pending(new_artifacts: list, session_path: str, domain: str = None):
    """Append new artifacts to the pending file with metadata."""
    existing = load_pending()
    timestamp = datetime.now(timezone.utc).isoformat()
    session_id = os.path.basename(session_path)

    for artifact in new_artifacts:
        artifact["_meta"] = {
            "extracted_at": timestamp,
            "source_session": session_id,
            "domain": domain,
        }
        existing.append(artifact)

    save_pending(existing)
    return len(new_artifacts)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Extract structured artifacts from session transcripts")
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--session", help="Path to Claude Code .jsonl session file")
    input_group.add_argument("--show-pending", action="store_true", help="Show pending artifacts")

    parser.add_argument("--model", "-m", default=DEFAULT_MODEL,
                        help=f"OpenRouter model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show artifacts without saving to pending file")
    parser.add_argument("--no-incremental", action="store_true",
                        help="Disable offset tracking (process last 50 messages)")

    args = parser.parse_args()

    if args.show_pending:
        pending = load_pending()
        if not pending:
            print("No pending artifacts.")
            return
        print(f"{len(pending)} pending artifact(s):\n")
        for i, a in enumerate(pending):
            meta = a.get("_meta", {})
            print(f"  [{i+1}] {a.get('type', '?')} — {a.get('title', '?')}")
            print(f"      Value: {a.get('value', '?')} | Persisted: {a.get('persistence_status', '?')}")
            print(f"      From: {meta.get('source_session', '?')} at {meta.get('extracted_at', '?')}")
            print()
        return

    if not args.session:
        parser.print_help()
        return

    # Use artifact-specific offsets (independent of fact extraction offsets)
    # We temporarily swap the offset file to use our own
    from extract import _parse_all_messages, CONTEXT_OVERLAP

    # Pre-filter: compress transcript for artifact extraction
    artifact_filter = None
    try:
        from session_prefilter import filter_for_artifacts
        artifact_filter = filter_for_artifacts
    except ImportError:
        pass  # Graceful degradation

    session_path = args.session
    session_key = os.path.basename(session_path)

    if args.no_incremental:
        transcript = parse_session_jsonl(session_path)
        new_end_offset = -1
    else:
        # Incremental: use artifact-specific offsets
        last_offset = _get_artifact_offset(session_path)
        all_messages = _parse_all_messages(session_path)

        if not all_messages:
            print("No messages in session.")
            sys.exit(0)

        total = len(all_messages)

        if last_offset < 0:
            # First time — process last 50 messages
            start = max(0, total - 50)
            context_msgs = []
            new_msgs = all_messages[start:]
        else:
            new_start = last_offset + 1
            if new_start >= total:
                print(f"No new messages (offset at {last_offset}, total {total})")
                sys.exit(0)

            context_start = max(0, new_start - CONTEXT_OVERLAP)
            context_msgs = all_messages[context_start:new_start]
            new_msgs = all_messages[new_start:]

        # Apply artifact filter: keep long assistant messages + triggering user context
        pre_filter_chars = sum(len(m['content']) for m in new_msgs)
        if artifact_filter:
            context_msgs = artifact_filter(context_msgs) if context_msgs else []
            new_msgs = artifact_filter(new_msgs)
        post_filter_chars = sum(len(m['content']) for m in new_msgs)

        # Build transcript with context separator
        parts = []
        if context_msgs:
            parts.append("[--- CONTEXT FROM PREVIOUS EXTRACTION (for reference only, already processed) ---]")
            for m in context_msgs:
                parts.append(f"[{m['role']}]: {m['content']}")
            parts.append("")
            parts.append("[--- NEW MESSAGES BELOW (extract artifacts from these) ---]")
            parts.append("")

        for m in new_msgs:
            parts.append(f"[{m['role']}]: {m['content']}")

        transcript = "\n\n".join(parts)
        new_end_offset = all_messages[-1]["index"]

        msg_count = len(new_msgs)
        ctx_count = len(context_msgs)
        compression = f" (filtered from {pre_filter_chars:,} to {post_filter_chars:,} chars)" if artifact_filter else ""
        print(f"Incremental: {msg_count} new messages, {ctx_count} context overlap{compression}")

    if not transcript.strip():
        print("Empty transcript.")
        sys.exit(0)

    # Truncate if needed
    if len(transcript) > 60000:  # GLM-5 has 205K context, but keep reasonable
        print(f"Transcript is {len(transcript)} chars, truncating to 60000...")
        transcript = transcript[-60000:]

    domain = detect_session_domain(session_path)

    # Check if this session is linked to a Konban task
    linked_task = None
    try:
        from context_frame import get_task_for_session
        linked_task = get_task_for_session(session_path)
        if linked_task:
            print(f"Linked task: {linked_task['title']} [{linked_task['task_id'][:8]}]")
    except ImportError:
        pass

    # Load dynamic context frame (active commitments, priorities)
    context_frame = ""
    try:
        from context_frame import load_context_frame
        context_frame = load_context_frame()
        if context_frame:
            print(f"Context frame: {len(context_frame)} chars")
    except ImportError:
        pass  # Graceful degradation

    # Parse tool error sequences from the session JSONL
    tool_errors = []
    try:
        artifact_offset = _get_artifact_offset(session_path) if not args.no_incremental else -1
        tool_errors = _parse_tool_error_sequences(session_path, offset=artifact_offset)
        if tool_errors:
            print(f"Tool errors found: {len(tool_errors)} error sequence(s)")
    except Exception as e:
        print(f"Warning: tool error parsing failed: {e}", file=sys.stderr)

    # Detect what was already persisted during the session (for session_memory dedup)
    persistence_signals = _detect_persistence_signals(session_path)
    if persistence_signals:
        print(f"Persistence signals: {len(persistence_signals)} ({', '.join(persistence_signals[:3])}{'...' if len(persistence_signals) > 3 else ''})")

    print(f"Extracting artifacts from {len(transcript)} chars...")
    print(f"Model: {args.model} | Domain: {domain or 'unknown'}")
    print()

    # Call model with context frame, tool errors, linked task, and persistence signals
    result = call_extraction_model(transcript, args.model, context_frame,
                                    tool_errors=tool_errors if tool_errors else None,
                                    linked_task=linked_task,
                                    persistence_signals=persistence_signals if persistence_signals else None)

    artifacts = result.get("artifacts", [])
    errors = result.get("error_patterns", [])
    summary = result.get("session_summary", "")
    session_memory = result.get("session_memory", {})

    # Post-processing: filter garbage artifacts (no title, "?" title, single-char title)
    pre_filter = len(artifacts)
    artifacts = [a for a in artifacts
                 if a.get("title") and len(a["title"].strip()) > 3
                 and a["title"].strip() not in ("?", "??", "...", "N/A", "n/a", "Untitled")]
    if len(artifacts) < pre_filter:
        print(f"Filtered {pre_filter - len(artifacts)} artifact(s) with garbage titles")

    print(f"Found: {len(artifacts)} artifact(s), {len(errors)} error pattern(s)")
    if summary:
        print(f"Session: {summary}")
    if session_memory.get("one_liner"):
        print(f"Memory:  {session_memory['one_liner']}")
    print()

    for a in artifacts:
        value_icon = {"very_high": "***", "medium": "**", "low": "*"}.get(a.get("value", ""), "?")
        persisted = a.get("persistence_status", "?")
        print(f"  [{value_icon}] {a.get('type', '?')}: {a.get('title', '?')}")
        print(f"      {a.get('summary', '')}")
        print(f"      Persisted: {persisted}")
        if a.get("persistence_evidence"):
            print(f"      Evidence: {a['persistence_evidence']}")
        print()

    for e in errors:
        print(f"  [!] Error: {e.get('tool', '?')} {e.get('command', '?')}")
        print(f"      {e.get('error_summary', '')}")
        print(f"      Fix: {e.get('suggested_fix', '')}")
        print()

    if args.dry_run:
        print("[DRY RUN — nothing saved]")
        return

    # Filter to actionable artifacts (medium+ value, not already persisted)
    # commitment_update artifacts are always actionable (they update tracked state)
    actionable = [a for a in artifacts
                  if (a.get("value") in ("very_high", "medium")
                      and a.get("persistence_status") != "persisted")
                  or a.get("type") == "commitment_update"]

    # Error patterns are always actionable (they're skill improvement signals)
    error_artifacts = [{"type": "error_pattern", **e} for e in errors]

    to_save = actionable + error_artifacts

    if to_save:
        count = append_pending(to_save, session_path, domain=domain)
        print(f"Saved {count} artifact(s) to pending file")
    else:
        print("No actionable artifacts to save (all persisted or low value)")

    # Store session memory (ADR-007)
    if session_memory.get("summary") or session_memory.get("one_liner"):
        try:
            from session_memory import init_db as sm_init, store_summary, get_db as sm_get_db
            sm_db = sm_get_db()
            sm_init(sm_db)
            stored = store_summary(
                sm_db,
                session_path=session_path,
                domain=domain,
                summary=session_memory.get("summary", ""),
                one_liner=session_memory.get("one_liner", ""),
                topics=session_memory.get("topics", []),
            )
            sm_db.close()
            if stored:
                print(f"Session memory stored ({len(session_memory.get('summary', ''))} chars)")
        except Exception as e:
            print(f"Warning: session memory storage failed: {e}", file=sys.stderr)

    # Save offset after successful extraction
    if new_end_offset >= 0:
        _set_artifact_offset(session_path, new_end_offset)
        print(f"Offset saved: {session_key} → {new_end_offset}")


if __name__ == "__main__":
    main()
