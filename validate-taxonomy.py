#!/usr/bin/env python3
"""Validate category taxonomy changes by re-extracting recent sessions in parallel.

Runs artifact extraction (with new category/sub_category/key_terms fields) and
optionally fact extraction (with new canonical source principle) across the last
N sessions. Both use different OpenRouter models so they parallelize fully.

Usage:
    # Validate artifact extraction on last 10 sessions (dry-run, no DB writes)
    python3 validate-taxonomy.py --last 10

    # Full re-extraction on last 50 sessions (artifacts + facts, writes to DB)
    caffeinate -is python3 validate-taxonomy.py --last 50 --facts --write

    # Just show which sessions would be processed
    python3 validate-taxonomy.py --last 50 --dry-run
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir))

from config import get_sessions_dir, cfg, get_username_path_segment

SESSIONS_DIR = get_sessions_dir()
MIN_SIZE = cfg("backfill_min_session_size", 10000)


def get_project_name(path: Path) -> str:
    parts = path.parts
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            proj = parts[i + 1].lstrip('-').replace(get_username_path_segment(), '').replace('-', '/')
            return proj
    return "unknown"


def find_recent_sessions(last_n: int, min_size: int = MIN_SIZE) -> list[Path]:
    """Find the N most recent session files above min_size."""
    sessions = []
    for f in SESSIONS_DIR.rglob("*.jsonl"):
        if f.name.startswith("agent-"):
            continue
        if f.stat().st_size < min_size:
            continue
        sessions.append(f)
    sessions.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return sessions[:last_n]


def extract_artifacts(idx: int, total: int, path: Path) -> dict:
    """Run artifact extraction on a session (parallel-safe)."""
    import artifact_extract
    from extract import parse_session_jsonl, detect_session_domain

    proj = get_project_name(path)
    size = path.stat().st_size
    print(f"  [A {idx}/{total}] {proj}/{path.name} ({size // 1024}KB)", flush=True)

    try:
        transcript = parse_session_jsonl(str(path))
        if not transcript.strip():
            return {"type": "artifact", "status": "skipped", "idx": idx, "reason": "empty"}

        if len(transcript) > 60000:
            transcript = transcript[-60000:]

        domain = detect_session_domain(str(path))
        result = artifact_extract.call_extraction_model(transcript, artifact_extract.DEFAULT_MODEL)
        artifacts = result.get("artifacts", [])

        # Check for category fields
        has_category = sum(1 for a in artifacts if a.get("category"))
        has_key_terms = sum(1 for a in artifacts if a.get("key_terms"))

        return {
            "type": "artifact",
            "status": "ok",
            "idx": idx,
            "session": path.name,
            "project": proj,
            "domain": domain,
            "artifact_count": len(artifacts),
            "with_category": has_category,
            "with_key_terms": has_key_terms,
            "artifacts": artifacts,
            "errors": result.get("error_patterns", []),
        }
    except SystemExit:
        return {"type": "artifact", "status": "failed", "idx": idx, "reason": "extraction error"}
    except Exception as e:
        return {"type": "artifact", "status": "failed", "idx": idx, "reason": str(e)}


def extract_facts(idx: int, total: int, path: Path) -> dict:
    """Run fact extraction on a session (parallel-safe)."""
    import extract as ext

    proj = get_project_name(path)
    size = path.stat().st_size
    print(f"  [F {idx}/{total}] {proj}/{path.name} ({size // 1024}KB)", flush=True)

    try:
        transcript = ext.parse_session_jsonl(str(path))
        if not transcript.strip():
            return {"type": "fact", "status": "skipped", "idx": idx, "reason": "empty"}

        if len(transcript) > 50000:
            transcript = transcript[-50000:]

        result = ext.call_extraction_model(transcript, ext.DEFAULT_MODEL)
        facts = result.get("facts", [])

        # Check for lookup_path attributes
        lookup_paths = [f for f in facts if f.get("attribute", "").startswith("lookup_path")]

        return {
            "type": "fact",
            "status": "ok",
            "idx": idx,
            "session": path.name,
            "project": proj,
            "entity_count": len(result.get("entities", [])),
            "fact_count": len(facts),
            "lookup_paths": len(lookup_paths),
            "extractions": result,
        }
    except SystemExit:
        return {"type": "fact", "status": "failed", "idx": idx, "reason": "extraction error"}
    except Exception as e:
        return {"type": "fact", "status": "failed", "idx": idx, "reason": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Validate taxonomy changes on recent sessions")
    parser.add_argument("--last", type=int, default=10, help="Process last N sessions (default: 10)")
    parser.add_argument("--workers", "-w", type=int, default=8, help="Parallel workers (default: 8)")
    parser.add_argument("--facts", action="store_true", help="Also run fact extraction (tests canonical source principle)")
    parser.add_argument("--write", action="store_true", help="Write results (artifacts to pending, facts to DB)")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Just list sessions, don't extract")
    parser.add_argument("--min-size", type=int, default=MIN_SIZE, help=f"Min session size in bytes (default: {MIN_SIZE})")
    args = parser.parse_args()

    sessions = find_recent_sessions(args.last, args.min_size)
    print(f"Found {len(sessions)} sessions (last {args.last}, >{args.min_size // 1024}KB)\n")

    if args.dry_run or not sessions:
        for s in sessions:
            proj = get_project_name(s)
            mtime = datetime.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"  {mtime}  {s.stat().st_size // 1024:>5}KB  {proj}/{s.name}")
        return

    start = time.time()
    artifact_results = []
    fact_results = []

    # Submit all jobs
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}

        # Artifact extraction jobs
        for i, path in enumerate(sessions):
            f = pool.submit(extract_artifacts, i + 1, len(sessions), path)
            futures[f] = ("artifact", path)

        # Fact extraction jobs (if requested)
        if args.facts:
            for i, path in enumerate(sessions):
                f = pool.submit(extract_facts, i + 1, len(sessions), path)
                futures[f] = ("fact", path)

        for future in as_completed(futures):
            kind, path = futures[future]
            try:
                result = future.result()
                if kind == "artifact":
                    artifact_results.append(result)
                else:
                    fact_results.append(result)
            except Exception as e:
                print(f"  [!] {kind} {path.name}: {e}", flush=True)

    elapsed = time.time() - start

    # --- Report ---
    print(f"\n{'=' * 60}")
    print(f"Completed in {elapsed:.0f}s ({len(sessions)} sessions, {args.workers} workers)\n")

    # Artifact summary
    ok = [r for r in artifact_results if r["status"] == "ok"]
    failed = [r for r in artifact_results if r["status"] == "failed"]
    total_artifacts = sum(r["artifact_count"] for r in ok)
    with_cat = sum(r["with_category"] for r in ok)
    with_terms = sum(r["with_key_terms"] for r in ok)

    print(f"ARTIFACT EXTRACTION:")
    print(f"  Sessions: {len(ok)} ok, {len(failed)} failed")
    print(f"  Artifacts found: {total_artifacts}")
    print(f"  With category: {with_cat}/{total_artifacts} ({with_cat * 100 // max(total_artifacts, 1)}%)")
    print(f"  With key_terms: {with_terms}/{total_artifacts} ({with_terms * 100 // max(total_artifacts, 1)}%)")

    if total_artifacts > 0:
        print(f"\n  Sample artifacts with categories:")
        shown = 0
        for r in ok:
            for a in r.get("artifacts", []):
                if a.get("category") and shown < 10:
                    cat = f"{a.get('category', '?')}/{a.get('sub_category', '?')}"
                    terms = ", ".join(a.get("key_terms", [])[:3])
                    print(f"    [{cat}] {a.get('title', '?')[:60]}  terms=[{terms}]")
                    shown += 1

    if failed:
        print(f"\n  Failed sessions:")
        for r in failed:
            print(f"    [{r['idx']}] {r.get('reason', '?')}")

    # Fact summary (if run)
    if fact_results:
        ok_f = [r for r in fact_results if r["status"] == "ok"]
        failed_f = [r for r in fact_results if r["status"] == "failed"]
        total_facts = sum(r["fact_count"] for r in ok_f)
        total_lookups = sum(r["lookup_paths"] for r in ok_f)

        print(f"\nFACT EXTRACTION:")
        print(f"  Sessions: {len(ok_f)} ok, {len(failed_f)} failed")
        print(f"  Facts extracted: {total_facts}")
        print(f"  lookup_path attributes: {total_lookups}")

        if total_lookups > 0:
            print(f"\n  Sample lookup_paths:")
            shown = 0
            for r in ok_f:
                for f in r.get("extractions", {}).get("facts", []):
                    if f.get("attribute", "").startswith("lookup_path") and shown < 5:
                        print(f"    {f['entity_name']}: {f['value'][:80]}")
                        shown += 1

    # Write results if requested
    if args.write and total_artifacts > 0:
        import artifact_extract
        all_artifacts = []
        for r in ok:
            for a in r.get("artifacts", []):
                if (a.get("value") in ("very_high", "medium")
                        and a.get("persistence_status") != "persisted"):
                    all_artifacts.append(a)
            for e in r.get("errors", []):
                all_artifacts.append({"type": "error_pattern", **e})

        if all_artifacts:
            existing = artifact_extract.load_pending()
            ts = datetime.now(timezone.utc).isoformat()
            for a in all_artifacts:
                a["_meta"] = {"extracted_at": ts, "source_session": "validate-taxonomy"}
            existing.extend(all_artifacts)
            artifact_extract.save_pending(existing)
            print(f"\nSaved {len(all_artifacts)} artifacts to pending")

    if args.write and fact_results:
        import extract as ext
        db = ext.get_db()
        written = 0
        for r in [r for r in fact_results if r["status"] == "ok"]:
            try:
                ext.upsert_extractions(db, r["extractions"], f"validate:{r['session']}", "2026-03-10")
                written += 1
            except Exception as e:
                print(f"  [!] DB write failed for {r['session']}: {e}")
        db.close()
        print(f"  Wrote facts from {written} sessions to DB")

    print()


if __name__ == "__main__":
    main()
