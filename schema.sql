-- Knowledge Base Schema
-- Temporal semantic knowledge store for Claude Code sessions

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('person', 'project', 'company', 'concept', 'feature', 'tool')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    superseded_by TEXT REFERENCES facts(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    from_entity_id TEXT NOT NULL REFERENCES entities(id),
    relation_type TEXT NOT NULL,
    to_entity_id TEXT NOT NULL REFERENCES entities(id),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'reversed')),
    context TEXT,
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Domain assignment: entities can belong to multiple domains
CREATE TABLE IF NOT EXISTS entity_domains (
    entity_id TEXT NOT NULL REFERENCES entities(id),
    domain TEXT NOT NULL CHECK(domain IN ('KH', 'Personal', 'Infrastructure', 'VSS', 'IsAI', 'Other')),
    confidence REAL NOT NULL DEFAULT 1.0,  -- 1.0 = certain, <1.0 = inferred
    source TEXT,  -- how this assignment was made (e.g., 'migration', 'extraction', 'manual')
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (entity_id, domain)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity_id);
CREATE INDEX IF NOT EXISTS idx_facts_current ON facts(entity_id, attribute) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_attribute ON facts(attribute, value);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_entity_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_entity_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_entity_domains_domain ON entity_domains(domain);
CREATE INDEX IF NOT EXISTS idx_entity_domains_entity ON entity_domains(entity_id);
