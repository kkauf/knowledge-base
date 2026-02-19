#!/usr/bin/env python3
"""
Extraction pipeline — extract structured facts from Claude Code session transcripts.

Uses a cheap model via OpenRouter (Qwen 3.5) to identify:
- New entities (people, projects, companies, features)
- New or changed facts
- New relations between entities
- Decisions made

Then upserts into the knowledge base SQLite DB and regenerates BRIEF.md.

API key: Set OPENROUTER_API_KEY env var, or create ~/.claude/secrets/openrouter.env
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.path.expanduser("~/.claude/knowledge/knowledge.db")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.5-397b-a17b"

EXTRACTION_PROMPT = """You are a knowledge extraction system. Your job is to identify durable facts, decisions, and relationships from a conversation transcript.

IMPORTANT DISTINCTIONS:
- DURABLE FACTS: Things that are true beyond this conversation. "Marta quit." "Katherine's role changed to strategy." "Concierge matching feature removed."
- EPHEMERAL TASKS: Things to do this week. "Contact institutes Monday." "Send booking link." DO NOT extract these.
- DECISIONS: Choices made that change how things work. "Mandatory therapist onboarding going forward." "Konstantin handles document reviews."

For each extracted item, determine:
1. Is this a new entity? (person, project, company, concept, feature, tool)
2. Is this a fact about an existing entity? What attribute changed?
3. Is this a relationship between entities?
4. Is this a decision?

Return ONLY valid JSON in this exact format:
{
  "entities": [
    {"name": "Human-readable name", "type": "person|project|company|concept|feature|tool"}
  ],
  "facts": [
    {"entity_name": "Name", "attribute": "attribute_name", "value": "value", "supersedes": "old_value or null"}
  ],
  "relations": [
    {"from": "Entity A", "relation": "relation_type", "to": "Entity B", "ended": false}
  ],
  "decisions": [
    {"title": "Short title", "rationale": "Why this was decided"}
  ]
}

Rules:
- Entity names should be consistent (use full names for people: "Marta Sapor", not "Marta")
- Attributes should be lowercase, snake_case: "role", "status", "availability", "email"
- Relation types: "works_for", "member_of", "manages", "owns", "part_of", "depends_on", "married_to"
- If a relation ended, set "ended": true
- If a fact supersedes an old value, include the old value in "supersedes" (helps with temporal tracking)
- Only extract facts you are confident about. Skip ambiguous or speculative content.
- DO NOT extract tasks, action items, or to-do's. Only durable state changes.
- Keep it concise. 5 high-quality extractions beat 20 noisy ones.

Transcript:
"""


def get_api_key() -> str:
    """Get OpenRouter API key from env or secrets file."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key

    # Try loading from secrets file
    secrets_path = os.path.expanduser("~/.claude/secrets/openrouter.env")
    if os.path.exists(secrets_path):
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "OPENROUTER_API_KEY":
                        return v.strip()

    # Try VSS .env.local as fallback
    vss_env = os.path.expanduser("~/github/vss/.env.local")
    if os.path.exists(vss_env):
        with open(vss_env) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if "OPENROUTER" in k.upper():
                        return v.strip()

    print("Error: No OpenRouter API key found.", file=sys.stderr)
    print("Set OPENROUTER_API_KEY env var or create ~/.claude/secrets/openrouter.env", file=sys.stderr)
    sys.exit(2)


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def call_extraction_model(transcript: str, model: str = DEFAULT_MODEL) -> dict:
    """Call OpenRouter to extract structured knowledge from a transcript."""
    api_key = get_api_key()

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "user", "content": EXTRACTION_PROMPT + transcript}
        ],
        "temperature": 0.3,
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/kkaufmann/knowledge-base",
            "X-Title": "Knowledge Base Extraction",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Error: OpenRouter API returned {e.code}: {body}", file=sys.stderr)
        sys.exit(2)
    except urllib.error.URLError as e:
        print(f"Error: Could not reach OpenRouter: {e.reason}", file=sys.stderr)
        sys.exit(2)

    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        print("Error: Empty response from model", file=sys.stderr)
        print(f"Full response: {json.dumps(result, indent=2)}", file=sys.stderr)
        sys.exit(2)

    # Parse JSON from response (handle markdown code blocks)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"Error parsing model response as JSON: {e}", file=sys.stderr)
        print(f"Raw response:\n{content}", file=sys.stderr)
        sys.exit(2)


def upsert_extractions(db: sqlite3.Connection, extractions: dict, source: str, date: str):
    """Write extracted knowledge into the database."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    stats = {"entities": 0, "facts": 0, "relations": 0, "decisions": 0, "superseded": 0}

    # 1. Ensure all entities exist
    entity_map = {}  # name -> id
    for ent in extractions.get("entities", []):
        name = ent["name"]
        etype = ent.get("type", "concept")

        existing = db.execute(
            "SELECT * FROM entities WHERE lower(name) = lower(?)", (name,)
        ).fetchone()

        if existing:
            entity_map[name.lower()] = existing['id']
            db.execute("UPDATE entities SET updated_at = ? WHERE id = ?", (now, existing['id']))
        else:
            eid = str(uuid.uuid4())[:8]
            db.execute(
                "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (eid, name, etype, now, now)
            )
            entity_map[name.lower()] = eid
            stats["entities"] += 1

    # 2. Assert facts
    for fact in extractions.get("facts", []):
        entity_name = fact["entity_name"]
        attribute = fact["attribute"]
        value = fact["value"]

        # Find entity (might have been created above, or might already exist)
        eid = entity_map.get(entity_name.lower())
        if not eid:
            existing = db.execute(
                "SELECT id FROM entities WHERE lower(name) = lower(?)", (entity_name,)
            ).fetchone()
            if existing:
                eid = existing['id']
            else:
                # Create entity on the fly
                eid = str(uuid.uuid4())[:8]
                db.execute(
                    "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, 'concept', ?, ?)",
                    (eid, entity_name, now, now)
                )
                entity_map[entity_name.lower()] = eid
                stats["entities"] += 1

        # Supersede existing fact for same entity+attribute
        existing_fact = db.execute(
            "SELECT * FROM facts WHERE entity_id = ? AND attribute = ? AND valid_to IS NULL",
            (eid, attribute)
        ).fetchone()

        fact_id = str(uuid.uuid4())[:8]

        if existing_fact:
            # Don't supersede if value is the same
            if existing_fact['value'] == value:
                continue
            db.execute(
                "UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
                (date, fact_id, existing_fact['id'])
            )
            stats["superseded"] += 1

        db.execute(
            "INSERT INTO facts (id, entity_id, attribute, value, source, valid_from, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fact_id, eid, attribute, value, source, date, now)
        )

        stats["facts"] += 1

    # 3. Assert relations
    for rel in extractions.get("relations", []):
        from_name = rel["from"]
        to_name = rel["to"]
        rel_type = rel["relation"]
        ended = rel.get("ended", False)

        from_id = entity_map.get(from_name.lower()) or db.execute(
            "SELECT id FROM entities WHERE lower(name) = lower(?)", (from_name,)
        ).fetchone()
        to_id = entity_map.get(to_name.lower()) or db.execute(
            "SELECT id FROM entities WHERE lower(name) = lower(?)", (to_name,)
        ).fetchone()

        if isinstance(from_id, sqlite3.Row):
            from_id = from_id['id']
        if isinstance(to_id, sqlite3.Row):
            to_id = to_id['id']

        if not from_id or not to_id:
            continue  # Skip if entities can't be resolved

        if ended:
            # End existing relation
            db.execute(
                "UPDATE relations SET valid_to = ? WHERE from_entity_id = ? AND to_entity_id = ? AND relation_type = ? AND valid_to IS NULL",
                (date, from_id, to_id, rel_type)
            )
        else:
            # Check if relation already exists
            existing_rel = db.execute(
                "SELECT id FROM relations WHERE from_entity_id = ? AND to_entity_id = ? AND relation_type = ? AND valid_to IS NULL",
                (from_id, to_id, rel_type)
            ).fetchone()

            if not existing_rel:
                rid = str(uuid.uuid4())[:8]
                db.execute(
                    "INSERT INTO relations (id, from_entity_id, relation_type, to_entity_id, valid_from, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (rid, from_id, rel_type, to_id, date, now)
                )
                stats["relations"] += 1

    # 4. Log decisions
    for dec in extractions.get("decisions", []):
        did = str(uuid.uuid4())[:8]
        db.execute(
            "INSERT INTO decisions (id, title, rationale, status, decided_at, created_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (did, dec["title"], dec.get("rationale", ""), date, now)
        )
        stats["decisions"] += 1

    db.commit()
    return stats


def find_last_session() -> str:
    """Find the most recent Claude Code session transcript."""
    # Claude Code stores sessions in ~/.claude/projects/
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        print("Error: No Claude Code projects directory found", file=sys.stderr)
        sys.exit(2)

    # Find the most recent session file
    session_files = []
    for f in projects_dir.rglob("*.jsonl"):
        session_files.append(f)

    if not session_files:
        print("Error: No session files found", file=sys.stderr)
        sys.exit(2)

    latest = max(session_files, key=lambda f: f.stat().st_mtime)
    print(f"Found session: {latest}")

    # Read and extract human/assistant messages
    messages = []
    with open(latest) as f:
        for line in f:
            try:
                msg = json.loads(line)
                role = msg.get("role", "")
                if role in ("human", "assistant"):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # Extract text from content blocks
                        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                        content = "\n".join(text_parts)
                    if content:
                        messages.append(f"[{role}]: {content}")
            except json.JSONDecodeError:
                continue

    return "\n\n".join(messages[-50:])  # Last 50 messages to stay within model limits


def main():
    parser = argparse.ArgumentParser(description="Extract knowledge from Claude Code sessions")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--input', '-i', help='Path to transcript file')
    input_group.add_argument('--last-session', action='store_true', help='Use most recent Claude Code session')
    input_group.add_argument('--stdin', action='store_true', help='Read transcript from stdin')

    parser.add_argument('--model', '-m', default=DEFAULT_MODEL,
                        help=f'OpenRouter model ID (default: {DEFAULT_MODEL})')
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Show extractions without writing to DB')
    parser.add_argument('--date', '-d', default=None,
                        help='Date for facts (default: today, format: YYYY-MM-DD)')
    parser.add_argument('--source', '-s', default=None,
                        help='Source label for extracted facts')

    args = parser.parse_args()

    # Get transcript
    if args.input:
        with open(args.input) as f:
            transcript = f.read()
        source = args.source or f"file:{os.path.basename(args.input)}"
    elif args.last_session:
        transcript = find_last_session()
        source = args.source or "claude-session"
    elif args.stdin:
        transcript = sys.stdin.read()
        source = args.source or "stdin"

    date = args.date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    if not transcript.strip():
        print("Error: Empty transcript", file=sys.stderr)
        sys.exit(2)

    # Truncate if very long (keep under ~50k chars for cheap models)
    if len(transcript) > 50000:
        print(f"Transcript is {len(transcript)} chars, truncating to last 50000...")
        transcript = transcript[-50000:]

    print(f"Extracting from {len(transcript)} chars of transcript...")
    print(f"Model: {args.model} | Source: {source} | Date: {date}")
    print()

    # Extract
    extractions = call_extraction_model(transcript, args.model)

    # Display
    print("Extracted:")
    print(f"  Entities:  {len(extractions.get('entities', []))}")
    print(f"  Facts:     {len(extractions.get('facts', []))}")
    print(f"  Relations: {len(extractions.get('relations', []))}")
    print(f"  Decisions: {len(extractions.get('decisions', []))}")
    print()

    for ent in extractions.get("entities", []):
        print(f"  + Entity: {ent['name']} ({ent.get('type', 'concept')})")

    for fact in extractions.get("facts", []):
        supersedes = f" (was: {fact['supersedes']})" if fact.get('supersedes') else ""
        print(f"  + Fact: [{fact['entity_name']}] {fact['attribute']} = {fact['value']}{supersedes}")

    for rel in extractions.get("relations", []):
        action = "ended" if rel.get('ended') else "active"
        print(f"  + Relation: {rel['from']} -> {rel['relation']} -> {rel['to']} ({action})")

    for dec in extractions.get("decisions", []):
        print(f"  + Decision: {dec['title']}")

    if args.dry_run:
        print("\n[DRY RUN — nothing written]")
        return

    # Write to DB
    print()
    db = get_db()
    stats = upsert_extractions(db, extractions, source, date)
    db.close()

    print(f"Written: {stats['entities']} new entities, {stats['facts']} facts ({stats['superseded']} superseded), {stats['relations']} relations, {stats['decisions']} decisions")

    # Regenerate briefing
    print()
    # Import from same directory as this script
    script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(script_dir))
    import briefing
    briefing.generate()


if __name__ == '__main__':
    main()
