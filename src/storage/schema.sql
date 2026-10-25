-- Schema version 1: local entities, directed triples, and synchronized FTS5.
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    canonical_name TEXT NOT NULL UNIQUE CHECK (length(trim(canonical_name)) > 0),
    entity_type TEXT NOT NULL DEFAULT 'concept' CHECK (length(trim(entity_type)) > 0),
    description TEXT NOT NULL DEFAULT '',
    properties TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(properties) AND json_type(properties) = 'object'),
    is_pinned INTEGER NOT NULL DEFAULT 0 CHECK (is_pinned IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'PRUNED')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

CREATE TABLE IF NOT EXISTS triples (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id > 0),
    subject_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate TEXT NOT NULL CHECK (length(trim(predicate)) > 0),
    object_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    properties TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(properties) AND json_type(properties) = 'object'),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'PRUNED')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (subject_id, predicate, object_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_entities_status_updated ON entities(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_triples_subject_status ON triples(subject_id, status);
CREATE INDEX IF NOT EXISTS idx_triples_object_status ON triples(object_id, status);
CREATE INDEX IF NOT EXISTS idx_triples_predicate_status ON triples(predicate, status);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_entities USING fts5(
    name, canonical_name, description,
    content='entities', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3 4'
);

CREATE TRIGGER IF NOT EXISTS entities_fts_insert AFTER INSERT ON entities BEGIN
    INSERT INTO fts_entities(rowid, name, canonical_name, description)
    VALUES (new.id, new.name, new.canonical_name, new.description);
END;

CREATE TRIGGER IF NOT EXISTS entities_fts_delete AFTER DELETE ON entities BEGIN
    INSERT INTO fts_entities(fts_entities, rowid, name, canonical_name, description)
    VALUES ('delete', old.id, old.name, old.canonical_name, old.description);
END;

CREATE TRIGGER IF NOT EXISTS entities_fts_update AFTER UPDATE OF name, canonical_name, description ON entities BEGIN
    INSERT INTO fts_entities(fts_entities, rowid, name, canonical_name, description)
    VALUES ('delete', old.id, old.name, old.canonical_name, old.description);
    INSERT INTO fts_entities(rowid, name, canonical_name, description)
    VALUES (new.id, new.name, new.canonical_name, new.description);
END;
