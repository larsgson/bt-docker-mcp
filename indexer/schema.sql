-- Structured index schema (Tier 2 — SQLite + FTS5).
--
-- Three retrieval paths share one database:
--   1. passage_refs (range overlap on encoded BBCCCVVV references)
--   2. tags         (exact-string lookup; replaces Aquifer's ACAI entity index)
--   3. chunks_fts   (FTS5 substring/keyword search with porter stemming)
--
-- Bodies live in `chunks` (separately addressable so an embedding pipeline
-- can later JOIN chunks ↔ vector_store on chunks.id without re-chunking).
--
-- Doc-level filter columns (resource_code, language, etc.) live in
-- documents.metadata (JSON) and become indexable scalar filters via
-- generated columns once the metadata schema stabilizes.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS documents (
  id           TEXT PRIMARY KEY,
  source_path  TEXT NOT NULL,
  source_sha   TEXT,
  title        TEXT NOT NULL,
  metadata     TEXT NOT NULL DEFAULT '{}',  -- JSON blob
  indexed_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path);

-- FTS5 over document titles. Used by `title_search` retriever for entity
-- lookups ("Who was Boaz?", "What is godliness?") where title-match is far
-- more reliable than body-match (titles are short and term-focused; bodies
-- saturate with noisy literal mentions of the entity name).
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  title,
  content='documents',
  content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, title) VALUES (new.rowid, new.title);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, title) VALUES('delete', old.rowid, old.title);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, title) VALUES('delete', old.rowid, old.title);
  INSERT INTO documents_fts(rowid, title) VALUES (new.rowid, new.title);
END;

CREATE TABLE IF NOT EXISTS chunks (
  id           TEXT PRIMARY KEY,
  doc_id       TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index  INTEGER NOT NULL,
  body         TEXT NOT NULL,
  UNIQUE(doc_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);

-- FTS5 virtual table over chunk bodies. Porter stemming fixes the
-- "justification ≠ justified" failure mode that Aquifer's substring search has.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  body,
  content='chunks',
  content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 2'
);

-- Triggers keep FTS5 in sync with chunks.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, body) VALUES (new.rowid, new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES('delete', old.rowid, old.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES('delete', old.rowid, old.body);
  INSERT INTO chunks_fts(rowid, body) VALUES (new.rowid, new.body);
END;

-- Passage references: encoded BBCCCVVV integers (e.g. Gen 1:1 = 1001001,
-- Romans 3:24 = 45003024). Range overlap = (a.start <= b.end AND a.end >= b.start).
CREATE TABLE IF NOT EXISTS passage_refs (
  doc_id          TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  start_bbcccvvv  INTEGER NOT NULL,
  end_bbcccvvv    INTEGER NOT NULL,
  PRIMARY KEY (doc_id, start_bbcccvvv, end_bbcccvvv)
);
CREATE INDEX IF NOT EXISTS idx_passage_start_end ON passage_refs(start_bbcccvvv, end_bbcccvvv);

CREATE TABLE IF NOT EXISTS tags (
  doc_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  tag     TEXT NOT NULL,
  PRIMARY KEY (doc_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- Free-form key/value: schema_version, source_root, source_repo_sha,
-- embedding_model_at_build (pinned for downstream vector reproducibility).
CREATE TABLE IF NOT EXISTS meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);
