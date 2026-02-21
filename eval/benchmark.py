#!/usr/bin/env python3
"""
Reconciliation Pipeline Benchmark

Tests different models on the artifact extraction + reconciliation task.
Uses real transcripts from eval/cases.json as test data.

Usage:
    # Run all models on all cases (default: dry-run, prints results)
    python3 eval/benchmark.py

    # Run specific model
    python3 eval/benchmark.py --model qwen/qwen3.5-397b-a17b

    # Run specific case
    python3 eval/benchmark.py --case eval-001

    # Full benchmark (all models × all cases), save results
    python3 eval/benchmark.py --full --output eval/results.json
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# Add parent dir to path for extract.py imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from extract import parse_session_jsonl, get_api_key

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MODELS = {
    "qwen": "qwen/qwen3.5-397b-a17b",
    "glm5": "z-ai/glm-5",
    "sonnet": "anthropic/claude-sonnet-4-6",
}

EVAL_DIR = Path(__file__).parent
CASES_FILE = EVAL_DIR / "cases.json"


# --- Prompts ---

ARTIFACT_EXTRACTION_PROMPT = """You are an artifact extraction system for a personal knowledge management pipeline.

Your job: read a conversation transcript and identify STRUCTURED WORK PRODUCTS that have durable value beyond the session. These are plans, analyses, roadmaps, frameworks, and decision records that should be persisted somewhere.

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

Also identify ERROR PATTERNS: places where the assistant used a tool incorrectly, got an error, and had to retry. These are skill improvement signals.

Return ONLY valid JSON:
{
  "artifacts": [
    {
      "type": "analysis",
      "title": "Short descriptive title",
      "summary": "1-2 sentence summary of what this artifact contains",
      "value": "very_high",
      "persistence_status": "not_persisted | persisted | partial",
      "persistence_evidence": "Description of what was/wasn't saved, or null",
      "content_excerpt": "First 500 chars of the actual artifact content",
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

If no artifacts or error patterns found, return empty arrays. Do NOT hallucinate artifacts that aren't in the transcript.
"""

RECONCILIATION_PROMPT = """You are a reconciliation system for a personal knowledge management pipeline.

You receive:
1. ARTIFACTS extracted from conversation transcripts (structured work products)
2. CURRENT STATE of the user's task board (Konban) and knowledge base

Your job: determine what actions should be taken to reconcile the artifacts with current state.

RULES:
- Only propose actions for artifacts with value "medium" or "very_high"
- Only propose actions for artifacts with persistence_status "not_persisted" or "partial"
- Check if the artifact content already exists in Konban tasks or Brain docs (fuzzy match on title/content)
- Flag conflicts between artifact content and current state (but NEVER resolve them — flag only)
- Prefer enriching existing items (log on task) over creating new ones
- For error_patterns: propose SKILL.md fixes (propose only, never auto-apply)

PERMISSION MODEL (strict):
- CAN: create_konban_task (priority, title, tagged [daemon]), log_konban_task, create_brain_doc
- CANNOT: delete anything, modify Active Context, mark tasks done, send external comms

Return ONLY valid JSON:
{
  "proposed_actions": [
    {
      "type": "create_konban_task | log_konban_task | create_brain_doc | fix_skill | no_action",
      "target": "task title or doc title or skill name",
      "content": "what to create/log/fix",
      "source_artifact": "artifact title",
      "rationale": "why this action"
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
"""


def load_cases():
    """Load eval cases from cases.json."""
    with open(CASES_FILE) as f:
        data = json.load(f)
    return data["cases"], data.get("scoring_criteria", {})


def extract_transcript_segment(session_path: str, target_line: int = None, context_window: int = 100) -> str:
    """Extract relevant portion of transcript around the artifact.

    For benchmarking, we use a window around the target line to keep token count manageable.
    The full pipeline would process the entire transcript.
    """
    expanded = os.path.expanduser(session_path)
    if not os.path.exists(expanded):
        return None

    # Use the existing parser from extract.py
    transcript = parse_session_jsonl(expanded)

    # For benchmarking, truncate to reasonable size
    if len(transcript) > 30000:
        # If we have a target line hint, try to center on it
        # Otherwise take the last 30K chars (most relevant context)
        transcript = transcript[-30000:]

    return transcript


def call_model(prompt: str, user_content: str, model: str) -> dict:
    """Call a model via OpenRouter and parse JSON response."""
    api_key = get_api_key()

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "provider": {"data_collection": "deny"},
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/kkaufmann/knowledge-base",
            "X-Title": "KB Reconciliation Benchmark",
        },
    )

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": f"API {e.code}: {body[:200]}", "latency_s": time.time() - start}
    except urllib.error.URLError as e:
        return {"error": f"Network: {e.reason}", "latency_s": time.time() - start}

    latency = time.time() - start

    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = result.get("usage", {})

    # Parse JSON from response
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
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        parsed = {"parse_error": str(e), "raw_content": content[:500]}

    parsed["_meta"] = {
        "model": model,
        "latency_s": round(latency, 2),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
    }

    return parsed


def run_extraction(case: dict, model: str) -> dict:
    """Run artifact extraction on a single eval case."""
    transcript = extract_transcript_segment(
        case["transcript_path"],
        case.get("approximate_line"),
    )
    if not transcript:
        return {"error": f"Could not load transcript: {case['transcript_path']}"}

    print(f"  Extracting from {len(transcript)} chars of transcript...")
    return call_model(ARTIFACT_EXTRACTION_PROMPT, transcript, model)


def score_extraction(result: dict, case: dict) -> dict:
    """Score extraction result against expected outcomes."""
    scores = {}

    if "error" in result or "parse_error" in result:
        return {"error": result.get("error") or result.get("parse_error"), "total": 0}

    artifacts = result.get("artifacts", [])
    expected_actions = case.get("expected_actions", [])

    # Did it find artifacts?
    scores["artifacts_found"] = len(artifacts)

    # Did it correctly assess value?
    if artifacts:
        top_artifact = max(artifacts, key=lambda a: {"very_high": 3, "medium": 2, "low": 1}.get(a.get("value", "low"), 0))
        scores["top_value"] = top_artifact.get("value")
        scores["expected_value"] = case.get("value")
        scores["value_match"] = top_artifact.get("value") == case.get("value")
    else:
        scores["value_match"] = case.get("value") == "low"  # No artifacts found = correct for low-value cases

    # Did it correctly assess persistence?
    if artifacts:
        scores["persistence_detected"] = artifacts[0].get("persistence_status")
        scores["expected_persistence"] = case.get("persistence_status")

    # Did expected actions suggest no_action? (for ephemeral cases)
    is_ephemeral = all(a.get("type") == "no_action" for a in expected_actions)
    if is_ephemeral:
        # For ephemeral cases, finding 0 artifacts OR finding low-value artifacts is correct
        scores["correctly_ephemeral"] = len(artifacts) == 0 or all(
            a.get("value") == "low" for a in artifacts
        )
    else:
        scores["correctly_ephemeral"] = None  # Not applicable

    # Composite score (simple for now)
    score = 0
    if scores.get("value_match"):
        score += 1
    if scores.get("correctly_ephemeral") is True:
        score += 1
    if len(artifacts) > 0 and case.get("value") in ("very_high", "medium"):
        score += 1
    if len(artifacts) == 0 and case.get("value") == "low":
        score += 1
    scores["total"] = score

    return scores


def main():
    parser = argparse.ArgumentParser(description="Reconciliation pipeline benchmark")
    parser.add_argument("--model", "-m", help="Model key (qwen, glm5, sonnet) or full model ID")
    parser.add_argument("--case", "-c", help="Specific case ID to run")
    parser.add_argument("--full", action="store_true", help="Run all models × all cases")
    parser.add_argument("--output", "-o", help="Save results to JSON file")
    parser.add_argument("--list", action="store_true", help="List available cases and models")
    args = parser.parse_args()

    cases, criteria = load_cases()

    if args.list:
        print("Models:")
        for key, model_id in MODELS.items():
            print(f"  {key}: {model_id}")
        print("\nCases:")
        for case in cases:
            print(f"  {case['id']}: {case['title']} ({case['value']} value, {case['persistence_status']})")
        return

    # Determine which models and cases to run
    if args.full:
        models_to_run = list(MODELS.items())
    elif args.model:
        model_id = MODELS.get(args.model, args.model)
        models_to_run = [(args.model, model_id)]
    else:
        # Default: just Qwen (cheapest)
        models_to_run = [("qwen", MODELS["qwen"])]

    if args.case:
        cases_to_run = [c for c in cases if c["id"] == args.case]
        if not cases_to_run:
            print(f"Case not found: {args.case}")
            return
    else:
        # Skip "current" session case (can't self-reference)
        cases_to_run = [c for c in cases if c["transcript_path"] != "current_session"]

    results = {
        "benchmark_date": datetime.now().isoformat(),
        "models": {},
    }

    for model_key, model_id in models_to_run:
        print(f"\n{'='*60}")
        print(f"Model: {model_key} ({model_id})")
        print(f"{'='*60}")

        model_results = {"cases": {}, "totals": {"score": 0, "cost_estimate": 0, "latency_total": 0}}

        for case in cases_to_run:
            print(f"\n--- Case: {case['id']} ({case['title'][:40]}...) ---")

            extraction = run_extraction(case, model_id)
            scores = score_extraction(extraction, case)

            model_results["cases"][case["id"]] = {
                "extraction": extraction,
                "scores": scores,
            }
            model_results["totals"]["score"] += scores.get("total", 0)

            meta = extraction.get("_meta", {})
            model_results["totals"]["latency_total"] += meta.get("latency_s", 0)

            # Print summary
            artifacts = extraction.get("artifacts", [])
            errors = extraction.get("error_patterns", [])
            print(f"  Artifacts found: {len(artifacts)}")
            for a in artifacts:
                print(f"    - [{a.get('value', '?')}] {a.get('title', 'untitled')}")
            if errors:
                print(f"  Error patterns: {len(errors)}")
            print(f"  Score: {scores.get('total', 0)} | Latency: {meta.get('latency_s', '?')}s | Tokens: {meta.get('total_tokens', '?')}")

        results["models"][model_key] = model_results

    # Summary
    print(f"\n{'='*60}")
    print("BENCHMARK SUMMARY")
    print(f"{'='*60}")
    for model_key, mr in results["models"].items():
        t = mr["totals"]
        print(f"  {model_key}: score={t['score']} latency={t['latency_total']:.1f}s")

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
