-- UCD Research Platform — PostgreSQL schema
-- Apply with: psql $DATABASE_URL -f db/schema.sql
--
-- This file is the source of truth. Forward-only migrations live in
-- db/migrations/ and bring an existing database up to this state.

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
    primary_project_id  INTEGER,            -- legacy; use article_projects (is_primary=TRUE) for new code
    content_hash        TEXT,               -- stable hash of page text; cache key for probe_runs
    embedding           vector(1536),       -- populated by embed.py
    ingested_at         TIMESTAMPTZ
);

-- ── Canonical entities ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS projects (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    typology            TEXT,
    location            TEXT,
    city                TEXT,
    state               CHAR(2),
    county              TEXT,
    lat                 DOUBLE PRECISION,
    lng                 DOUBLE PRECISION,
    cost                TEXT,
    cost_usd            BIGINT,             -- corpus: usually final / actual
    estimated_cost_usd  BIGINT,             -- public_data: usually estimate / budget
    square_footage      TEXT,
    sq_ft               INTEGER,
    stories_levels      TEXT,
    delivery_method     TEXT,
    year_completed      INTEGER,
    status              TEXT,               -- corpus label: completed | under_construction | announced
    phase               TEXT
        CHECK (phase IN ('planning', 'design', 'approved', 'bidding', 'construction', 'completed')),
    source              TEXT NOT NULL DEFAULT 'corpus'
        CHECK (source IN ('corpus', 'public_data', 'merged')),
    source_article_id   INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    embedding           vector(1536)
);

ALTER TABLE articles
    DROP CONSTRAINT IF EXISTS fk_articles_primary_project;
ALTER TABLE articles
    ADD CONSTRAINT fk_articles_primary_project
    FOREIGN KEY (primary_project_id) REFERENCES projects(id)
    NOT VALID;                              -- NOT VALID allows adding after both tables exist

-- Many-to-many: an article can mention multiple projects; a project
-- can be referenced by many articles. is_primary preserves the "main
-- subject" notion when one exists.
CREATE TABLE IF NOT EXISTS article_projects (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (article_id, project_id)
);

CREATE TABLE IF NOT EXISTS firms (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    aliases       JSONB NOT NULL DEFAULT '[]',
    firm_type     TEXT NOT NULL DEFAULT 'unknown'
        CHECK (firm_type IN (
            'architect', 'engineer', 'contractor',
            'owner', 'developer', 'consultant', 'subcontractor',
            'other', 'unknown'
        )),
    firm_type_aux JSONB NOT NULL DEFAULT '[]',
    website       TEXT,
    notes         TEXT,
    embedding     vector(1536)        -- populated by embed.py
);

CREATE TABLE IF NOT EXISTS people (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    title      TEXT,
    firm_id    INTEGER REFERENCES firms(id),
    aliases    JSONB NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 1.0,
    notes      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name_firm
    ON people(name, COALESCE(firm_id, 0));

-- ── Provenance ───────────────────────────────────────────────────────────────

-- Every project row traces back to one or more sources. Corpus rows
-- get an 'article' source; public-data rows get a source_type that
-- matches the scraper origin.
CREATE TABLE IF NOT EXISTS project_sources (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_type  TEXT    NOT NULL CHECK (source_type IN (
        'article',
        'up3_solicitation',
        'dfcm_listing',
        'stip_entry',
        'appropriation_line',
        'recorder_filing',
        'planning_agenda'
    )),
    source_ref   TEXT    NOT NULL,
    confidence   REAL    NOT NULL DEFAULT 1.0,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, source_type, source_ref)
);

-- ── Relationships ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS roles (
    id          SERIAL PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    firm_id     INTEGER NOT NULL REFERENCES firms(id),
    role        TEXT NOT NULL,
    team        TEXT NOT NULL,              -- design | construction | owner
    raw_name    TEXT,
    confidence  REAL DEFAULT 1.0,
    scope       TEXT,                       -- parenthetical scope: "patching", "MEP", etc.
    CONSTRAINT uniq_roles_natural_key UNIQUE (project_id, firm_id, role, team)
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
    page              INTEGER,
    embedding         vector(1536)    -- populated by embed.py
);

-- ── Probe system ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS probes (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    prompt      TEXT NOT NULL,
    schema_json JSONB NOT NULL,
    model       TEXT,
    active      BOOLEAN DEFAULT TRUE,
    CONSTRAINT uniq_probes_name_version UNIQUE (name, version)
);

-- A probe_run is the result of (probe at version V) against (article
-- with content_hash H). The unique index treats a different
-- content_hash as a different run, so re-OCR does not collide and
-- history is preserved.
CREATE TABLE IF NOT EXISTS probe_runs (
    id            SERIAL PRIMARY KEY,
    probe_id      INTEGER NOT NULL REFERENCES probes(id),
    article_id    INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    probe_version INTEGER NOT NULL,
    model         TEXT,
    content_hash  TEXT,
    output_json   JSONB,
    ran_at        TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_probe_runs_unique
    ON probe_runs (
        probe_id,
        article_id,
        probe_version,
        COALESCE(content_hash, '')
    );

-- ── Entity resolution ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS firm_mentions (
    id           SERIAL PRIMARY KEY,
    raw_text     TEXT NOT NULL,
    canonical_id INTEGER REFERENCES firms(id),
    confidence   REAL,
    corrected    BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS person_mentions (
    id           SERIAL PRIMARY KEY,
    raw_name     TEXT NOT NULL,
    raw_title    TEXT,
    raw_firm     TEXT,
    canonical_id INTEGER REFERENCES people(id),
    confidence   REAL,
    corrected    BOOLEAN NOT NULL DEFAULT FALSE
);

-- ── Indexes ──────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_articles_issue        ON articles(issue_id);
CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_embedding    ON articles USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_projects_year         ON projects(year_completed);
CREATE INDEX IF NOT EXISTS idx_projects_typology     ON projects(typology);
CREATE INDEX IF NOT EXISTS idx_projects_source       ON projects(source);
CREATE INDEX IF NOT EXISTS idx_projects_phase        ON projects(phase);
CREATE INDEX IF NOT EXISTS idx_projects_county       ON projects(county);
CREATE INDEX IF NOT EXISTS idx_projects_embedding    ON projects USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_article_projects_project ON article_projects(project_id);
CREATE INDEX IF NOT EXISTS idx_project_sources_project  ON project_sources(project_id);
CREATE INDEX IF NOT EXISTS idx_project_sources_type     ON project_sources(source_type);
CREATE INDEX IF NOT EXISTS idx_claims_article        ON claims(article_id);
CREATE INDEX IF NOT EXISTS idx_claims_project        ON claims(project_id);
CREATE INDEX IF NOT EXISTS idx_claims_embedding      ON claims USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_quotes_embedding      ON quotes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_firms_embedding       ON firms USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_quotes_article        ON quotes(article_id);
-- Natural-key uniqueness on claims/quotes (plan §2.6). md5() keeps the
-- index size bounded if text grows long.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_natural_key
    ON claims (project_id, article_id, md5(text));
CREATE UNIQUE INDEX IF NOT EXISTS uniq_quotes_natural_key
    ON quotes (project_id, article_id, md5(text), speaker_name);
CREATE INDEX IF NOT EXISTS idx_roles_project         ON roles(project_id);
CREATE INDEX IF NOT EXISTS idx_roles_firm            ON roles(firm_id);
CREATE INDEX IF NOT EXISTS idx_roles_role            ON roles(role);
CREATE INDEX IF NOT EXISTS idx_probe_runs_article    ON probe_runs(article_id);
CREATE INDEX IF NOT EXISTS idx_firm_mentions_raw     ON firm_mentions(raw_text);
CREATE INDEX IF NOT EXISTS idx_firms_name_trgm       ON firms USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_firms_firm_type       ON firms(firm_type);
CREATE INDEX IF NOT EXISTS idx_firm_mentions_trgm    ON firm_mentions USING GIN (raw_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_person_mentions_raw   ON person_mentions(raw_name);
CREATE INDEX IF NOT EXISTS idx_person_mentions_trgm  ON person_mentions USING GIN (raw_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_people_name_trgm      ON people USING GIN (name gin_trgm_ops);

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
