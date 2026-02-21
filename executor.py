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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Paths ---

KB_DIR = Path.home() / ".claude" / "knowledge"
AUDIT_LOG = KB_DIR / "reconciliation.log"
REVIEW_FILE = KB_DIR / "reconciliation-review.md"

KONBAN_SCRIPT = Path.home() / ".claude" / "skills" / "konban" / "notion-api.py"
BRAIN_SCRIPT = Path.home() / ".claude" / "skills" / "notion-docs" / "notion-api.py"

# --- Permission Model ---

ALLOWED_ACTIONS = {
    "create_konban_task",   # Create new task (pending, tagged [daemon])
    "log_konban_task",      # Append log entry to existing task
    "create_brain_doc",     # Create new Brain doc under a section
    "enrich_brain_doc",     # Append new section to existing Brain doc (additive only)
    "done_konban_task",     # Mark task done (high-confidence only, Tier 1)
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

    if action_type == "done_konban_task":
        if not action.get("task_id") and not action.get("target"):
            return False, "DENIED: done_konban_task requires task_id or target"
        confidence = action.get("confidence", "low")
        if confidence != "high":
            return False, f"DEFERRED: done_konban_task requires high confidence (got {confidence})"

    return True, "ALLOWED"


def execute_create_konban_task(action: dict, dry_run: bool) -> dict:
    """Create a Konban task."""
    title = action.get("title") or action.get("target", "Untitled")
    # Tag all daemon-created tasks
    if "[daemon]" not in title:
        title = f"[daemon] {title}"

    priority = action.get("priority", "Medium")
    timebox = action.get("timebox")

    cmd = ["python3", str(KONBAN_SCRIPT), "create", title, "--priority", priority]
    if timebox:
        cmd.extend(["--timebox", timebox])

    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd)}

    exit_code, stdout, stderr = run_command(cmd)

    result = {"status": "success" if exit_code == 0 else "failed", "exit_code": exit_code}
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


def execute_log_konban_task(action: dict, dry_run: bool) -> dict:
    """Log a message on an existing Konban task."""
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
    task_id = action.get("task_id", "")
    target = action.get("target", "")
    evidence = action.get("content", "")
    source = action.get("source_artifact", "unknown")

    # Resolve task_id from target name if needed
    if not task_id and target:
        if dry_run:
            return {"status": "dry_run", "note": f"Would search for '{target}' then mark done"}

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


def execute_create_brain_doc(action: dict, dry_run: bool) -> dict:
    """Create a Brain doc under the appropriate section."""
    title = action.get("title") or action.get("target", "Untitled")
    content = action.get("content", "")
    source = action.get("source_artifact", "unknown")
    section = action.get("section", "Research")  # Default to Research for daemon-created docs
    domain = action.get("domain")

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

    Strategy: read existing doc, append new section, write back via update.
    Only safe when the new section is purely additive — we never touch existing content.
    Falls back to patch if the section already exists (re-enrichment replaces previous daemon section).
    """
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

    # Step 3: Append new section to existing content
    enriched = existing_content + new_section

    # Step 4: Write back via update (safe: we only added content)
    tmp_file = Path("/tmp") / f"daemon-enrich-{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
    tmp_file.write_text(enriched)
    cmd = ["python3", str(BRAIN_SCRIPT), "update", target,
           "--file", str(tmp_file), "--force"]  # --force because we're doing a full rewrite
    exit_code, stdout, stderr = run_command(cmd, timeout=90)
    tmp_file.unlink(missing_ok=True)

    result = {"status": "success" if exit_code == 0 else "failed",
              "exit_code": exit_code, "mode": "append_new"}
    if stdout:
        result["output"] = stdout
    if stderr:
        result["error"] = stderr
    return result


def execute_fix_skill(action: dict, dry_run: bool) -> dict:
    """Propose a skill fix — write to review file, never auto-apply."""
    skill = action.get("skill") or action.get("target", "unknown")
    change = action.get("content") or action.get("change", "")
    rationale = action.get("rationale", "")

    proposal = {
        "skill": skill,
        "proposed_change": change,
        "rationale": rationale,
        "source": action.get("source_artifact", "unknown"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Append to review file (human reviews at standup)
    review_path = KB_DIR / "skill-fixes-pending.json"
    existing = []
    if review_path.exists():
        try:
            existing = json.loads(review_path.read_text())
        except json.JSONDecodeError:
            existing = []

    existing.append(proposal)

    if not dry_run:
        review_path.write_text(json.dumps(existing, indent=2))

    return {"status": "proposed" if not dry_run else "dry_run", "proposal": proposal}


# Action type → executor function
EXECUTORS = {
    "create_konban_task": execute_create_konban_task,
    "log_konban_task": execute_log_konban_task,
    "done_konban_task": execute_done_konban_task,
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

        # Read existing doc, append cross-reference section, write back
        read_code, existing, _ = run_command(
            ["python3", str(BRAIN_SCRIPT), "read", group["brain_doc"], "--raw"], timeout=30
        )
        if read_code != 0:
            log_audit(f"  XREF: Could not read Brain doc '{group['brain_doc']}' for cross-referencing")
            continue

        import tempfile
        tmp = Path(tempfile.mktemp(suffix=".md", prefix="daemon-xref-"))
        tmp.write_text(existing + xref_section)
        run_command(
            ["python3", str(BRAIN_SCRIPT), "update", group["brain_doc"],
             "--file", str(tmp), "--force"],
            timeout=90,
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
        proposals_file = KB_DIR / "standup-proposals.json"
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

    # Summary line
    executed = report.get("actions_executed", 0)
    deferred = report.get("actions_deferred", 0)
    denied = report.get("actions_denied", 0)
    failed = report.get("actions_failed", 0)
    lines.append(f"**{executed} auto-executed, {deferred} for your review, "
                 f"{denied} denied, {failed} failed**")
    lines.append("")

    # DONE section (auto-executed, spot-check)
    auto_results = [r for r in report.get("results", [])
                    if r.get("status") in ("success", "dry_run", "no_action", "proposed")]
    if auto_results:
        lines.append("**DONE** (spot-check if wrong):")
        for r in auto_results:
            status_icon = {"success": "+", "dry_run": "~", "no_action": ".",
                           "proposed": "?"}.get(r.get("status"), "?")
            lines.append(f"  [{status_icon}] {r['action']}: {r['target']}")
            if r.get("page_id"):
                lines.append(f"      → Created: {r['page_id']}")
            if r.get("output"):
                lines.append(f"      → {r['output'][:100]}")
        lines.append("")

    # YOUR CALL section (deferred proposals)
    proposals = report.get("proposals", [])
    if proposals:
        lines.append("**YOUR CALL** (approve or dismiss):")
        for i, p in enumerate(proposals, 1):
            lines.append(f"  [{i}] {p['action']}: {p['target']}")
            if p.get("rationale"):
                lines.append(f"      → {p['rationale'][:120]}")
            if p.get("content"):
                lines.append(f"      Evidence: {p['content'][:100]}")
        lines.append("")

    # Conflicts
    if report.get("conflicts_detail"):
        lines.append(f"**Conflicts flagged** ({report['conflicts']}):")
        for c in report["conflicts_detail"]:
            lines.append(f"  - {c.get('artifact', '?')} vs {c.get('conflicts_with', '?')}")
            lines.append(f"    → {c.get('recommendation', 'review needed')}")
        lines.append("")

    # Pending skill fixes
    skill_fixes = KB_DIR / "skill-fixes-pending.json"
    if skill_fixes.exists():
        try:
            fixes = json.loads(skill_fixes.read_text())
            if fixes:
                lines.append(f"**Skill fixes pending review** ({len(fixes)}):")
                for fix in fixes:
                    lines.append(f"  - {fix.get('skill', '?')}: {fix.get('proposed_change', '')[:80]}")
                lines.append("")
        except json.JSONDecodeError:
            pass

    # Failed actions
    failed_results = [r for r in report.get("results", []) if r.get("status") == "failed"]
    if failed_results:
        lines.append("**Failed** (investigate):")
        for r in failed_results:
            lines.append(f"  [!] {r['action']}: {r['target']}")
            if r.get("error"):
                lines.append(f"      → {r['error'][:100]}")
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
