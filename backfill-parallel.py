#!/usr/bin/env python3
"""Parallel backfill — process all historical sessions concurrently.

API calls run in parallel (the bottleneck at ~60s each).
DB writes happen sequentially on the main thread (SQLite is single-writer).

Usage:
    caffeinate -is python3 backfill-parallel.py          # Default 10 workers
    caffeinate -is python3 backfill-parallel.py -w 15    # 15 workers
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

import extract
import briefing

SESSIONS_DIR = Path.home() / ".claude" / "projects"
MIN_SIZE = 10_000
LOG_PATH = Path.home() / ".claude" / "knowledge" / "backfill.log"


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


def read_session_transcript(path: Path) -> str:
    messages = []
    with open(path) as f:
        for line in f:
            try:
                msg = json.loads(line)
                role = msg.get("type", "")
                if role not in ("user", "assistant"):
                    continue
                inner = msg.get("message", {})
                content = inner.get("content", "") if isinstance(inner, dict) else ""
                if isinstance(content, list):
                    text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    content = "\n".join(text_parts)
                if content:
                    messages.append(f"[{role}]: {content}")
            except json.JSONDecodeError:
                continue
    return "\n\n".join(messages[-50:])


def get_session_date(path: Path) -> str:
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y-%m-%d')


def get_project_name(path: Path) -> str:
    parts = path.parts
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            proj = parts[i + 1].lstrip('-').replace('-Users-kkaufmann-', '').replace('-', '/')
            return proj
    return "unknown"


def extract_from_session(idx: int, total: int, path: Path, model: str) -> dict:
    """Call the extraction API (parallel-safe, no DB access)."""
    proj = get_project_name(path)
    date = get_session_date(path)
    size = path.stat().st_size
    source = f"backfill:{proj}/{path.stem}"

    log(f"[{idx}/{total}] Extracting: {proj}/{path.name} ({size//1024}KB, {date})")

    transcript = read_session_transcript(path)
    if not transcript.strip():
        log(f"[{idx}/{total}] Skipped (empty)")
        return {"status": "skipped", "idx": idx}

    if len(transcript) > 50000:
        transcript = transcript[-50000:]

    try:
        extractions = extract.call_extraction_model(transcript, model)
    except SystemExit:
        log(f"[{idx}/{total}] FAILED (extraction error)")
        return {"status": "failed", "idx": idx}
    except Exception as e:
        log(f"[{idx}/{total}] FAILED ({e})")
        return {"status": "failed", "idx": idx}

    n = sum(len(extractions.get(k, [])) for k in ('entities', 'facts', 'relations', 'decisions'))
    if n == 0:
        log(f"[{idx}/{total}] Skipped (nothing extracted)")
        return {"status": "skipped", "idx": idx}

    return {
        "status": "extracted",
        "idx": idx,
        "total": total,
        "extractions": extractions,
        "source": source,
        "date": date,
    }


def main():
    parser = argparse.ArgumentParser(description="Parallel backfill from historical sessions")
    parser.add_argument('-w', '--workers', type=int, default=10, help='Parallel workers (default: 10)')
    parser.add_argument('--model', '-m', default=extract.DEFAULT_MODEL)
    parser.add_argument('--min-size', type=int, default=MIN_SIZE)
    parser.add_argument('--dry-run', '-n', action='store_true')
    args = parser.parse_args()

    sessions = []
    for f in SESSIONS_DIR.rglob("*.jsonl"):
        if f.name.startswith("agent-"):
            continue
        if f.stat().st_size < args.min_size:
            continue
        sessions.append(f)

    sessions.sort(key=lambda f: f.stat().st_mtime)
    total = len(sessions)

    log(f"Parallel backfill: {total} sessions, {args.workers} workers")

    if args.dry_run:
        for f in sessions:
            date = get_session_date(f)
            proj = get_project_name(f)
            print(f"  {date}  {f.stat().st_size//1024:>5}KB  {proj}")
        print(f"\nTotal: {total}")
        return

    start = time.time()
    succeeded = failed = skipped = 0

    # Single DB connection for all writes (main thread only)
    db = extract.get_db()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(extract_from_session, i + 1, total, path, args.model): path
            for i, path in enumerate(sessions)
        }

        for future in as_completed(futures):
            result = future.result()

            if result["status"] == "extracted":
                # Write to DB on main thread — no lock contention
                try:
                    stats = extract.upsert_extractions(
                        db, result["extractions"], result["source"], result["date"]
                    )
                    log(f"[{result['idx']}/{total}] OK: {stats['entities']}e {stats['facts']}f {stats['relations']}r {stats['decisions']}d ({stats['superseded']} superseded)")
                    succeeded += 1
                except Exception as e:
                    log(f"[{result['idx']}/{total}] FAILED writing DB ({e})")
                    failed += 1
            elif result["status"] == "failed":
                failed += 1
            else:
                skipped += 1

    db.close()

    elapsed = time.time() - start
    log(f"Backfill complete in {elapsed/60:.0f}m: {succeeded} ok, {failed} failed, {skipped} skipped")

    log("Regenerating BRIEF.md...")
    briefing.generate()


if __name__ == '__main__':
    main()
