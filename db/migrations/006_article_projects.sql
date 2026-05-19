-- 006 — article_projects (many-to-many)
--
-- Replaces the implicit one-project-per-article model expressed by
-- articles.primary_project_id. A magazine feature can mention multiple
-- projects; one project can be referenced by many articles. The boolean
-- `is_primary` preserves the "main subject" notion when one exists.
--
-- Backfill: every existing articles.primary_project_id becomes a row
-- here with is_primary=TRUE. The legacy column stays in place (kept
-- in sync via trigger or removed in a later migration once readers are
-- updated). For now we keep it for read compatibility.

CREATE TABLE IF NOT EXISTS article_projects (
    article_id INTEGER NOT NULL REFERENCES articles(id)  ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id)  ON DELETE CASCADE,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (article_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_article_projects_project ON article_projects(project_id);

-- Backfill from the legacy column.
INSERT INTO article_projects (article_id, project_id, is_primary)
SELECT id, primary_project_id, TRUE
FROM articles
WHERE primary_project_id IS NOT NULL
ON CONFLICT DO NOTHING;
