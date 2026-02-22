#!/usr/bin/env python3
"""
Reconciliation Pipeline — Orchestrator

Ties together all three stages:
  Stage 1: Artifact extraction (artifact_extract.py) — runs per-session in daemon
  Stage 2: Reconciliation (pipeline_reconcile.py) — compares artifacts vs system state
  Stage 3: Execution (executor.py) — applies actions with permission enforcement

Usage:
    # Run full pipeline: reconcile pending artifacts + execute actions
    python3 pipeline.py

    # Reconcile only (show action plan, don't execute)
    python3 pipeline.py --reconcile

    # Reconcile + execute
    python3 pipeline.py --reconcile --execute

    # Dry-run the whole pipeline
    python3 pipeline.py --dry-run

    # Show pipeline status
    python3 pipeline.py --status

    # Show and clear pending artifacts
    python3 pipeline.py --show-pending
    python3 pipeline.py --clear-pending
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

KB_DIR = Path.home() / ".claude" / "knowledge"
PENDING_FILE = KB_DIR / "artifacts-pending.json"
REVIEW_FILE = KB_DIR / "reconciliation-review.md"
AUDIT_LOG = KB_DIR / "reconciliation.log"
SKILL_FIXES_FILE = KB_DIR / "skill-fixes-pending.json"
SESSION_OFFSETS_FILE = KB_DIR / ".session-offsets.json"
ARTIFACT_OFFSETS_FILE = KB_DIR / ".artifact-offsets.json"

SCRIPTS_DIR = Path(__file__).resolve().parent
RECONCILE_SCRIPT = SCRIPTS_DIR / "pipeline_reconcile.py"
EXECUTOR_SCRIPT = SCRIPTS_DIR / "executor.py"
ARTIFACT_SCRIPT = SCRIPTS_DIR / "artifact_extract.py"


def show_status():
    """Show pipeline status: pending artifacts, recent actions, offset state."""
    print("## Pipeline Status\n")

    # Pending artifacts
    if PENDING_FILE.exists():
        try:
            pending = json.loads(PENDING_FILE.read_text())
            print(f"**Pending artifacts**: {len(pending)}")
            for a in pending:
                meta = a.get("_meta", {})
                print(f"  - [{a.get('type', '?')}] {a.get('title', '?')} "
                      f"(from {meta.get('source_session', '?')[:20]}...)")
        except (json.JSONDecodeError, OSError):
            print("**Pending artifacts**: error reading file")
    else:
        print("**Pending artifacts**: 0")
    print()

    # Skill fixes pending
    if SKILL_FIXES_FILE.exists():
        try:
            fixes = json.loads(SKILL_FIXES_FILE.read_text())
            print(f"**Skill fixes pending review**: {len(fixes)}")
            for fix in fixes:
                print(f"  - {fix.get('skill', '?')}: {fix.get('proposed_change', '?')[:80]}")
        except (json.JSONDecodeError, OSError):
            print("**Skill fixes**: error reading file")
    else:
        print("**Skill fixes pending**: 0")
    print()

    # Session offsets
    for label, path in [("Fact extraction offsets", SESSION_OFFSETS_FILE),
                        ("Artifact extraction offsets", ARTIFACT_OFFSETS_FILE)]:
        if os.path.exists(path):
            try:
                offsets = json.loads(Path(path).read_text())
                print(f"**{label}**: {len(offsets)} session(s) tracked")
                for k, v in sorted(offsets.items())[:5]:
                    print(f"  - {k[:30]}... → offset {v}")
                if len(offsets) > 5:
                    print(f"  ... and {len(offsets) - 5} more")
            except (json.JSONDecodeError, OSError):
                print(f"**{label}**: error reading file")
        else:
            print(f"**{label}**: not started")
    print()

    # Recent audit log
    if AUDIT_LOG.exists():
        lines = AUDIT_LOG.read_text().strip().split("\n")
        recent = lines[-10:] if len(lines) > 10 else lines
        print(f"**Recent audit log** (last {len(recent)} entries):")
        for line in recent:
            print(f"  {line}")
    else:
        print("**Audit log**: empty")
    print()

    # Last review
    if REVIEW_FILE.exists():
        print(f"**Last review**: {REVIEW_FILE}")
        print(REVIEW_FILE.read_text()[:500])
    else:
        print("**Last review**: none")


def run_reconcile(dry_run: bool = False, execute: bool = False, output: str = None):
    """Run Stage 2 reconciliation."""
    cmd = ["python3", str(RECONCILE_SCRIPT)]
    if dry_run:
        cmd.append("--dry-run")
    if execute:
        cmd.append("--execute")
    if output:
        cmd.extend(["--output", output])

    print("=" * 60)
    print("STAGE 2: Reconciliation")
    print("=" * 60)
    result = subprocess.run(cmd, timeout=300)
    return result.returncode


def run_executor(plan_file: str, dry_run: bool = False):
    """Run Stage 3 executor on an action plan."""
    cmd = ["python3", str(EXECUTOR_SCRIPT), "--plan", plan_file]
    if dry_run:
        cmd.append("--dry-run")

    print("=" * 60)
    print("STAGE 3: Execution")
    print("=" * 60)
    result = subprocess.run(cmd, timeout=120)
    return result.returncode


PROPOSALS_FILE = KB_DIR / "standup-proposals.json"


def show_proposals():
    """Show pending standup proposals."""
    if not PROPOSALS_FILE.exists():
        print("No pending proposals.")
        return []

    try:
        proposals = json.loads(PROPOSALS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        print("Error reading proposals file.")
        return []

    if not proposals:
        print("No pending proposals.")
        return []

    print(f"**{len(proposals)} proposal(s) awaiting review:**\n")
    for i, p in enumerate(proposals, 1):
        print(f"  [{i}] {p.get('action', '?')}: {p.get('target', '?')}")
        if p.get("rationale"):
            print(f"      Why: {p['rationale'][:120]}")
        if p.get("content"):
            print(f"      Evidence: {p['content'][:100]}")
        print()

    return proposals


def approve_proposals(indices: str, dry_run: bool = False):
    """Approve and execute specific proposals by index (1-based) or 'all'.

    Usage:
        pipeline.py --approve 1,3      # approve proposals 1 and 3
        pipeline.py --approve all       # approve all proposals
    """
    proposals = []
    if PROPOSALS_FILE.exists():
        try:
            proposals = json.loads(PROPOSALS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            print("Error reading proposals file.")
            return

    if not proposals:
        print("No pending proposals to approve.")
        return

    # Parse indices
    if indices.strip().lower() == "all":
        selected = list(range(len(proposals)))
    else:
        try:
            selected = [int(x.strip()) - 1 for x in indices.split(",")]
            # Validate
            for idx in selected:
                if idx < 0 or idx >= len(proposals):
                    print(f"Invalid index: {idx + 1} (valid: 1-{len(proposals)})")
                    return
        except ValueError:
            print(f"Invalid indices format: '{indices}'. Use comma-separated numbers or 'all'.")
            return

    approved = [proposals[i] for i in selected]
    dismissed_count = len(proposals) - len(approved)

    print(f"Approving {len(approved)} proposal(s), dismissing {dismissed_count}...")

    # Convert proposals to action plan format.
    # Override confidence to "high" since the human explicitly approved.
    actions = []
    for p in approved:
        action = {
            "type": p.get("action", ""),
            "target": p.get("target", ""),
            "title": p.get("target", ""),
            "content": p.get("content", ""),
            "rationale": f"[human-approved] {p.get('rationale', '')}",
            "source_artifact": p.get("source_artifact", "standup-approval"),
            "confidence": "high",  # Human approved → override confidence
        }
        actions.append(action)

    plan = {
        "proposed_actions": actions,
        "conflicts_flagged": [],
        "summary": f"Standup approval: {len(actions)} proposal(s) approved by user",
    }

    # Write to temp file and execute
    plan_file = "/tmp/standup-approved-plan.json"
    Path(plan_file).write_text(json.dumps(plan, indent=2))

    if dry_run:
        print(f"\n[DRY RUN] Would execute {len(actions)} action(s):")
        for a in actions:
            print(f"  - {a['type']}: {a['target']}")
        return

    exit_code = run_executor(plan_file)

    # Clear proposals file after execution (regardless of partial failures)
    PROPOSALS_FILE.write_text("[]")
    print(f"\nProposals cleared. {dismissed_count} dismissed, {len(approved)} executed.")


def dismiss_proposals():
    """Dismiss all pending proposals without executing."""
    if PROPOSALS_FILE.exists():
        try:
            proposals = json.loads(PROPOSALS_FILE.read_text())
            count = len(proposals)
        except (json.JSONDecodeError, OSError):
            count = 0
        PROPOSALS_FILE.write_text("[]")
        print(f"Dismissed {count} proposal(s).")
    else:
        print("No proposals to dismiss.")


def main():
    parser = argparse.ArgumentParser(
        description="Reconciliation Pipeline — orchestrates artifact extraction, reconciliation, and execution"
    )
    parser.add_argument("--status", action="store_true", help="Show pipeline status")
    parser.add_argument("--show-pending", action="store_true", help="Show pending artifacts")
    parser.add_argument("--clear-pending", action="store_true", help="Clear pending artifacts")
    parser.add_argument("--reconcile", action="store_true", help="Run reconciliation (Stage 2)")
    parser.add_argument("--execute", action="store_true", help="Execute action plan (Stage 3)")
    parser.add_argument("--plan", help="Execute a specific action plan file")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Dry-run all stages")
    parser.add_argument("--output", "-o", help="Save action plan to file")
    parser.add_argument("--show-proposals", action="store_true", help="Show pending standup proposals")
    parser.add_argument("--approve", metavar="INDICES",
                        help="Approve proposals by index (e.g., '1,3' or 'all')")
    parser.add_argument("--dismiss-proposals", action="store_true",
                        help="Dismiss all pending proposals without executing")

    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.show_pending:
        subprocess.run(["python3", str(ARTIFACT_SCRIPT), "--show-pending"])
        return

    if args.clear_pending:
        if PENDING_FILE.exists():
            PENDING_FILE.write_text("[]")
            print("Pending artifacts cleared.")
        else:
            print("No pending file to clear.")
        return

    if args.show_proposals:
        show_proposals()
        return

    if args.approve:
        approve_proposals(args.approve, dry_run=args.dry_run)
        return

    if args.dismiss_proposals:
        dismiss_proposals()
        return

    if args.plan:
        # Execute a specific action plan
        return run_executor(args.plan, dry_run=args.dry_run)

    if args.reconcile or (not args.status and not args.show_pending and not args.clear_pending
                          and not args.plan and not args.show_proposals
                          and not args.approve and not args.dismiss_proposals):
        # Default: run reconciliation
        output = args.output or "/tmp/reconciliation-plan.json"
        exit_code = run_reconcile(
            dry_run=args.dry_run,
            execute=args.execute,
            output=output if not args.execute else None,
        )

        if exit_code != 0:
            print(f"\nReconciliation failed (exit {exit_code})")
            sys.exit(exit_code)

        # If reconcile produced a plan and we want to execute separately
        if not args.execute and not args.dry_run and args.output:
            print(f"\nAction plan saved to {args.output}")
            print(f"To execute: python3 pipeline.py --plan {args.output}")

    print("\nDone.")


if __name__ == "__main__":
    main()
