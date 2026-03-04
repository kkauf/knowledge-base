#!/usr/bin/env python3
"""
Centralized configuration for knowledge-base.

Config hierarchy (highest priority first):
  1. Environment variables
  2. ~/.knowledge-base/config.json (user config)
  3. Built-in defaults

Usage:
    from config import get_kb_dir, get_db_path, get_api_key, get_domains, cfg

    db = sqlite3.connect(str(get_db_path()))
    key = get_api_key()
    domains = get_domains()
"""

import json
import os
import sys
from pathlib import Path

# --- Defaults ---

DEFAULTS = {
    "kb_dir": "~/.knowledge-base",
    "sessions_dir": "~/.claude/projects",
    "skills_dir": "~/.claude/skills",
    "openrouter_api_key_sources": [
        "env:OPENROUTER_API_KEY",
        "~/.knowledge-base/secrets/openrouter.env",
    ],
    "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
    "extraction_model": "qwen/qwen3.5-397b-a17b",
    "reconciliation_model": "z-ai/glm-5",
    "http_referer": "",
    "context_overlap": 10,
    "max_transcript_chars": 50000,
    "artifact_max_transcript_chars": 60000,
    "reconciliation_batch_size": 15,
    "context_frame_ttl_hours": 6,
    "daemon_max_per_run": 5,
    "backfill_min_session_size": 10000,
    "domains": [],
    "git_repos": [],
    "owner_entity_names": [],
    "external_tools": {
        "konban_script": "",
        "brain_script": "",
        "recall_script": "",
    },
    "daemon_label": "org.knowledge-base.extract",
    "briefing": {
        "key_entities": [],
        "key_attrs": [],
        "domain_order": [],
    },
}


# --- Config loading ---

_config_cache = None


def _expand(val):
    """Expand ~ and env vars in string values."""
    if isinstance(val, str):
        return os.path.expanduser(os.path.expandvars(val))
    return val


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursing into nested dicts."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _find_config_file() -> Path | None:
    """Find config.json, checking env var override first."""
    env_path = os.environ.get("KNOWLEDGE_BASE_CONFIG")
    if env_path:
        p = Path(os.path.expanduser(env_path))
        if p.exists():
            return p

    default = Path.home() / ".knowledge-base" / "config.json"
    if default.exists():
        return default

    return None


def load_config(force_reload: bool = False) -> dict:
    """Load and cache merged configuration."""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    config = DEFAULTS.copy()

    config_file = _find_config_file()
    if config_file:
        try:
            with open(config_file) as f:
                user_config = json.load(f)
            config = _deep_merge(config, user_config)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Could not load config from {config_file}: {e}", file=sys.stderr)

    _config_cache = config
    return config


def cfg(key: str, default=None):
    """Get a config value by dot-separated key path.

    Example: cfg("briefing.key_entities") → ["Entity1", "Entity2"]
    """
    config = load_config()
    parts = key.split(".")
    val = config
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            return default
        if val is None:
            return default
    return val


# --- Path accessors (the main API) ---

def get_kb_dir() -> Path:
    """Knowledge base data directory."""
    return Path(_expand(load_config()["kb_dir"]))


def get_db_path() -> Path:
    """Path to knowledge.db."""
    return get_kb_dir() / "knowledge.db"


def get_sessions_dir() -> Path:
    """Claude Code sessions directory."""
    return Path(_expand(load_config()["sessions_dir"]))


def get_skills_dir() -> Path:
    """Skills directory (for skill improvement patches)."""
    return Path(_expand(load_config()["skills_dir"]))


def get_brief_path() -> Path:
    """Path to generated BRIEF.md."""
    return get_kb_dir() / "BRIEF.md"


def get_context_frame_path() -> Path:
    """Path to generated context-frame.md."""
    return get_kb_dir() / "context-frame.md"


def get_pending_file() -> Path:
    """Path to artifacts-pending.json."""
    return get_kb_dir() / "artifacts-pending.json"


def get_session_offsets_file() -> Path:
    """Path to .session-offsets.json."""
    return get_kb_dir() / ".session-offsets.json"


def get_artifact_offsets_file() -> Path:
    """Path to .artifact-offsets.json."""
    return get_kb_dir() / ".artifact-offsets.json"


def get_audit_log() -> Path:
    """Path to reconciliation.log."""
    return get_kb_dir() / "reconciliation.log"


def get_review_file() -> Path:
    """Path to reconciliation-review.md."""
    return get_kb_dir() / "reconciliation-review.md"


def get_skill_fixes_file() -> Path:
    """Path to skill-fixes-pending.json."""
    return get_kb_dir() / "skill-fixes-pending.json"


def get_proposals_file() -> Path:
    """Path to standup-proposals.json."""
    return get_kb_dir() / "standup-proposals.json"


def get_consistency_cache_file() -> Path:
    """Path to consistency-cache.json (cached state consistency check result)."""
    return get_kb_dir() / "consistency-cache.json"


def get_backfill_log() -> Path:
    """Path to backfill.log."""
    return get_kb_dir() / "backfill.log"


# --- Tool paths ---

def get_konban_script() -> Path | None:
    """Path to Konban helper script, or None if not configured."""
    config = load_config()
    path = config.get("external_tools", {}).get("konban_script", "")
    if path:
        return Path(_expand(path))
    # Fallback: check skills_dir
    fallback = get_skills_dir() / "konban" / "notion-api.py"
    return fallback if fallback.exists() else None


def get_brain_script() -> Path | None:
    """Path to Brain/knowledge-docs helper script, or None if not configured."""
    config = load_config()
    path = config.get("external_tools", {}).get("brain_script", "")
    if path:
        return Path(_expand(path))
    fallback = get_skills_dir() / "notion-docs" / "notion-api.py"
    return fallback if fallback.exists() else None


def get_linear_script() -> Path | None:
    """Path to Linear helper script, or None if not configured."""
    config = load_config()
    path = config.get("external_tools", {}).get("linear_script", "")
    if path:
        return Path(_expand(path))
    fallback = get_skills_dir() / "linear" / "linear-api.py"
    return fallback if fallback.exists() else None


def get_recall_script() -> Path | None:
    """Path to recall index builder script, or None if not configured."""
    config = load_config()
    path = config.get("external_tools", {}).get("recall_script", "")
    if path:
        return Path(_expand(path))
    return None


# --- API key ---

def get_api_key() -> str:
    """Get OpenRouter API key from configured sources.

    Checks sources in order:
    1. OPENROUTER_API_KEY environment variable
    2. Each path in openrouter_api_key_sources config
    """
    # Check env first (always)
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key

    # Check configured sources
    config = load_config()
    for source in config.get("openrouter_api_key_sources", []):
        if source.startswith("env:"):
            env_name = source[4:]
            key = os.environ.get(env_name)
            if key:
                return key
        else:
            path = Path(_expand(source))
            if path.exists():
                try:
                    with open(path) as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                if "OPENROUTER" in k.upper():
                                    return v.strip()
                except OSError:
                    continue

    print("Error: No OpenRouter API key found.", file=sys.stderr)
    print("Set OPENROUTER_API_KEY env var or configure openrouter_api_key_sources in config.json",
          file=sys.stderr)
    sys.exit(2)


# --- Domain detection ---

def get_domains() -> list[tuple[str, list[str]]]:
    """Get domain detection rules as list of (name, patterns) tuples.

    Each domain has a name and a list of path patterns to match against session paths.
    """
    config = load_config()
    domains = config.get("domains", [])
    result = []
    for d in domains:
        if isinstance(d, dict) and "name" in d and "patterns" in d:
            result.append((d["name"], d["patterns"]))
        elif isinstance(d, (list, tuple)) and len(d) == 2:
            result.append((d[0], d[1]))
    return result


def detect_domain(path: str) -> str | None:
    """Detect domain from a session path using configured rules."""
    path_lower = path.lower() if path else ""
    for name, patterns in get_domains():
        for pattern in patterns:
            if pattern.lower() in path_lower:
                return name
    return None


def get_domain_order() -> list[str]:
    """Get display order for domains in BRIEF.md."""
    config = load_config()
    order = config.get("briefing", {}).get("domain_order", [])
    if order:
        return order
    # Derive from configured domains + "Other"
    names = [name for name, _ in get_domains()]
    if "Other" not in names:
        names.append("Other")
    return names


# --- Git repos ---

def get_git_repos() -> list[Path]:
    """Get list of git repos to scan for commit history."""
    config = load_config()
    repos = config.get("git_repos", [])
    return [Path(_expand(r)) for r in repos]


# --- Owner entity names (for reconcile.py dedup) ---

def get_owner_entity_names() -> list[tuple[str, str]]:
    """Get owner entity name variants for semantic dedup.

    Returns list of (canonical, variant) tuples.
    The first name in the config list is canonical; all others are variants.
    """
    config = load_config()
    names = config.get("owner_entity_names", [])
    if len(names) < 2:
        return []
    canonical = names[0]
    return [(canonical, variant) for variant in names[1:]]


# --- Briefing config ---

def get_briefing_key_entities() -> list[str]:
    """Entities to always include in BRIEF.md key numbers section."""
    return cfg("briefing.key_entities") or []


def get_briefing_key_attrs() -> list[str]:
    """Attributes to always surface in BRIEF.md key numbers section."""
    return cfg("briefing.key_attrs") or []


# --- Model config ---

def get_extraction_model() -> str:
    """Model ID for fact extraction."""
    return load_config().get("extraction_model", DEFAULTS["extraction_model"])


def get_reconciliation_model() -> str:
    """Model ID for artifact extraction and reconciliation."""
    return load_config().get("reconciliation_model", DEFAULTS["reconciliation_model"])


def get_openrouter_url() -> str:
    """OpenRouter API URL."""
    return load_config().get("openrouter_url", DEFAULTS["openrouter_url"])


def get_http_referer() -> str:
    """HTTP-Referer header for OpenRouter (optional, for attribution)."""
    return load_config().get("http_referer", "")


# --- Daemon config ---

def get_daemon_label() -> str:
    """Launchd daemon label."""
    return load_config().get("daemon_label", DEFAULTS["daemon_label"])


# --- Username stripping for backfill ---

def get_username_path_segment() -> str:
    """Get the username path segment to strip from project paths (e.g., '-Users-jdoe-').

    Auto-detected from the current user's home directory.
    """
    home = str(Path.home())
    # /Users/jdoe → -Users-jdoe-
    return home.replace("/", "-") + "-"
