-- UCD Research Platform — PostgreSQL schema
-- Apply with: psql $DATABASE_URL -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector for embeddings
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram similarity for fuzzy firm matching

-- ── Source material ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS issues (
    id          SERIAL PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    year        INTEGER,
    month_label TEXT,
    page_count  INTEGER,
    ingested_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS articles (
    id                  SERIAL PRIMARY KEY,
    issue_id            INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    page_start          INTEGER NOT NULL,
    page_end            INTEGER NOT NULL,
    title               TEXT,
    author              TEXT,
    article_type        TEXT,               -- project_feature | column | advertisement | other
    summary             TEXT,
    primary_project_id  INTEGER,            -- FK added after projects table exists (see below)
    embedding           vector(1536),       -- populated by embed.py
    ingested_at         TIMESTAMPTZ
);

-- ── Canonical entities ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS projects (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    typology        TEXT,
    location        TEXT,
    city            TEXT,
    state           CHAR(2),
    cost            TEXT,
    cost_usd        BIGINT,
    square_footage  TEXT,
    sq_ft           INTEGER,
    stories_levels  TEXT,
    delivery_method TEXT,
    year_completed  INTEGER,
    status          TEXT,                   -- completed | under_construction | announced
    source_article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    embedding       vector(1536)
);

ALTER TABLE articles
    ADD CONSTRAINT fk_articles_primary_project
    FOREIGN KEY (primary_project_id) REFERENCES projects(id)
    NOT VALID;                              -- NOT VALID allows adding after both tables exist

CREATE TABLE IF NOT EXISTS firms (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    aliases JSONB DEFAULT '[]',
    website TEXT,
    notes   TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    title   TEXT,
    firm_id INTEGER REFERENCES firms(id),
    aliases JSONB DEFAULT '[]'
);

-- ── Relationships ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS roles (
    id          SERIAL PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    firm_id     INTEGER NOT NULL REFERENCES firms(id),
    role        TEXT NOT NULL,
    team        TEXT NOT NULL,              -- design | construction | owner
    raw_name    TEXT,
    confidence  REAL DEFAULT 1.0
);

-- ── Extracted content ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS claims (
    id          SERIAL PRIMARY KEY,
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    project_id  INTEGER REFERENCES projects(id),
    text        TEXT NOT NULL,
    type        TEXT,                       -- stat | milestone | challenge | award | first | other
    page        INTEGER,
    confidence  REAL DEFAULT 1.0,
    embedding   vector(1536)
);

CREATE TABLE IF NOT EXISTS quotes (
    id                SERIAL PRIMARY KEY,
    article_id        INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    project_id        INTEGER REFERENCES projects(id),
    speaker_name      TEXT,
    speaker_title     TEXT,
    speaker_firm      TEXT,
    speaker_person_id INTEGER REFERENCES people(id),
    text              TEXT NOT NULL,
    page              INTEGER
);

-- ── Probe system ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS probes (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    version     INTEGER NOT NULL DEFAULT 1,
    prompt      TEXT NOT NULL,
    schema_json JSONB NOT NULL,
    model       TEXT,
    active      BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS probe_runs (
    id            SERIAL PRIMARY KEY,
    probe_id      INTEGER NOT NULL REFERENCES probes(id),
    article_id    INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    probe_version INTEGER NOT NULL,
    output_json   JSONB,
    ran_at        TIMESTAMPTZ,
    UNIQUE (probe_id, article_id, probe_version)
);

-- ── Entity resolution ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS firm_mentions (
    id           SERIAL PRIMARY KEY,
    raw_text     TEXT NOT NULL,
    canonical_id INTEGER REFERENCES firms(id),
    confidence   REAL,
    corrected    BOOLEAN DEFAULT FALSE
);

-- ── Indexes ──────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_articles_issue        ON articles(issue_id);
CREATE INDEX IF NOT EXISTS idx_articles_embedding    ON articles USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_projects_year         ON projects(year_completed);
CREATE INDEX IF NOT EXISTS idx_projects_typology     ON projects(typology);
CREATE INDEX IF NOT EXISTS idx_projects_embedding    ON projects USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_claims_article        ON claims(article_id);
CREATE INDEX IF NOT EXISTS idx_claims_project        ON claims(project_id);
CREATE INDEX IF NOT EXISTS idx_claims_embedding      ON claims USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_quotes_article        ON quotes(article_id);
CREATE INDEX IF NOT EXISTS idx_roles_project         ON roles(project_id);
CREATE INDEX IF NOT EXISTS idx_roles_firm            ON roles(firm_id);
CREATE INDEX IF NOT EXISTS idx_roles_role            ON roles(role);
CREATE INDEX IF NOT EXISTS idx_firm_mentions_raw     ON firm_mentions(raw_text);
CREATE INDEX IF NOT EXISTS idx_firms_name_trgm       ON firms USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_firm_mentions_trgm    ON firm_mentions USING GIN (raw_text gin_trgm_ops);

-- Full-text search
ALTER TABLE articles  ADD COLUMN IF NOT EXISTS search_vector tsvector;
ALTER TABLE claims    ADD COLUMN IF NOT EXISTS search_vector tsvector;
ALTER TABLE quotes    ADD COLUMN IF NOT EXISTS search_vector tsvector;

CREATE INDEX IF NOT EXISTS idx_articles_fts ON articles USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS idx_claims_fts   ON claims   USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS idx_quotes_fts   ON quotes   USING GIN(search_vector);

-- Triggers to keep search_vector columns up to date
CREATE OR REPLACE FUNCTION update_articles_search() RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.title, '') || ' ' || coalesce(NEW.summary, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_articles_search
    BEFORE INSERT OR UPDATE ON articles
    FOR EACH ROW EXECUTE FUNCTION update_articles_search();

CREATE OR REPLACE FUNCTION update_claims_search() RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector('english', coalesce(NEW.text, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_claims_search
    BEFORE INSERT OR UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION update_claims_search();

CREATE OR REPLACE FUNCTION update_quotes_search() RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        coalesce(NEW.text, '') || ' ' || coalesce(NEW.speaker_name, '') || ' ' || coalesce(NEW.speaker_firm, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_quotes_search
    BEFORE INSERT OR UPDATE ON quotes
    FOR EACH ROW EXECUTE FUNCTION update_quotes_search();
