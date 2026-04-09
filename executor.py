#!/usr/bin/env python3
"""
Reconciliation Pipeline — Stage 3: Executor

Permission-enforced action runner. Takes an action plan JSON (from Stage 2 reconciliation)
and applies it via notion-api.py calls, enforcing strict permission boundaries.

No LLM involved — pure deterministic Python.

Usage:
    # Execute action plan from file
    python3 executor.py --plan actions.json

    # Execute from stdin
    cat actions.json | python3 executor.py --stdin

    # Dry-run (log what would happen, don't execute)
    python3 executor.py --plan actions.json --dry-run

    # Generate review summary only (from existing audit log)
    python3 executor.py --review
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- Paths ---

from config import (get_kb_dir, get_audit_log, get_review_file, get_konban_script,
                    get_brain_script, get_skills_dir, get_skill_fixes_file,
                    get_proposals_file, get_linear_script)

KB_DIR = get_kb_dir()
AUDIT_LOG = get_audit_log()
REVIEW_FILE = get_review_file()

KONBAN_SCRIPT = get_konban_script()
BRAIN_SCRIPT = get_brain_script()
LINEAR_SCRIPT = get_linear_script()

# --- Permission Model ---

ALLOWED_ACTIONS = {
    "create_konban_task",   # Create new task (pending, tagged [daemon])
    "create_linear_issue",  # Create Linear issue for KH dev tasks (tagged [daemon])
    "log_konban_task",      # Append log entry to existing task
    "update_konban_task",   # Update task metadata (title, due date) — Tier 1
    "create_brain_doc",     # Create new Brain doc under a section
    "enrich_brain_doc",     # Append new section to existing Brain doc (additive only)
    "done_konban_task",     # Mark task done (high-confidence only, Tier 1)
    "done_linear_issue",    # Mark Linear issue done (high-confidence only, Tier 1)
    "fix_skill",            # Propose skill fix (write to review file, don't apply)
    "no_action",            # Explicit no-op (for audit trail)
}

DENIED_ACTIONS = {
    "delete",               # Never delete anything
    "update_active_context",# Never modify Active Context
    "send_email",           # Never send external comms
    "modify_claude_md",     # Never modify CLAUDE.md
    "modify_soul_md",       # Never modify SOUL.md
}

# Actions that require high confidence to auto-execute.
# Medium confidence → proposed at standup. Low confidence → skipped.
HIGH_CONFIDENCE_REQUIRED = {
    "done_konban_task",
    "done_linear_issue",
}


def log_audit(message: str):
    """Append to audit log with timestamp."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {message}\n"
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(line)


def run_command(args: list[str], stdin_text: str = None, timeout: int = 30) -> tuple[int, str, str]:
    """Run a subprocess and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.PIPE if stdin_text else None,
            input=stdin_text,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Script not found: {args[0]}"


def check_permission(action: dict) -> tuple[bool, str]:
    """Check if an action is allowed by the permission model.

    Returns (allowed, reason).
    """
    action_type = action.get("type", "")

    if action_type in DENIED_ACTIONS:
        return False, f"DENIED: {action_type} is explicitly forbidden"

    if action_type not in ALLOWED_ACTIONS:
        return False, f"DENIED: {action_type} is not in the allowed actions list"

    # Additional checks per action type
    if action_type == "create_konban_task":
        if not action.get("title"):
            return False, "DENIED: create_konban_task requires a title"

    if action_type == "create_linear_issue":
        if not action.get("title"):
            return False, "DENIED: create_linear_issue requires a title"

    if action_type == "log_konban_task":
        if not action.get("task_id") and not action.get("target"):
            return False, "DENIED: log_konban_task requires task_id or target"
        if not action.get("content"):
            return False, "DENIED: log_konban_task requires content"

    if action_type == "create_brain_doc":
        if not action.get("title") and not action.get("target"):
            return False, "DENIED: create_brain_doc requires a title"

    if action_type == "enrich_brain_doc":
        if not action.get("target"):
            return False, "DENIED: enrich_brain_doc requires a target (existing doc title)"
        if not action.get("content"):
            return False, "DENIED: enrich_brain_doc requires content"
        if not action.get("section_name"):
            return False, "DENIED: enrich_brain_doc requires section_name (heading for the new section)"

    if action_type == "update_konban_task":
        if not action.get("task_id") and not action.get("target"):
            return False, "DENIED: update_konban_task requires task_id or target"
        # Must have at least one field to update
        if not any(action.get(f) for f in ("new_name", "new_due", "new_priority", "new_timebox")):
            return False, "DENIED: update_konban_task requires at least one update field (new_name, new_due, new_priority, new_timebox)"

    if action_type == "done_konban_task":
        if not action.get("task_id") and not action.get("target"):
            return False, "DENIED: done_konban_task requires task_id or target"
        confidence = action.get("confidence", "low")
        if confidence != "high":
            return False, f"DEFERRED: done_konban_task requires high confidence (got {confidence})"

    if action_type == "done_linear_issue":
        if not action.get("identifier") and not action.get("target"):
            return False, "DENIED: done_linear_issue requires identifier (e.g., EARTH-379) or target"
        confidence = action.get("confidence", "low")
        if confidence != "high":
            return False, f"DEFERRED: done_linear_issue requires high confidence (got {confidence})"

    return True, "ALLOWED"


def _strip_daemon_decoration(title: str) -> str:
    """Strip [daemon] tag and category prefixes to get the core task description.

    Examples:
        "[daemon] [Business/KH/Product] Fix attribution: Populate person_id"
        → "Fix attribution: Populate person_id"

        "[daemon] Fix tracking: Populate person_id on variant-tagged events"
        → "Fix tracking: Populate person_id on variant-tagged events"
    """
    # Remove [daemon] tag
    title = title.replace("[daemon]", "").strip()
    # Remove category tags like [Business/KH/Product], [Personal/Health], etc.
    title = re.sub(r'\[[^\]]*\]\s*', '', title).strip()
    return title


def _normalize_for_dedup(title: str) -> str:
    """Normalize a task title for dedup comparison.

    Strips daemon decoration AND the verb/category prefix before the colon,
    so "Fix attribution: Populate person_id" and "Fix tracking: Populate person_id"
    both normalize to "populate person_id".
    """
    core = _strip_daemon_decoration(title).lower()
    # Strip the prefix before the first colon (e.g., "Fix attribution:", "Fix tracking:")
    if ":" in core:
        core = core.split(":", 1)[1].strip()
    return core


def _tokenize(text: str) -> set[str]:
    """Split text into a set of lowercase words, stripping punctuation."""
    return set(re.findall(r'[a-z0-9_-]+', text.lower()))


def _word_overlap_score(a: str, b: str) -> float:
    """Compute word-overlap similarity between two strings (Jaccard index)."""
    words_a = _tokenize(a)
    words_b = _tokenize(b)
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _containment_score(short: str, long: str) -> float:
    """What fraction of the shorter string's words appear in the longer string.

    Catches cases where a human-created task ("Fix gender matching bug") is
    a subset of a verbose daemon task ("Fix gender matching: Filter fix for null therapist gender").
    """
    words_short = _tokenize(short)
    words_long = _tokenize(long)
    if not words_short:
        return 0.0
    return len(words_short & words_long) / len(words_short)


# Cache for dedup: loaded once per executor run
_konban_all_cache: list[str] | None = None


def _load_all_konban_titles() -> list[str]:
    """Load ALL task/item titles for dedup — Konban (including Done/Grave) + Roadmap.

    Cross-references both databases so the daemon doesn't create Konban tasks
    for work already tracked on the Roadmap (e.g., "Care Pathway Funnel Experiment").
    """
    global _konban_all_cache
    if _konban_all_cache is not None:
        return _konban_all_cache

    titles = []

    # Load Konban tasks
    if KONBAN_SCRIPT and KONBAN_SCRIPT.exists():
        exit_code, stdout, stderr = run_command(
            ["python3", str(KONBAN_SCRIPT), "all"], timeout=60
        )
        if exit_code == 0 and stdout:
            for line in stdout.splitlines():
                # Format: "Status | Priority | Timebox | Due | Title [page_id]"
                parts = line.split(" | ")
                if len(parts) >= 5:
                    title_part = " | ".join(parts[4:])
                    title_clean = re.sub(r'\s*\[[0-9a-f-]+\]\s*$', '', title_part)
                    titles.append(title_clean)

    # Load Roadmap items (prevents daemon creating Konban tasks for roadmap work)
    roadmap_script = get_skills_dir() / "roadmap" / "roadmap-api.py"
    if roadmap_script.exists():
        exit_code, stdout, stderr = run_command(
            ["python3", str(roadmap_script), "all"], timeout=30
        )
        if exit_code == 0 and stdout:
            for line in stdout.splitlines():
                if line.startswith("Status") or line.startswith("---") or not line.strip():
                    continue
                # Strip trailing [page_id], then extract title after the last "- " separator
                clean = re.sub(r'\s*\[[0-9a-f-]+\]\s*$', '', line).strip()
                # Title follows the shipped column (always "-" or a date)
                # Split on 2+ spaces to find columns, title is the last segment
                segments = re.split(r'\s{2,}', clean)
                if len(segments) >= 3:
                    titles.append(segments[-1].strip())

    _konban_all_cache = titles
    return _konban_all_cache


def _find_duplicate_task(new_title: str) -> str | None:
    """Check if a similar task already exists in Konban (including Done/Grave).

    Returns the matching existing task title if a duplicate is found, None otherwise.

    Dedup strategy (layered, any match → duplicate):
    1. Normalize both titles (strip daemon tags, category prefixes, verb prefix before colon)
       → Exact match or high word-overlap (>= 0.7 Jaccard) → duplicate
    2. Strip decoration only (keep colon prefix) and check containment
       → If >= 75% of the shorter title's words appear in the longer → duplicate
       (catches human-created Done tasks like "Fix gender matching bug" matching
        daemon tasks like "[daemon] Fix gender matching: Filter fix for null therapist gender")
    """
    existing_titles = _load_all_konban_titles()
    new_normalized = _normalize_for_dedup(new_title)
    new_stripped = _strip_daemon_decoration(new_title).lower()

    if not new_normalized:
        return None

    for existing in existing_titles:
        existing_normalized = _normalize_for_dedup(existing)
        existing_stripped = _strip_daemon_decoration(existing).lower()
        if not existing_normalized:
            continue

        # Layer 1: Exact match on normalized core (colon prefix stripped)
        if new_normalized == existing_normalized:
            return existing

        # Layer 2: High word overlap on normalized core
        if _word_overlap_score(new_normalized, existing_normalized) >= 0.7:
            return existing

        # Layer 3: Containment check on stripped form (keeps colon prefix)
        # The shorter title's words should mostly appear in the longer title.
        # Catches already-shipped human tasks like "Fix gender matching bug"
        # matching daemon tasks like "Fix gender matching: Filter fix for null therapist gender"
        if existing_stripped and new_stripped:
            short, long = (existing_stripped, new_stripped) if len(existing_stripped) <= len(new_stripped) else (new_stripped, existing_stripped)
            short_words = _tokenize(short)
            long_words = _tokenize(long)
            # Require: short title has >= 3 words (avoid false positives on "Fix bug"),
            # at least 3 content words overlap, and >= 60% of short's words are contained
            overlap_count = len(short_words & long_words)
            if len(short_words) >= 3 and overlap_count >= 3 and _containment_score(short, long) >= 0.6:
                return existing

    return None


# Cache for Linear dedup: loaded once per executor run
_linear_all_cache: list[str] | None = None


def _load_all_linear_titles() -> list[str]:
    """Load ALL Linear issue titles (all statuses incl. Done/Canceled) for dedup."""
    global _linear_all_cache
    if _linear_all_cache is not None:
        return _linear_all_cache

    if not LINEAR_SCRIPT or not LINEAR_SCRIPT.exists():
        _linear_all_cache = []
        return _linear_all_cache

    exit_code, stdout, stderr = run_command(
        ["python3", str(LINEAR_SCRIPT), "board", "--all"], timeout=30
    )
    titles = []
    if exit_code == 0 and stdout:
        for line in stdout.splitlines():
            # Format: "EARTH-XXX  | Status | Priority | [Label] Title"
            parts = line.split(" | ")
            if len(parts) >= 4:
                # Last part is "[Label] Title" — strip the label tag
                title_part = " | ".join(parts[3:])
                title_clean = re.sub(r'^\[[^\]]*\]\s*', '', title_part).strip()
                titles.append(title_clean)
    _linear_all_cache = titles
    return _linear_all_cache


def _find_duplicate_linear_issue(new_title: str) -> str | None:
    """Check if a similar issue already exists in Linear.

    Uses the same 3-layer matching strategy as Konban dedup:
    1. Exact match on normalized core (colon prefix stripped)
    2. High word overlap (Jaccard >= 0.7)
    3. Containment (>= 60% of shorter title's words in longer)
    """
    existing_titles = _load_all_linear_titles()
    new_normalized = _normalize_for_dedup(new_title)
    new_stripped = _strip_daemon_decoration(new_title).lower()

    if not new_normalized:
        return None

    for existing in existing_titles:
        existing_normalized = _normalize_for_dedup(existing)
        existing_stripped = _strip_daemon_decoration(existing).lower()
        if not existing_normalized:
            continue

        # Layer 1: Exact match on normalized core
        if new_normalized == existing_normalized:
            return existing

        # Layer 2: High word overlap on normalized core
        if _word_overlap_score(new_normalized, existing_normalized) >= 0.7:
            return existing

        # Layer 3: Containment check
        if existing_stripped and new_stripped:
            short, long = (existing_stripped, new_stripped) if len(existing_stripped) <= len(new_stripped) else (new_stripped, existing_stripped)
            short_words = _tokenize(short)
            if len(short_words) >= 3 and len(short_words & _tokenize(long)) >= 3 and _containment_score(short, long) >= 0.6:
                return existing

    return None


def execute_create_konban_task(action: dict, dry_run: bool) -> dict:
    """Create a Konban task."""
    if not KONBAN_SCRIPT or not KONBAN_SCRIPT.exists():
        return {"status": "failed", "reason": "Konban script not available"}
    title = action.get("title") or action.get("target", "Untitled")
    # Tag all daemon-created tasks with category for disambiguation
    category = action.get("category", "")
    sub_cat = action.get("sub_category", "")
    cat_tag = f"[{category}/{sub_cat}] " if sub_cat else (f"[{category}] " if category else "")
    if "[daemon]" not in title:
        title = f"[daemon] {cat_tag}{title}"

    # Domain guard: skip implementation-level dev tasks (belong in Linear, not Konban)
    domain = action.get("domain", "")
    dev_keywords = {"component", "endpoint", "api", "schema", "refactor", "dialog",
                    "form", "view", "tab", "button", "test", "migration", "hook"}
    title_words = set(title.lower().split())
    if domain == "KH" and title_words & dev_keywords:
        return {"status": "skipped", "reason": f"Dev task routed away from Konban (domain={domain})",
                "title": title}

    # Dedup check: compare core title against ALL existing tasks (including Done/Grave)
    # Strips daemon tags, category prefixes, and verb prefixes before colon for comparison
    duplicate = _find_duplicate_task(title)
    if duplicate:
        log_audit(f"  DEDUP: Skipped '{title}' — similar task exists: '{duplicate}'")
        return {"status": "skipped",
                "reason": f"Duplicate detected: '{duplicate}'",
                "title": title}

    priority = action.get("priority", "Medium")
    timebox = action.get("timebox")

    cmd = ["python3", str(KONBAN_SCRIPT), "create", title, "--priority", priority]
    if timebox:
        cmd.extend(["--timebox", timebox])

    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd)}

    exit_code, stdout, stderr = run_command(cmd)

    result = {"status": "success" if exit_code == 0 else "failed", "exit_code": exit_code}
    if exit_code == 0 and _konban_all_cache is not None:
        # Add to cache so subsequent creates in the same run also dedup against it
        _konban_all_cache.append(title)
    if stdout:
        result["output"] = stdout
        # Extract page ID from "Created: <id>" output
        if stdout.startswith("Created:"):
            page_id = stdout.split("Created:")[1].strip()
            result["page_id"] = page_id

            # If there's content to log on the new task, do it
            content = action.get("content")
            if content and page_id:
                log_cmd = ["python3", str(KONBAN_SCRIPT), "log", page_id,
                           f"[daemon] {content}\nSource: {action.get('source_artifact', 'unknown')}"]
                run_command(log_cmd)

            # Cross-reference: if this task was decomposed from a Brain doc, log the pointer
            brain_doc = action.get("brain_doc")
            if brain_doc and page_id:
                xref_msg = f"[daemon] 📋 Brain doc: '{brain_doc}' — full analysis and rationale"
                run_command(["python3", str(KONBAN_SCRIPT), "log", page_id, xref_msg])
    if stderr:
        result["error"] = stderr

    return result


def execute_create_linear_issue(action: dict, dry_run: bool) -> dict:
    """Create a Linear issue for KH dev tasks."""
    linear_script = get_skills_dir() / "linear" / "linear-api.py"
    if not linear_script.exists():
        return {"status": "failed", "reason": f"Linear script not available at {linear_script}"}

    title = action.get("title") or action.get("target", "Untitled")
    # Tag all daemon-created issues
    if "[daemon]" not in title:
        title = f"[daemon] {title}"

    # Dedup check: compare against existing Linear issues (all statuses)
    duplicate = _find_duplicate_linear_issue(title)
    if duplicate:
        log_audit(f"  DEDUP: Skipped Linear issue '{title}' — similar issue exists: '{duplicate}'")
        return {"status": "skipped",
                "reason": f"Duplicate of existing Linear issue: {duplicate}",
                "title": title}

    # Also check Konban (cross-system dedup: catches tasks that were done in Konban but not Linear)
    konban_dup = _find_duplicate_task(title)
    if konban_dup:
        log_audit(f"  DEDUP: Skipped Linear issue '{title}' — similar Konban task exists: '{konban_dup}'")
        return {"status": "skipped",
                "reason": f"Duplicate of existing Konban task: {konban_dup}",
                "title": title}

    # Map priority: string → int (Linear uses 0-4)
    priority_raw = action.get("priority", "3")  # Default Medium
    priority_map = {"urgent": "1", "high": "2", "medium": "3", "low": "4", "none": "0"}
    if isinstance(priority_raw, str) and priority_raw.lower() in priority_map:
        priority = priority_map[priority_raw.lower()]
    else:
        priority = str(priority_raw) if str(priority_raw) in ("0", "1", "2", "3", "4") else "3"

    status = action.get("status", "backlog")
    label = action.get("label", "Feature")
    description = action.get("content") or action.get("description", "")
    source = action.get("source_artifact", "unknown")

    # Append source info to description
    if description:
        description = f"{description}\n\n[daemon] Source: {source}"
    else:
        description = f"[daemon] Source: {source}"

    cmd = ["python3", str(linear_script), "create", title,
           "--priority", priority, "--status", status, "--label", label,
           "--description", description]

    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd)}

    exit_code, stdout, stderr = run_command(cmd, timeout=30)
    result = {"status": "success" if exit_code == 0 else "failed",
              "exit_code": exit_code, "title": title}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


def execute_log_konban_task(action: dict, dry_run: bool) -> dict:
    """Log a message on an existing Konban task."""
    if not KONBAN_SCRIPT or not KONBAN_SCRIPT.exists():
        return {"status": "failed", "reason": "Konban script not available"}

    # Domain guard: only Konban-appropriate domains
    domain = action.get("domain", "")
    if domain and domain not in ("Personal", "KH", "Consulting", "MBA", ""):
        return {"status": "skipped",
                "reason": f"log_konban_task skipped — domain '{domain}' is not a Konban domain"}

    task_id = action.get("task_id", "")
    target = action.get("target", "")
    content = action.get("content", "")
    source = action.get("source_artifact", "unknown")

    message = f"[daemon] {content}\nSource: {source}"

    # If we have a task_id, use it directly
    if task_id:
        cmd = ["python3", str(KONBAN_SCRIPT), "log", task_id, message]
    elif target:
        # Search for the task first
        if dry_run:
            return {"status": "dry_run", "note": f"Would search for '{target}' then log"}

        search_code, search_out, _ = run_command(
            ["python3", str(KONBAN_SCRIPT), "search", target]
        )
        if search_code != 0 or "No active tasks" in search_out:
            return {"status": "skipped", "reason": f"Task not found: {target}"}

        # Parse first result ID from search output
        # Format: "Status | Priority | Timebox | Due | Title [page-id]"
        # The page ID is in brackets at the end of each line
        import re
        id_matches = re.findall(r'\[([0-9a-f-]{36})\]', search_out)
        if not id_matches:
            return {"status": "skipped", "reason": f"No matching task for: {target}"}

        task_id = id_matches[0]  # First match

        cmd = ["python3", str(KONBAN_SCRIPT), "log", task_id, message]
    else:
        return {"status": "failed", "reason": "No task_id or target provided"}

    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd)}

    exit_code, stdout, stderr = run_command(cmd)
    result = {"status": "success" if exit_code == 0 else "failed", "exit_code": exit_code}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


def execute_done_konban_task(action: dict, dry_run: bool) -> dict:
    """Mark a Konban task as done. Only executes with high confidence."""
    if not KONBAN_SCRIPT or not KONBAN_SCRIPT.exists():
        return {"status": "failed", "reason": "Konban script not available"}
    task_id = action.get("task_id", "")
    target = action.get("target", "")
    evidence = action.get("content", "")
    source = action.get("source_artifact", "unknown")

    # Resolve task_id from target name if needed
    if not task_id and target:
        # Try to extract page ID directly from target string (e.g., "Task name [uuid]")
        id_in_target = re.findall(r'\[([0-9a-f-]{36})\]', target)
        if id_in_target:
            task_id = id_in_target[0]
        else:
            if dry_run:
                return {"status": "dry_run", "note": f"Would search for '{target}' then mark done"}

            search_code, search_out, _ = run_command(
                ["python3", str(KONBAN_SCRIPT), "search", target]
            )
            if search_code != 0 or "No active tasks" in search_out:
                return {"status": "skipped", "reason": f"Task not found: {target}"}

            id_matches = re.findall(r'\[([0-9a-f-]{36})\]', search_out)
            if not id_matches:
                return {"status": "skipped", "reason": f"No matching task for: {target}"}
            task_id = id_matches[0]

    if not task_id:
        return {"status": "failed", "reason": "No task_id or target provided"}

    # Log the evidence before marking done
    log_msg = f"[daemon] Marked done — {evidence}\nSource: {source}"
    if not dry_run:
        run_command(["python3", str(KONBAN_SCRIPT), "log", task_id, log_msg])

    cmd = ["python3", str(KONBAN_SCRIPT), "done", task_id]
    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd)}

    exit_code, stdout, stderr = run_command(cmd)
    result = {"status": "success" if exit_code == 0 else "failed", "exit_code": exit_code}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


def execute_done_linear_issue(action: dict, dry_run: bool) -> dict:
    """Mark a Linear issue as done. Only executes with high confidence."""
    linear_script = get_skills_dir() / "linear" / "linear-api.py"
    if not linear_script.exists():
        return {"status": "failed", "reason": f"Linear script not available at {linear_script}"}

    identifier = action.get("identifier", "")
    target = action.get("target", "")
    evidence = action.get("content", "")
    source = action.get("source_artifact", "unknown")

    # Extract EARTH-XXX identifier from target if not provided directly
    if not identifier and target:
        m = re.search(r'(EARTH-\d+)', target)
        if m:
            identifier = m.group(1)

    if not identifier:
        return {"status": "failed", "reason": "No Linear identifier found in action"}

    # Add comment with evidence before closing
    comment = f"[daemon] Marked done — {evidence}\nSource: {source}"
    if not dry_run:
        run_command(["python3", str(linear_script), "comment", identifier, comment])

    cmd = ["python3", str(linear_script), "update", identifier, "--status", "done"]
    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd)}

    exit_code, stdout, stderr = run_command(cmd)
    result = {"status": "success" if exit_code == 0 else "failed", "exit_code": exit_code,
              "identifier": identifier}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


def execute_update_konban_task(action: dict, dry_run: bool) -> dict:
    """Update a Konban task's metadata (title, due date, priority, timebox)."""
    if not KONBAN_SCRIPT or not KONBAN_SCRIPT.exists():
        return {"status": "failed", "reason": "Konban script not available"}

    # Domain guard: only Konban-appropriate domains
    domain = action.get("domain", "")
    if domain and domain not in ("Personal", "KH", "Consulting", "MBA", ""):
        return {"status": "skipped",
                "reason": f"update_konban_task skipped — domain '{domain}' is not a Konban domain"}

    task_id = action.get("task_id", "")
    target = action.get("target", "")
    source = action.get("source_artifact", "unknown")

    # Resolve task_id from target name if needed
    if not task_id and target:
        if dry_run:
            return {"status": "dry_run", "note": f"Would search for '{target}' then update"}

        import re
        search_code, search_out, _ = run_command(
            ["python3", str(KONBAN_SCRIPT), "search", target]
        )
        if search_code != 0 or "No active tasks" in search_out:
            return {"status": "skipped", "reason": f"Task not found: {target}"}

        id_matches = re.findall(r'\[([0-9a-f-]{36})\]', search_out)
        if not id_matches:
            return {"status": "skipped", "reason": f"No matching task for: {target}"}
        task_id = id_matches[0]

    if not task_id:
        return {"status": "failed", "reason": "No task_id or target provided"}

    # Build update command
    cmd = ["python3", str(KONBAN_SCRIPT), "update", task_id]
    updates = []
    if action.get("new_name"):
        cmd.extend(["--name", action["new_name"]])
        updates.append(f"name → {action['new_name']}")
    if action.get("new_due"):
        cmd.extend(["--due", action["new_due"]])
        updates.append(f"due → {action['new_due']}")
    if action.get("new_priority"):
        cmd.extend(["--priority", action["new_priority"]])
        updates.append(f"priority → {action['new_priority']}")
    if action.get("new_timebox"):
        cmd.extend(["--timebox", action["new_timebox"]])
        updates.append(f"timebox → {action['new_timebox']}")

    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd), "updates": updates}

    # Log the update reason before applying
    log_msg = f"[daemon] Updated: {', '.join(updates)}\nSource: {source}"
    run_command(["python3", str(KONBAN_SCRIPT), "log", task_id, log_msg])

    exit_code, stdout, stderr = run_command(cmd)
    result = {"status": "success" if exit_code == 0 else "failed",
              "exit_code": exit_code, "updates": updates}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


def execute_create_brain_doc(action: dict, dry_run: bool) -> dict:
    """Create a Brain doc under the appropriate section."""
    if not BRAIN_SCRIPT or not BRAIN_SCRIPT.exists():
        return {"status": "failed", "reason": "Brain script not available"}
    title = action.get("title") or action.get("target", "Untitled")
    content = action.get("content", "")
    source = action.get("source_artifact", "unknown")
    section = action.get("section", "Research")  # Default to Research for daemon-created docs
    domain = action.get("domain")

    # Domain guard: Brain is KH-only. Other domains have their own docs.
    if domain and domain not in ("KH", None):
        return {"status": "skipped",
                "reason": f"Brain doc skipped — domain '{domain}' is not KH",
                "title": title}

    # Implementation artifact guard: block docs that are clearly code-level debugging,
    # not strategic knowledge. Conservative — only blocks unambiguous patterns.
    # The real fix is better reconciliation prompts + the cleanup plan, not keyword whack-a-mole.
    _IMPL_PATTERNS = {
        "e2e test",          # test analyses belong in Linear/git
        "test failure",      # debugging artifacts
        "test gap",
        "root cause analysis",
        "post-mortem",       # incident response → git/Linear
        "overflow analysis", # CSS debugging
        "race condition",    # code-level
        "desync",
        "bug analysis",
        "serialization",
    }
    title_lower = title.lower()
    for kw in _IMPL_PATTERNS:
        if kw in title_lower:
            return {"status": "skipped",
                    "reason": f"Brain doc blocked — implementation artifact (matched '{kw}')",
                    "title": title}

    # Add daemon header
    full_content = f"*Created by reconciliation daemon — source: {source}*\n\n{content}"

    if dry_run:
        return {"status": "dry_run", "note": f"Would create Brain doc: {title} (under {section})"}

    # Write content to temp file
    tmp_file = Path("/tmp") / f"daemon-brain-{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
    tmp_file.write_text(full_content)

    cmd = ["python3", str(BRAIN_SCRIPT), "create", title, "--file", str(tmp_file),
           "--parent", section]
    if domain:
        cmd.extend(["--domain", domain])
    exit_code, stdout, stderr = run_command(cmd, timeout=60)

    # Clean up
    tmp_file.unlink(missing_ok=True)

    result = {"status": "success" if exit_code == 0 else "failed", "exit_code": exit_code}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


def execute_enrich_brain_doc(action: dict, dry_run: bool) -> dict:
    """Append a new section to an existing Brain doc. Additive only — never modifies existing content.

    Strategy:
    - If the section already exists (re-enrichment): use `patch` to replace just that section.
    - If the section is new: use `append` to add blocks at the end. No deletions, no risk of data loss.
    Never uses `update` — that does delete-all-then-rewrite and can nuke large pages on timeout.
    """
    if not BRAIN_SCRIPT or not BRAIN_SCRIPT.exists():
        return {"status": "failed", "reason": "Brain script not available"}
    target = action.get("target", "")
    section_name = action.get("section_name", "Daemon Enrichment")
    content = action.get("content", "")
    source = action.get("source_artifact", "unknown")

    date_str = datetime.now().strftime("%b %d, %Y")
    new_section = (
        f"\n\n---\n\n"
        f"## {section_name}\n\n"
        f"*Added by reconciliation daemon on {date_str} — source: {source}*\n\n"
        f"{content}"
    )

    if dry_run:
        return {"status": "dry_run", "note": f"Would append section '{section_name}' to '{target}'"}

    # Step 1: Read existing doc content
    read_code, existing_content, read_err = run_command(
        ["python3", str(BRAIN_SCRIPT), "read", target, "--raw"], timeout=30
    )
    if read_code != 0:
        return {"status": "failed", "reason": f"Could not read '{target}': {read_err}"}

    # Step 2: Check if this section already exists (re-enrichment case)
    if f"## {section_name}" in existing_content:
        # Use patch to replace the existing daemon section
        tmp_file = Path("/tmp") / f"daemon-enrich-{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
        section_content = (
            f"## {section_name}\n\n"
            f"*Updated by reconciliation daemon on {date_str} — source: {source}*\n\n"
            f"{content}"
        )
        tmp_file.write_text(section_content)
        cmd = ["python3", str(BRAIN_SCRIPT), "patch", target,
               "--section", section_name, "--file", str(tmp_file)]
        exit_code, stdout, stderr = run_command(cmd, timeout=60)
        tmp_file.unlink(missing_ok=True)
        result = {"status": "success" if exit_code == 0 else "failed",
                  "exit_code": exit_code, "mode": "patch_existing"}
        if stdout:
            result["output"] = stdout
        if stderr:
            result["error"] = stderr
        return result

    # Step 3: Append new section using additive-only `append` command.
    # NEVER use `update --force` here — update does delete-all-then-rewrite.
    # On large pages (214+ blocks), a 90s timeout can kill the process after
    # deletions commit but before writes complete, nuking the entire page.
    tmp_file = Path("/tmp") / f"daemon-enrich-{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
    tmp_file.write_text(new_section)
    cmd = ["python3", str(BRAIN_SCRIPT), "append", target,
           "--file", str(tmp_file)]
    exit_code, stdout, stderr = run_command(cmd, timeout=60)
    tmp_file.unlink(missing_ok=True)

    result = {"status": "success" if exit_code == 0 else "failed",
              "exit_code": exit_code, "mode": "append_new"}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


SKILLS_DIR = get_skills_dir()


def _apply_skill_patch(skill: str, patch_type: str, section_heading: str = None,
                       anchor_text: str = None, new_content: str = None) -> tuple[bool, str]:
    """Apply a structured patch to a SKILL.md file.

    Returns (success, message).
    """
    skill_path = SKILLS_DIR / skill / "SKILL.md"
    if not skill_path.exists():
        return False, f"SKILL.md not found: {skill_path}"

    content = skill_path.read_text()
    if not new_content:
        return False, "No new_content provided"

    # Dedup: check if the new content is already present
    if new_content.strip() in content:
        return False, f"Content already present in SKILL.md for {skill}"

    if patch_type == "append_to_section":
        if not section_heading:
            return False, "append_to_section requires section_heading"

        # Find the section heading (## or ###)
        lines = content.split("\n")
        section_start = -1
        section_end = len(lines)

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") and section_heading.lower() in stripped.lower():
                section_start = i
                # Find the end of this section (next heading of same or higher level)
                heading_level = len(stripped) - len(stripped.lstrip("#"))
                for j in range(i + 1, len(lines)):
                    next_stripped = lines[j].strip()
                    if next_stripped.startswith("#"):
                        next_level = len(next_stripped) - len(next_stripped.lstrip("#"))
                        if next_level <= heading_level:
                            section_end = j
                            break
                break

        if section_start < 0:
            return False, f"Section '{section_heading}' not found in SKILL.md for {skill}"

        # Insert new content at end of section (before next section)
        insert_line = section_end
        # Add a blank line before if needed
        if insert_line > 0 and lines[insert_line - 1].strip():
            new_lines = lines[:insert_line] + ["", new_content, ""] + lines[insert_line:]
        else:
            new_lines = lines[:insert_line] + [new_content, ""] + lines[insert_line:]

        skill_path.write_text("\n".join(new_lines))
        return True, f"Appended to section '{section_heading}'"

    elif patch_type == "add_note_after":
        if not anchor_text:
            return False, "add_note_after requires anchor_text"

        lines = content.split("\n")
        for i, line in enumerate(lines):
            if anchor_text in line:
                # Insert after this line
                new_lines = lines[:i + 1] + [new_content] + lines[i + 1:]
                skill_path.write_text("\n".join(new_lines))
                return True, f"Added note after '{anchor_text[:40]}...'"

        return False, f"Anchor text '{anchor_text[:40]}...' not found in SKILL.md for {skill}"

    elif patch_type == "add_new_section":
        # Append a new ## section at the end of the file
        if not content.endswith("\n"):
            content += "\n"
        heading = section_heading or "Additional Notes"
        content += f"\n## {heading}\n\n{new_content}\n"
        skill_path.write_text(content)
        return True, f"Added new section '## {heading}'"

    elif patch_type == "report_bug":
        # Bug reports are always proposals, never auto-applied
        return False, "report_bug patches are always saved as proposals"

    else:
        return False, f"Unknown patch_type: {patch_type}"


def _save_skill_proposal(proposal: dict) -> bool:
    """Save a skill fix proposal with dedup check and TTL cleanup.

    Returns True if saved, False if duplicate.
    Drops proposals older than 14 days on each save.
    """
    review_path = get_skill_fixes_file()
    existing = []
    if review_path.exists():
        try:
            existing = json.loads(review_path.read_text())
        except json.JSONDecodeError:
            existing = []

    # TTL cleanup: drop proposals older than 14 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    pre_ttl = len(existing)
    existing = [p for p in existing if p.get("timestamp", "") >= cutoff]
    if len(existing) < pre_ttl:
        print(f"  TTL cleanup: dropped {pre_ttl - len(existing)} stale proposal(s)")

    # Dedup: same skill + same new_content = skip
    for ex in existing:
        if (ex.get("skill") == proposal.get("skill") and
                ex.get("new_content") == proposal.get("new_content")):
            return False

    existing.append(proposal)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(existing, indent=2))
    return True


def execute_fix_skill(action: dict, dry_run: bool) -> dict:
    """Apply or propose a SKILL.md fix based on structured patch instructions.

    High-confidence additive patches (append_to_section, add_note_after) auto-apply.
    Medium-confidence, non-additive, or report_bug patches are saved as proposals.
    """
    skill = action.get("skill") or action.get("target", "unknown")

    # Guard: skip proposals for nonexistent skills
    skills_dir = get_skills_dir()
    if skills_dir:
        skill_path_check = Path(skills_dir) / skill / "SKILL.md"
        if not skill_path_check.exists():
            return {"status": "skipped", "reason": f"Skill '{skill}' does not exist (no SKILL.md)"}
    patch_type = action.get("patch_type", "")
    section_heading = action.get("section_heading")
    anchor_text = action.get("anchor_text")
    new_content = action.get("new_content") or action.get("content", "")
    confidence = action.get("confidence", "low")
    rationale = action.get("rationale", "")

    proposal = {
        "skill": skill,
        "patch_type": patch_type,
        "section_heading": section_heading,
        "anchor_text": anchor_text,
        "new_content": new_content,
        "rationale": rationale,
        "confidence": confidence,
        "source": action.get("source_artifact", "unknown"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Determine whether to auto-apply or save as proposal
    auto_apply = (
        confidence == "high"
        and patch_type in ("append_to_section", "add_note_after", "add_new_section")
        and new_content
    )

    if dry_run:
        proposal["would_auto_apply"] = auto_apply
        return {"status": "dry_run", "proposal": proposal}

    if auto_apply:
        success, message = _apply_skill_patch(
            skill, patch_type, section_heading, anchor_text, new_content
        )
        if success:
            return {"status": "success", "message": message, "auto_applied": True}
        else:
            # Auto-apply failed — fall through to save as proposal
            proposal["auto_apply_failed"] = message

    # Save as proposal for standup review
    saved = _save_skill_proposal(proposal)
    if saved:
        return {"status": "proposed", "proposal": proposal}
    else:
        return {"status": "skipped", "reason": "Duplicate proposal"}


# Action type → executor function
EXECUTORS = {
    "create_konban_task": execute_create_konban_task,
    "create_linear_issue": execute_create_linear_issue,
    "log_konban_task": execute_log_konban_task,
    "update_konban_task": execute_update_konban_task,
    "done_konban_task": execute_done_konban_task,
    "done_linear_issue": execute_done_linear_issue,
    "create_brain_doc": execute_create_brain_doc,
    "enrich_brain_doc": execute_enrich_brain_doc,
    "fix_skill": execute_fix_skill,
    "no_action": lambda action, dry_run: {"status": "no_action", "rationale": action.get("rationale", "")},
}


def _cross_reference_artifact_groups(actions: list, results: list):
    """After execution, cross-reference Brain docs and Konban tasks from the same artifact group.

    When a Brain doc and Konban tasks are created from the same artifact (decomposition),
    enrich the Brain doc with a note about which tasks were created.
    """
    # Build map: artifact_group → {brain_doc_title, konban_tasks: [{title, page_id}]}
    groups: dict[str, dict] = {}
    for action, result in zip(actions, results):
        group = action.get("artifact_group")
        if not group or result.get("status") != "success":
            continue

        if group not in groups:
            groups[group] = {"brain_doc": None, "konban_tasks": []}

        if action.get("type") == "create_brain_doc":
            groups[group]["brain_doc"] = action.get("title") or action.get("target")
        elif action.get("type") == "create_konban_task":
            task_title = action.get("title") or action.get("target", "?")
            groups[group]["konban_tasks"].append(task_title)

    # For each group with both a Brain doc and tasks, enrich the Brain doc
    for group_id, group in groups.items():
        if not group["brain_doc"] or not group["konban_tasks"]:
            continue

        task_list = "\n".join(f"- {t}" for t in group["konban_tasks"])
        xref_section = (
            f"\n\n---\n\n"
            f"## Konban Tasks Created\n\n"
            f"*The following tasks were created from this analysis by the reconciliation daemon:*\n\n"
            f"{task_list}\n"
        )

        # Append cross-reference section using additive-only `append` command.
        # NEVER use `update --force` here — it does delete-all-then-rewrite and
        # can nuke large pages on timeout (deletions commit, writes don't).
        import tempfile
        tmp = Path(tempfile.mktemp(suffix=".md", prefix="daemon-xref-"))
        tmp.write_text(xref_section)
        run_command(
            ["python3", str(BRAIN_SCRIPT), "append", group["brain_doc"],
             "--file", str(tmp)],
            timeout=60,
        )
        tmp.unlink(missing_ok=True)
        log_audit(f"  XREF: Cross-referenced '{group['brain_doc']}' ↔ {len(group['konban_tasks'])} Konban task(s)")


def execute_plan(plan: dict, dry_run: bool = False) -> dict:
    """Execute an action plan, enforcing permissions.

    Returns execution report.
    """
    actions = plan.get("proposed_actions", [])
    conflicts = plan.get("conflicts_flagged", [])
    summary = plan.get("summary", "")

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "actions_total": len(actions),
        "actions_executed": 0,
        "actions_denied": 0,
        "actions_deferred": 0,
        "actions_failed": 0,
        "actions_skipped": 0,
        "conflicts": len(conflicts),
        "results": [],
        "proposals": [],  # Medium-confidence actions deferred to standup
    }

    log_audit(f"{'DRY RUN — ' if dry_run else ''}Executing plan: {len(actions)} actions, {len(conflicts)} conflicts")
    if summary:
        log_audit(f"  Plan summary: {summary}")

    for i, action in enumerate(actions):
        action_type = action.get("type", "unknown")
        target = action.get("target") or action.get("title", "?")

        # Check permission
        allowed, reason = check_permission(action)
        if not allowed:
            # Distinguish DEFERRED (medium confidence → standup) from DENIED (forbidden)
            if reason.startswith("DEFERRED:"):
                log_audit(f"  [{i+1}/{len(actions)}] {reason}: {action_type} → {target}")
                report["actions_deferred"] += 1
                proposal = {
                    "action": action_type,
                    "target": target,
                    "reason": reason,
                    "confidence": action.get("confidence", "unknown"),
                    "content": action.get("content", ""),
                    "rationale": action.get("rationale", ""),
                    "source_artifact": action.get("source_artifact", ""),
                }
                report["proposals"].append(proposal)
                report["results"].append({**proposal, "status": "deferred"})
                continue

            log_audit(f"  [{i+1}/{len(actions)}] {reason}: {action_type} → {target}")
            report["actions_denied"] += 1
            report["results"].append({
                "action": action_type,
                "target": target,
                "status": "denied",
                "reason": reason,
            })
            continue

        # Execute
        executor = EXECUTORS.get(action_type)
        if not executor:
            log_audit(f"  [{i+1}/{len(actions)}] SKIPPED: no executor for {action_type}")
            report["actions_skipped"] += 1
            continue

        log_audit(f"  [{i+1}/{len(actions)}] EXECUTING: {action_type} → {target}")
        result = executor(action, dry_run)

        status = result.get("status", "unknown")
        log_audit(f"    Result: {status}")
        if result.get("page_id"):
            log_audit(f"    Page ID: {result['page_id']}")
        if result.get("error"):
            log_audit(f"    Error: {result['error']}")

        report["results"].append({
            "action": action_type,
            "target": target,
            **result,
        })

        if status in ("success", "dry_run", "no_action", "proposed"):
            report["actions_executed"] += 1
        elif status == "skipped":
            report["actions_skipped"] += 1
        else:
            report["actions_failed"] += 1

    # Log conflicts
    if conflicts:
        log_audit(f"  CONFLICTS FLAGGED ({len(conflicts)}):")
        for c in conflicts:
            log_audit(f"    - {c.get('artifact', '?')} conflicts with {c.get('conflicts_with', '?')}")
            log_audit(f"      Recommendation: {c.get('recommendation', 'review needed')}")

    report["conflicts_detail"] = conflicts

    # Post-execution: cross-reference Brain docs ↔ Konban tasks within artifact groups
    if not dry_run:
        _cross_reference_artifact_groups(actions, report["results"])

    # Save deferred proposals for standup
    if report["proposals"] and not dry_run:
        proposals_file = get_proposals_file()
        existing_proposals = []
        if proposals_file.exists():
            try:
                existing_proposals = json.loads(proposals_file.read_text())
            except json.JSONDecodeError:
                existing_proposals = []
        existing_proposals.extend(report["proposals"])
        proposals_file.write_text(json.dumps(existing_proposals, indent=2))
        log_audit(f"  Saved {len(report['proposals'])} proposal(s) to {proposals_file}")

    log_audit(f"Done: {report['actions_executed']} executed, {report['actions_deferred']} deferred, "
              f"{report['actions_denied']} denied, {report['actions_failed']} failed, "
              f"{report['actions_skipped']} skipped")

    return report


def _format_action_label(r: dict) -> str:
    """Build a readable label for a reconciliation action result."""
    action = r.get("action", "?")
    target = r.get("target", "")
    title = r.get("title", "")
    rationale = r.get("rationale", "")

    # Use best available description
    label = target or title or rationale[:80] or "?"
    if label == "?" and r.get("output"):
        label = r["output"][:80]

    return f"{action}: {label}"


def _load_kb_extraction_stats() -> list:
    """Load recent KB extraction stats from daemon log for standup context."""
    daemon_log = KB_DIR / "extract.log"
    if not daemon_log.exists():
        return []

    lines = []
    try:
        log_lines = daemon_log.read_text().strip().splitlines()
        # Find the most recent "Done:" summary line
        for line in reversed(log_lines):
            if "Done:" in line and "extracted" in line:
                lines.append(f"  Last daemon run: {line.strip()}")
                break
    except OSError:
        pass

    # Check KB status for entity/fact counts
    try:
        import subprocess as _sp
        result = _sp.run(
            ["python3", str(KB_DIR / "kb.py"), "status"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if "DB:" in line:
                lines.append(f"  {line.strip()}")
            elif "superseded" in line.lower():
                lines.append(f"  {line.strip()}")
    except Exception:
        pass

    return lines


def generate_review(report: dict = None) -> str:
    """Generate a review summary for morning standup.

    If report is None, reads from the most recent audit log entries.
    """
    if not report:
        return "No report available. Run the pipeline first."

    date = datetime.now().strftime("%b %d")
    dry_label = " (DRY RUN)" if report.get("dry_run") else ""

    lines = [
        f"## Overnight Reconciliation — {date}{dry_label}",
        "",
    ]

    # Summary counts
    executed = report.get("actions_executed", 0)
    deferred = report.get("actions_deferred", 0)
    denied = report.get("actions_denied", 0)
    failed = report.get("actions_failed", 0)

    # Categorize results for readable summary
    results = report.get("results", [])
    created_docs = [r for r in results if r.get("action") == "create_brain_doc" and r.get("status") == "success"]
    created_tasks = [r for r in results if r.get("action") == "create_konban_task" and r.get("status") == "success"]
    created_issues = [r for r in results if r.get("action") == "create_linear_issue" and r.get("status") == "success"]
    enriched = [r for r in results if r.get("action") == "enrich_brain_doc" and r.get("status") == "success"]
    logged = [r for r in results if r.get("action") == "log_konban_task" and r.get("status") == "success"]
    done_tasks = [r for r in results if r.get("action") == "done_konban_task" and r.get("status") == "success"]
    done_issues = [r for r in results if r.get("action") == "done_linear_issue" and r.get("status") == "success"]
    updated_tasks = [r for r in results if r.get("action") == "update_konban_task" and r.get("status") == "success"]
    skill_fixes = [r for r in results if r.get("action") == "fix_skill" and r.get("status") == "success"]
    no_actions = [r for r in results if r.get("status") == "no_action"]

    # One-line summary
    parts = []
    if created_docs:
        parts.append(f"{len(created_docs)} Brain docs created")
    if created_tasks:
        parts.append(f"{len(created_tasks)} Konban tasks created")
    if created_issues:
        parts.append(f"{len(created_issues)} Linear issues created")
    if enriched:
        parts.append(f"{len(enriched)} Brain docs enriched")
    if logged:
        parts.append(f"{len(logged)} task logs added")
    if done_tasks:
        parts.append(f"{len(done_tasks)} tasks marked done")
    if done_issues:
        parts.append(f"{len(done_issues)} Linear issues closed")
    if updated_tasks:
        parts.append(f"{len(updated_tasks)} tasks updated")
    if skill_fixes:
        parts.append(f"{len(skill_fixes)} skill docs fixed")
    if no_actions:
        parts.append(f"{len(no_actions)} already current")

    if parts:
        lines.append(f"**Summary**: {', '.join(parts)}")
    else:
        lines.append(f"**{executed} auto-executed, {deferred} for your review**")
    if failed:
        lines.append(f"  ({failed} failed — see below)")
    lines.append("")

    # DONE: Brain docs created (most valuable to know about)
    if created_docs:
        lines.append("**Brain docs created** (spot-check):")
        for r in created_docs:
            label = r.get("target") or r.get("title") or "?"
            lines.append(f"  [+] {label}")
            if r.get("page_id"):
                lines.append(f"      → {r['page_id'][:40]}")
        lines.append("")

    # DONE: Konban tasks created
    if created_tasks:
        lines.append("**Konban tasks created** (spot-check):")
        for r in created_tasks:
            label = r.get("target") or r.get("title") or "?"
            lines.append(f"  [+] {label}")
        lines.append("")

    # DONE: Linear issues created
    if created_issues:
        lines.append("**Linear issues created** (spot-check):")
        for r in created_issues:
            label = r.get("target") or r.get("title") or "?"
            lines.append(f"  [+] {label}")
            if r.get("output"):
                lines.append(f"      → {r['output'][:80]}")
        lines.append("")

    # DONE: Tasks marked done
    if done_tasks:
        lines.append("**Tasks marked done**:")
        for r in done_tasks:
            lines.append(f"  [✓] {r.get('target', '?')}")
        lines.append("")

    # DONE: Linear issues closed
    if done_issues:
        lines.append("**Linear issues closed**:")
        for r in done_issues:
            identifier = r.get("identifier", "")
            target = r.get("target", "?")
            lines.append(f"  [✓] {identifier} {target}")
        lines.append("")

    # DONE: Tasks updated
    if updated_tasks:
        lines.append("**Tasks updated**:")
        for r in updated_tasks:
            label = r.get("target") or r.get("title") or "?"
            msg = r.get("message", r.get("output", ""))
            lines.append(f"  [~] {label}")
            if msg:
                lines.append(f"      → {str(msg)[:100]}")
        lines.append("")

    # DONE: Enrichments and logs (lower priority, compact)
    other_done = enriched + logged
    if other_done:
        lines.append("**Other updates**:")
        for r in other_done:
            lines.append(f"  [{'+' if r.get('status') == 'success' else '.'}] "
                         f"{_format_action_label(r)}")
        lines.append("")

    # Skill docs auto-fixed
    if skill_fixes:
        lines.append("**Skill docs auto-updated** (verify correctness):")
        for r in skill_fixes:
            skill_name = r.get("target") or r.get("skill", "?")
            msg = r.get("message", r.get("output", ""))
            lines.append(f"  [+] {skill_name}")
            if msg:
                lines.append(f"      → {str(msg)[:120]}")
        lines.append("")

    # YOUR CALL section (deferred proposals)
    proposals = report.get("proposals", [])
    if proposals:
        lines.append("**YOUR CALL** (approve or dismiss):")
        for i, p in enumerate(proposals, 1):
            label = p.get("target") or p.get("title") or "?"
            lines.append(f"  [{i}] {p['action']}: {label}")
            if p.get("rationale"):
                lines.append(f"      → {p['rationale'][:150]}")
        lines.append("")

    # Conflicts (stale state)
    conflicts_detail = report.get("conflicts_detail", [])
    if conflicts_detail:
        lines.append(f"**Stale state detected** ({len(conflicts_detail)} items):")
        for c in conflicts_detail:
            what = c.get("conflicts_with", c.get("artifact", "?"))
            rec = c.get("recommendation", "review needed")
            # Extract just the action (after last —) for brevity
            parts = rec.split(" — ")
            action = parts[-1] if len(parts) > 1 else rec
            lines.append(f"  - {what}")
            lines.append(f"    → {action}")
        lines.append("")

    # Pending skill fixes (proposals from previous runs)
    skill_fixes_file = get_skill_fixes_file()
    if skill_fixes_file.exists():
        try:
            pending_fixes = json.loads(skill_fixes_file.read_text())
            if pending_fixes:
                lines.append(f"**Skill fixes pending review** ({len(pending_fixes)}):")
                for i, fix in enumerate(pending_fixes, 1):
                    skill = fix.get('skill', '?')
                    change = fix.get('proposed_change', fix.get('new_content', ''))[:80]
                    lines.append(f"  [{i}] {skill}: {change}")
                lines.append("")
        except json.JSONDecodeError:
            pass

    # Failed actions
    failed_results = [r for r in results if r.get("status") == "failed"]
    if failed_results:
        lines.append("**Failed** (investigate):")
        for r in failed_results:
            lines.append(f"  [!] {_format_action_label(r)}")
            if r.get("error"):
                lines.append(f"      → {r['error'][:100]}")
        lines.append("")

    # KB extraction stats (what the daemon learned overnight)
    kb_stats = _load_kb_extraction_stats()
    if kb_stats:
        lines.append("**KB extraction stats**:")
        lines.extend(kb_stats)
        lines.append("")

    review = "\n".join(lines)

    # Write review file
    REVIEW_FILE.write_text(review)

    return review


def main():
    parser = argparse.ArgumentParser(description="Reconciliation pipeline executor")
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--plan", "-p", help="Action plan JSON file")
    input_group.add_argument("--stdin", action="store_true", help="Read plan from stdin")
    input_group.add_argument("--review", action="store_true", help="Generate review from last run")

    parser.add_argument("--dry-run", "-n", action="store_true", help="Log actions without executing")
    parser.add_argument("--output", "-o", help="Save execution report to JSON file")

    args = parser.parse_args()

    if args.review:
        review = generate_review()
        print(review)
        return

    # Load plan
    if args.plan:
        with open(args.plan) as f:
            plan = json.load(f)
    elif args.stdin:
        plan = json.load(sys.stdin)
    else:
        parser.print_help()
        return

    # Execute
    report = execute_plan(plan, dry_run=args.dry_run)

    # Generate review
    review = generate_review(report)
    print(review)

    # Save report
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
