#!/usr/bin/env python3
"""
Session Pre-Filter — Deterministic transcript compression for the extraction pipeline.

Zero LLM cost. Classifies sessions for routing (skip/run decisions) and builds
focused transcripts per pipeline stage.

Architecture:
  Raw JSONL → quick_classify() → routing decision
                                   ├─ skip both (subagent, tiny)
                                   ├─ facts only → filter_for_facts(messages)
                                   ├─ artifacts only → filter_for_artifacts(messages)
                                   └─ both → filtered transcripts per stage

Design principle (from session-picker):
  Claude's responses follow: opening remark → tool calls → closing summary.
  The first and last text blocks carry the signal ("gems"). The verbose middle
  (code explanations, tool descriptions, reasoning chains) is noise for extraction.

Usage:
    from session_prefilter import quick_classify, filter_for_facts, filter_for_artifacts

    stats = quick_classify(session_path)
    if stats["skip_facts"] and stats["skip_artifacts"]:
        advance_offset_and_return()

    messages = _parse_all_messages(session_path)
    fact_msgs = filter_for_facts(messages)
    artifact_msgs = filter_for_artifacts(messages)
"""

import json
import os
import re
from pathlib import Path


# --- System-reminder noise patterns (stripped from user messages) ---

_SYSTEM_REMINDER_RE = re.compile(
    r'<system-reminder>.*?</system-reminder>', re.DOTALL
)
_HOOK_OUTPUT_RE = re.compile(
    r'UserPromptSubmit hook success:.*?(?=\n\n|\Z)', re.DOTALL
)
_TASK_TOOLS_REMINDER_RE = re.compile(
    r'The task tools haven\'t been used recently\..*?'
    r'Make sure that you NEVER mention this reminder to the user',
    re.DOTALL
)


def _strip_noise(text: str) -> str:
    """Remove system-reminders, hook outputs, and other injected noise from message text."""
    text = _SYSTEM_REMINDER_RE.sub('', text)
    text = _HOOK_OUTPUT_RE.sub('', text)
    text = _TASK_TOOLS_REMINDER_RE.sub('', text)
    return text.strip()


# --- Session classification ---

def quick_classify(session_path: str) -> dict:
    """Classify a session for routing decisions. Fast — reads raw bytes, no full parse.

    Returns dict with:
        user_chars: int — total user text characters
        asst_chars: int — total assistant text characters
        msg_count: int — total user+assistant text messages
        is_subagent: bool — filename matches subagent pattern
        has_long_asst: bool — at least one assistant message > 1500 chars
        skip_facts: bool — recommended: skip fact extraction
        skip_artifacts: bool — recommended: skip artifact extraction
    """
    filename = os.path.basename(session_path)
    is_subagent = 'subagent' in str(session_path) or filename.startswith('agent-a')

    user_chars = 0
    asst_chars = 0
    msg_count = 0
    has_long_asst = False

    try:
        with open(session_path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    role = msg.get("type", "")
                    if role not in ("user", "assistant"):
                        continue

                    inner = msg.get("message", {})
                    content = inner.get("content", "") if isinstance(inner, dict) else ""

                    if isinstance(content, list):
                        text_parts = [c.get("text", "") for c in content
                                      if isinstance(c, dict) and c.get("type") == "text"]
                        content = "\n".join(text_parts)

                    if not content:
                        continue

                    msg_count += 1
                    if role == "user":
                        # Strip noise for accurate user content measurement
                        clean = _strip_noise(content)
                        user_chars += len(clean)
                    else:
                        asst_chars += len(content)
                        if len(content) > 1500:
                            has_long_asst = True

                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    # Routing decisions
    skip_facts = is_subagent and user_chars < 1500
    skip_artifacts = not has_long_asst

    return {
        "user_chars": user_chars,
        "asst_chars": asst_chars,
        "msg_count": msg_count,
        "is_subagent": is_subagent,
        "has_long_asst": has_long_asst,
        "skip_facts": skip_facts,
        "skip_artifacts": skip_artifacts,
    }


# --- Transcript filters ---

# Max chars for the "middle" of a long assistant message in fact extraction.
# Keeps first + last paragraphs (gems pattern), compresses the verbose middle.
_GEMS_HEAD = 300
_GEMS_TAIL = 300
_LONG_ASST_THRESHOLD = 800  # Messages longer than this get compressed


def _compress_assistant_message(content: str) -> str:
    """Compress a long assistant message to its 'gems' — first and last paragraphs.

    Mirrors the session-picker pattern: opening remark + closing summary carry
    the signal. The middle (verbose reasoning, code, tool descriptions) is noise
    for fact extraction.
    """
    if len(content) <= _LONG_ASST_THRESHOLD:
        return content

    head = content[:_GEMS_HEAD].rstrip()
    tail = content[-_GEMS_TAIL:].lstrip()
    skipped = len(content) - _GEMS_HEAD - _GEMS_TAIL
    return f"{head}\n[... {skipped} chars compressed ...]\n{tail}"


def filter_for_facts(messages: list) -> list:
    """Filter message list for fact extraction.

    Keeps:
    - All user messages (stripped of system-reminder noise) — facts, decisions, entities
    - Short assistant messages (<800 chars) — confirmations, acknowledgments
    - Compressed assistant messages (gems: first + last paragraphs) — conclusions

    Drops:
    - Verbose assistant middles (reasoning chains, code explanations, tool descriptions)
    - System-reminder / hook noise in user messages
    """
    filtered = []
    for m in messages:
        if m["role"] == "user":
            clean = _strip_noise(m["content"])
            if clean:
                filtered.append({**m, "content": clean})
        else:
            # Assistant message — compress if long
            compressed = _compress_assistant_message(m["content"])
            if compressed:
                filtered.append({**m, "content": compressed})
    return filtered


def filter_for_artifacts(messages: list) -> list:
    """Filter message list for artifact extraction.

    Keeps:
    - Long assistant messages (>1500 chars) IN FULL — plans, analyses, frameworks
    - The user message immediately before each long assistant message — triggering context
    - Short assistant messages that directly precede user messages (dialogue flow)

    Drops:
    - Short assistant filler ("Let me check...", "Done.", tool descriptions)
    - User messages that don't trigger long responses (noise for artifact detection)
    - System-reminder / hook noise
    """
    filtered = []
    n = len(messages)

    # First pass: identify indices of long assistant messages
    long_asst_indices = set()
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and len(m["content"]) > 1500:
            long_asst_indices.add(i)

    for i, m in enumerate(messages):
        if i in long_asst_indices:
            # Long assistant message — keep in full (this is where artifacts live)
            filtered.append(m)
        elif m["role"] == "user":
            # Keep user message if it triggers a long assistant response
            next_asst = i + 1
            if next_asst in long_asst_indices:
                clean = _strip_noise(m["content"])
                if clean:
                    filtered.append({**m, "content": clean})
        # Short assistant messages: skip (filler)

    return filtered


# --- CLI for testing ---

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from extract import _parse_all_messages

    if len(sys.argv) < 2:
        print("Usage: python3 session_prefilter.py <session.jsonl> [--verbose]")
        sys.exit(1)

    session_path = sys.argv[1]
    verbose = "--verbose" in sys.argv

    # Classify
    stats = quick_classify(session_path)
    print(f"Session: {os.path.basename(session_path)}")
    print(f"  User chars:    {stats['user_chars']:,}")
    print(f"  Asst chars:    {stats['asst_chars']:,}")
    print(f"  Messages:      {stats['msg_count']}")
    print(f"  Is subagent:   {stats['is_subagent']}")
    print(f"  Has long asst: {stats['has_long_asst']}")
    print(f"  Skip facts:    {stats['skip_facts']}")
    print(f"  Skip artifacts:{stats['skip_artifacts']}")
    print()

    # Parse and filter
    messages = _parse_all_messages(session_path)
    total_chars = sum(len(m["content"]) for m in messages)

    fact_msgs = filter_for_facts(messages)
    fact_chars = sum(len(m["content"]) for m in fact_msgs)

    artifact_msgs = filter_for_artifacts(messages)
    artifact_chars = sum(len(m["content"]) for m in artifact_msgs)

    print(f"  Original:   {len(messages):3d} msgs, {total_chars:>8,} chars")
    print(f"  Facts:      {len(fact_msgs):3d} msgs, {fact_chars:>8,} chars ({fact_chars/max(total_chars,1)*100:.0f}%)")
    print(f"  Artifacts:  {len(artifact_msgs):3d} msgs, {artifact_chars:>8,} chars ({artifact_chars/max(total_chars,1)*100:.0f}%)")

    if verbose:
        print("\n--- Fact transcript ---")
        for m in fact_msgs[:5]:
            preview = m["content"][:100].replace("\n", " ")
            print(f"  [{m['role']}] {preview}...")
        print("\n--- Artifact transcript ---")
        for m in artifact_msgs[:5]:
            preview = m["content"][:100].replace("\n", " ")
            print(f"  [{m['role']}] {preview}...")
