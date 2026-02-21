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
from extract import (
    get_api_key,
    parse_session_jsonl,
    parse_session_incremental,
    save_session_offset,
    _load_session_offsets,
    detect_session_domain,
)

# --- Config ---

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "z-ai/glm-5"  # GLM-5: precision > cost > speed (ADR-004)
KB_DIR = Path.home() / ".claude" / "knowledge"
PENDING_FILE = KB_DIR / "artifacts-pending.json"
ARTIFACT_OFFSETS_FILE = KB_DIR / ".artifact-offsets.json"

# Separate offset tracking from fact extraction — they may run at different
# cadences or one may fail while the other succeeds.

# --- Artifact Extraction Prompt ---

EXTRACTION_PROMPT = """You are an artifact extraction system for a personal knowledge management pipeline.

Your job: read a conversation transcript and REPRODUCE structured work products that have durable value beyond the session. You are creating the permanent record — if you don't reproduce the content, it is lost forever.

IMPORTANT DISTINCTIONS:
- ARTIFACTS (extract these): Plans with ordered steps, strategic analyses with sections, decision frameworks, roadmaps with milestones, architectural designs, interview analysis summaries
- NOT ARTIFACTS (skip these): Casual discussion, code snippets (already in git), daily standup dashboards (ephemeral), simple Q&A, tool output, error messages, status updates
- EPHEMERAL (skip these): Capacity snapshots, daily schedules, meeting agendas — these go stale within days

For each artifact found, assess:
1. TYPE: plan | analysis | framework | decision | roadmap | error_pattern
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

Also identify ERROR PATTERNS: places where the assistant used a tool incorrectly, got an error, and had to retry. These are skill improvement signals.

If the transcript contains a section marked "[--- CONTEXT FROM PREVIOUS EXTRACTION ---]", that section is already processed. Only extract artifacts from the "[--- NEW MESSAGES BELOW ---]" section. Use the context section only for understanding references in the new messages.

Return ONLY valid JSON:
{
  "artifacts": [
    {
      "type": "analysis",
      "title": "Short descriptive title",
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
      "tool": "konban",
      "command": "create --description",
      "error_summary": "Flag not supported",
      "resolution": "Used create + log instead",
      "suggested_fix": "Add --description to create command or document limitation"
    }
  ],
  "session_summary": "1-2 sentence summary of the overall session"
}

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


# --- Model call ---

def call_extraction_model(transcript: str, model: str = DEFAULT_MODEL) -> dict:
    """Call GLM-5 via OpenRouter to extract artifacts."""
    api_key = get_api_key()

    # Wrap transcript in XML tags so the model doesn't confuse it with conversation
    user_content = (
        "Extract artifacts from the following transcript. "
        "The transcript is wrapped in <transcript> tags — analyze it, do NOT continue it.\n\n"
        f"<transcript>\n{transcript}\n</transcript>\n\n"
        "Now return ONLY valid JSON with your extraction results."
    )

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.1,  # Low temp for precision
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/kkaufmann/knowledge-base",
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
        print(f"Incremental: {msg_count} new messages, {ctx_count} context overlap")

    if not transcript.strip():
        print("Empty transcript.")
        sys.exit(0)

    # Truncate if needed
    if len(transcript) > 60000:  # GLM-5 has 205K context, but keep reasonable
        print(f"Transcript is {len(transcript)} chars, truncating to 60000...")
        transcript = transcript[-60000:]

    domain = detect_session_domain(session_path)
    print(f"Extracting artifacts from {len(transcript)} chars...")
    print(f"Model: {args.model} | Domain: {domain or 'unknown'}")
    print()

    # Call model
    result = call_extraction_model(transcript, args.model)

    artifacts = result.get("artifacts", [])
    errors = result.get("error_patterns", [])
    summary = result.get("session_summary", "")

    print(f"Found: {len(artifacts)} artifact(s), {len(errors)} error pattern(s)")
    if summary:
        print(f"Session: {summary}")
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
    actionable = [a for a in artifacts
                  if a.get("value") in ("very_high", "medium")
                  and a.get("persistence_status") != "persisted"]

    # Error patterns are always actionable (they're skill improvement signals)
    error_artifacts = [{"type": "error_pattern", **e} for e in errors]

    to_save = actionable + error_artifacts

    if to_save:
        count = append_pending(to_save, session_path, domain=domain)
        print(f"Saved {count} artifact(s) to pending file")
    else:
        print("No actionable artifacts to save (all persisted or low value)")

    # Save offset after successful extraction
    if new_end_offset >= 0:
        _set_artifact_offset(session_path, new_end_offset)
        print(f"Offset saved: {session_key} → {new_end_offset}")


if __name__ == "__main__":
    main()
