-- 007 — project_sources (provenance)
--
-- Every fact about a project must trace back to a source. For corpus
-- rows the source is an article. For public-data rows it's a
-- solicitation, listing, STIP entry, appropriation line, recorder
-- filing, or planning agenda item.
--
-- (project_id, source_type, source_ref) is unique so re-runs of a
-- scraper don't create duplicate provenance rows; last_seen advances
-- on conflict.

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

CREATE INDEX IF NOT EXISTS idx_project_sources_project ON project_sources(project_id);
CREATE INDEX IF NOT EXISTS idx_project_sources_type    ON project_sources(source_type);

-- Backfill: every project with a source_article_id becomes an
-- 'article' provenance row. source_ref is the article id as text.
INSERT INTO project_sources (project_id, source_type, source_ref, confidence)
SELECT id, 'article', source_article_id::TEXT, 1.0
FROM projects
WHERE source_article_id IS NOT NULL
ON CONFLICT DO NOTHING;
